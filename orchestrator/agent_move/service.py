"""Target-owned, recoverable Agent move transactions.

The source sends an opaque ``agent-move-v1`` archive.  The receiver validates
and stages it without touching live configuration, then performs an explicit
inactive import.  Activation is a separate operation so a source can be
disabled first.  Once a new Move records forward intent, every retry continues
that same cutover. An authenticated staged-only abort may resolve a rejected
policy preview without touching a committed or active target.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from orchestrator.agent_directory import AgentDirectory
from orchestrator.agent_incarnation import (
    AGENT_LIFECYCLE_FIELD,
    new_agent_lifecycle_id,
    valid_agent_lifecycle_id,
)
from orchestrator.config import (
    SESSION_MODE_BACKENDS,
    SUPPORTED_AGENT_MODES,
    VALID_ACCESS_SCOPES,
    default_agent_mode_for_backend,
)
from orchestrator.agent_config_json import write_agent_config_json
from orchestrator.config_json import (
    ConfigDocument,
    ConfigList,
    delete_config_json,
    file_write_lock,
    new_config_json,
    read_managed_json,
    write_config_json,
)
from orchestrator.flexible_backend_registry import (
    get_secret_lookup_order,
    migrate_provider_only_active_backend,
    normalize_allowed_backends,
)
from orchestrator.telegram_configuration_lock import TELEGRAM_PROTECTED_SECRET_KEYS
from orchestrator.telegram_delivery_state import (
    adopt_transferred_state,
    retire_agent_state,
    telegram_bot_fingerprint,
)
from orchestrator.session_store import (
    ConversationContinuityBinding,
    SessionConflict,
    SessionStore,
)

from .package import (
    AGENT_MOVE_CAPABILITY,
    AGENT_ID_MAX_LENGTH,
    CONVERSATION_CONTINUITY_CAPABILITY,
    RETAIN_SOURCE_HISTORY_CAPABILITY,
    AGENT_TRANSFER_LIFECYCLE_CAPABILITY,
    INACTIVE_MOVE_CAPABILITY,
    INACTIVE_CLONE_BACKUP_CAPABILITY,
    MAX_TRANSFER_PACKAGE_BYTES,
    TRANSFER_MODES_CAPABILITY,
    PACKAGE_SCHEMA_MIN_VERSION,
    PACKAGE_SCHEMA_VERSION,
    RETAINED_IDENTITY_CAPABILITY,
    AgentMoveArchive,
    AgentMoveError,
    authenticated_conversation_command_owner,
    configured_archive_member_limit,
    configured_transfer_package_limit,
    configured_unpacked_byte_limit,
    receiver_unpacked_byte_limit,
    configured_workspace_byte_limit,
    receiver_workspace_byte_limit,
    receiver_archive_member_limit,
    AccessScopePreviewChanged,
    CloneImportPreflightError,
    normalize_agent_id,
    package_sha256,
    configured_conversation_owner_id,
    establish_personal_target_conversation_owner,
    decrypt_agent_secrets,
    detect_environment_kind,
    extract_agent_workspace,
    read_retained_identity_bytes,
    read_agent_move_package,
    utc_now_iso,
    validate_agent_id_for_platform,
)
from .transport_crypto import ENVELOPE_SCHEME

MAX_PACKAGE_BYTES = MAX_TRANSFER_PACKAGE_BYTES
MOVE_STATE_SCHEMA_VERSION = 2
_ACCESS_RANK = {"workspace": 0, "project": 1, "drive": 2}
_SOURCE_TRANSFER_FIELDS = (
    "transfer_import_state",
    "transfer_package_id",
    "transfer_source_instance",
    "transfer_state",
    "transfer_target",
)
_TRANSFER_OPERATIONS = {"legacy_move", "move", "clone"}
_SOURCE_BOUND_CONFIG_FIELDS = {
    "bridge_home",
    "code_root",
    "config_path",
    "project_root",
    "runtime_dir",
    "session_id",
    "workspace",
    "worker_pid",
}


class InvalidTargetScopePolicy(AgentMoveError):
    """A configured target scope value cannot be interpreted."""


def receiver_capabilities(hashi_root: Path | str) -> dict[str, Any]:
    root = _root(hashi_root)
    policy_error = {}
    try:
        maximum = _target_max_access_scope(root)
    except InvalidTargetScopePolicy as exc:
        maximum = None
        policy_error["max_access_scope_error"] = str(exc)
    return {
        "ok": True,
        "capability": AGENT_MOVE_CAPABILITY,
        "capabilities": [
            AGENT_MOVE_CAPABILITY,
            TRANSFER_MODES_CAPABILITY,
            RETAINED_IDENTITY_CAPABILITY,
            AGENT_TRANSFER_LIFECYCLE_CAPABILITY,
            INACTIVE_MOVE_CAPABILITY,
            INACTIVE_CLONE_BACKUP_CAPABILITY,
            CONVERSATION_CONTINUITY_CAPABILITY,
            RETAIN_SOURCE_HISTORY_CAPABILITY,
        ],
        "package_type": "hashi-agent-move",
        "schema_min": PACKAGE_SCHEMA_MIN_VERSION,
        "schema_max": PACKAGE_SCHEMA_VERSION,
        "max_package_bytes": configured_transfer_package_limit(),
        "max_archive_members": configured_archive_member_limit(),
        "max_unpacked_bytes": configured_unpacked_byte_limit(),
        "max_workspace_bytes": configured_workspace_byte_limit(),
        "environment_kind": detect_environment_kind(),
        "instance_id": _configured_instance_id(root),
        "target_owned_import": True,
        "staged_inactive": True,
        "encrypted_agent_secrets": True,
        "package_encryption": [ENVELOPE_SCHEME],
        "streaming_upload": "authenticated-query-gcm-v1",
        "source_workspace_retained": True,
        "retained_identity_attachment": True,
        "move_clone_lifecycle": True,
        "target_id_suffixing": True,
        "clone_without_telegram": True,
        "conversation_continuity": {
            "version": 1,
            "move_default": "move",
            "clone_modes": ["none", "inherit_read_only", "copy"],
        },
        "max_access_scope": maximum,
        **policy_error,
    }


def resolve_agent_transfer_target(
    hashi_root: Path | str,
    source_agent_id: str,
    *,
    operation: str,
    requested_agent_id: str | None = None,
    target_platform: str | None = None,
) -> dict[str, Any]:
    """Resolve the stable target ID without mutating receiver state."""

    root = _root(hashi_root)
    transfer_operation = _normalize_operation(operation)
    if transfer_operation == "legacy_move":
        transfer_operation = "move"
    platform = target_platform or detect_environment_kind()
    source_name = normalize_agent_id(source_agent_id)
    occupied = _occupied_agent_ids(root)
    explicit = str(requested_agent_id or "").strip()
    if explicit:
        selected = validate_agent_id_for_platform(
            explicit,
            platform,
        )
        collision = occupied.get(selected.casefold())
        if collision is not None:
            raise AgentMoveError(
                f"target Agent ID '{selected}' is already used by '{collision}'"
            )
    else:
        base = source_name
        try:
            selected = validate_agent_id_for_platform(base, platform)
        except AgentMoveError:
            if transfer_operation != "clone":
                raise
            # Source IDs are metadata, not target paths. A Unix Agent named
            # CON.txt can be cloned to Windows with an explicit or safe auto ID.
            base = f"clone_{source_name.rstrip('.')}"[:AGENT_ID_MAX_LENGTH]
            selected = validate_agent_id_for_platform(base, platform)
        suffix = 0
        while selected.casefold() in occupied:
            suffix += 1
            tail = f"_{suffix}"
            selected = validate_agent_id_for_platform(
                f"{base[:AGENT_ID_MAX_LENGTH - len(tail)]}{tail}",
                platform,
            )
    return {
        "ok": True,
        "operation": transfer_operation,
        "source_agent_id": source_name,
        "target_agent_id": selected,
        "renamed": selected != source_name,
    }


def _staged_archive_member_limit(record: Mapping[str, Any]) -> int:
    accepted = record.get("archive_member_count")
    if accepted is not None:
        return receiver_archive_member_limit({"max_archive_members": accepted})
    # Old journals were accepted under the former built-in limit. A later
    # lower configuration must not invalidate an already staged transaction.
    return max(configured_archive_member_limit(), receiver_archive_member_limit({}))


def _staged_unpacked_byte_limit(record: Mapping[str, Any]) -> int:
    accepted = record.get("archive_unpacked_bytes")
    if accepted is not None:
        return receiver_unpacked_byte_limit({"max_unpacked_bytes": accepted})
    return max(configured_unpacked_byte_limit(), receiver_unpacked_byte_limit({}))


def _staged_workspace_byte_limit(record: Mapping[str, Any]) -> int:
    accepted = record.get("archive_workspace_bytes")
    if accepted is not None:
        return receiver_workspace_byte_limit({"max_workspace_bytes": accepted})
    return max(configured_workspace_byte_limit(), receiver_workspace_byte_limit({}))


def stage_agent_move(
    hashi_root: Path | str,
    package_bytes: bytes | Path,
    *,
    expected_sha256: str,
    source_instance: str,
    target_instance: str | None = None,
    secret_passphrase: str | None = None,
    target_platform: str | None = None,
    operation: str = "legacy_move",
    target_agent_id: str | None = None,
) -> dict[str, Any]:
    """Verify and persist an archive without changing Agent configuration."""

    root = _root(hashi_root)
    member_limit = configured_archive_member_limit()
    unpacked_limit = configured_unpacked_byte_limit()
    workspace_limit = configured_workspace_byte_limit()
    package_limit = configured_transfer_package_limit()
    source = _normalize_instance(source_instance)
    transfer_operation = _normalize_operation(operation)
    source_file = package_bytes if isinstance(package_bytes, Path) else None
    size = source_file.stat().st_size if source_file else len(package_bytes)
    if not size:
        raise AgentMoveError("Agent move package is empty")
    if size > package_limit:
        raise AgentMoveError(f"Agent move package is {size} bytes; receiver limit is {package_limit} bytes")

    move_root = _move_root(root)
    target_kind = target_platform or detect_environment_kind()
    inbox = move_root / "incoming"
    inbox.mkdir(parents=True, exist_ok=True)
    upload = inbox / f".upload-{uuid4().hex}.hashi-agent"
    digest_state = hashlib.sha256()
    try:
        with upload.open("xb") as writer:
            os.chmod(upload, 0o600)
            if source_file:
                copied = 0
                with source_file.open("rb") as reader:
                    while chunk := reader.read(1024 * 1024):
                        copied += len(chunk)
                        if copied > package_limit:
                            raise AgentMoveError(f"Agent move package exceeds {copied} bytes; receiver limit is {package_limit} bytes")
                        digest_state.update(chunk)
                        writer.write(chunk)
                size = copied
            else:
                digest_state.update(package_bytes)
                writer.write(package_bytes)
        digest = digest_state.hexdigest()
        if not expected_sha256 or digest != str(expected_sha256).strip().lower():
            raise AgentMoveError("Agent move package SHA-256 does not match the request")
    except BaseException:
        upload.unlink(missing_ok=True)
        raise

    incomplete_record_dir: Path | None = None
    try:
        package = read_agent_move_package(
            upload,
            verify=True,
            target_platform=target_kind,
            max_archive_members=member_limit,
            max_unpacked_bytes=unpacked_limit,
            max_workspace_bytes=workspace_limit,
        )
        manifest_operation = str(package.manifest.get("operation") or "").lower()
        if transfer_operation != "legacy_move" and manifest_operation != transfer_operation:
            raise AgentMoveError(
                "Agent transfer operation does not match the package manifest"
            )
        if (package.manifest.get("desired_target_active") is False
                and transfer_operation not in {"move", "clone"}):
            raise AgentMoveError("inactive target policy requires a modern transfer")
        backup_clone = (
            transfer_operation == "clone"
            and package.manifest.get("desired_target_active") is False
            and package.manifest.get("retain_telegram_backup") is True
        )
        if (transfer_operation == "clone" and
                package.access_requirements.get("telegram_secret_included") and not backup_clone):
            raise AgentMoveError("active clone packages must not contain a Telegram token")
        manifest_source = _normalize_instance(package.manifest.get("source_instance"))
        if manifest_source != source:
            raise AgentMoveError(
                f"package source_instance {manifest_source!r} does not match authenticated sender {source!r}"
            )
        target = _normalize_instance(
            target_instance or _configured_instance_id(root) or "HASHI"
        )
        if target == source and transfer_operation != "clone":
            raise AgentMoveError("source and target HASHI instances must be different")
        local_principal = None
        if source == target and transfer_operation == "clone":
            local_principal = authenticated_conversation_command_owner(root)
        local_history_owner = (
            str(package.conversation_continuity.get("owner_id") or "").strip()
            if package.conversation_continuity is not None else ""
        )
        governed_target_owner = None
        history_mapping_downgraded = False
        try:
            governed_target_owner = _require_governed_history_mapping(
                root, package, local_authenticated=bool(
                    local_principal and local_principal == local_history_owner
                ),
            )
        except GovernedHistoryMappingUnavailable:
            # The transfer itself is still valid; only conversation history
            # cannot yet be attributed to a verified destination user. Fall
            # back to an Agent-only transfer instead of rejecting the Clone.
            history_mapping_downgraded = True
        if "secrets/agent.enc" in package.names:
            # Decrypt during staging so a bad/mismatched shared key fails before
            # any target configuration is touched.
            decrypted = decrypt_agent_secrets(package, secret_passphrase)
            telegram_key = str(
                package.access_requirements.get("telegram_secret_key") or ""
            )
            if (
                transfer_operation == "clone"
                and telegram_key
                and telegram_key in decrypted
                and not backup_clone
            ):
                raise AgentMoveError("active clone packages must not contain a Telegram token")
            if (backup_clone and bool(telegram_key and telegram_key in decrypted)
                    != package.access_requirements.get("telegram_secret_included")):
                raise AgentMoveError("Telegram backup declaration does not match encrypted credentials")

        with _mutation_lock(root):
            record_dir = _record_dir(root, package.package_id)
            record_path = record_dir / "state.json"
            package_path = record_dir / "package.hashi-agent"
            if record_path.exists():
                current = _load_json(record_path)
                if str(current.get("sha256") or "").lower() != digest.lower():
                    raise AgentMoveError(
                        "package_id already exists with different content"
                    )
                if _record_operation(current) != transfer_operation:
                    raise AgentMoveError(
                        "package_id already exists for a different transfer operation"
                    )
                recorded_target = str(current.get("target_instance") or "").strip()
                if recorded_target and _normalize_instance(recorded_target) != target:
                    raise AgentMoveError(
                        "package_id already exists for a different target instance"
                    )
                recorded_source = str(current.get("source_instance") or "").strip()
                if recorded_source and _normalize_instance(recorded_source) != source:
                    raise AgentMoveError(
                        "package_id already exists for a different source instance"
                    )
                requested = str(target_agent_id or "").strip()
                current_target = str(
                    current.get("target_agent_id") or current.get("agent_id") or ""
                )
                if requested and requested != current_target:
                    raise AgentMoveError(
                        "package_id already exists for a different target Agent ID"
                    )
                upload.unlink(missing_ok=True)
                return _public_state(current)

            if transfer_operation == "legacy_move":
                selected_agent_id = package.agent_id
                dormant = _assert_target_available(
                    root,
                    selected_agent_id,
                    source_instance=source,
                )
            else:
                selected_agent_id = resolve_agent_transfer_target(
                    root,
                    package.agent_id,
                    operation=transfer_operation,
                    requested_agent_id=target_agent_id,
                    target_platform=target_kind,
                )["target_agent_id"]
                dormant = None
            if transfer_operation == "clone":
                _preflight_clone_import(
                    root, package, target_agent_id=selected_agent_id,
                    passphrase=secret_passphrase,
                )
            if package.conversation_continuity is not None and (source != target or not local_principal):
                from orchestrator.telegram_configuration_lock import telegram_configuration_lock

                # Bind the target's local data partition once, without using it
                # as network authority. Keep configuration stable during the
                # one-time migration lookup.
                with (
                    telegram_configuration_lock(root / "secrets.json"),
                    file_write_lock(root / "secrets.json"),
                    file_write_lock(root / "agents.json"),
                ):
                    establish_personal_target_conversation_owner(root)
            record_dir.mkdir(parents=True, exist_ok=False)
            incomplete_record_dir = record_dir
            os.replace(upload, package_path)
            package = read_agent_move_package(
                package_path,
                verify=True,
                target_platform=target_kind,
                max_archive_members=member_limit,
                max_unpacked_bytes=unpacked_limit,
                max_workspace_bytes=workspace_limit,
            )
            retained_identity = _preserve_retained_identity(
                root,
                record_dir,
                package,
            )
            credential_status = _credential_status(
                root,
                package,
                secret_passphrase,
                target_agent_id=selected_agent_id,
            )
            access_scope_plan = _access_scope_plan(root, package)
            record = {
                "schema_version": MOVE_STATE_SCHEMA_VERSION,
                "package_id": package.package_id,
                "agent_id": package.agent_id,
                "source_agent_id": package.agent_id,
                "target_agent_id": selected_agent_id,
                "operation": transfer_operation,
                "desired_target_active": package.manifest.get("desired_target_active", True),
                "retain_telegram_backup": package.manifest.get("retain_telegram_backup", False),
                "source_instance": source,
                "target_instance": target,
                "source_environment": package.manifest.get("source_environment"),
                "target_environment": target_kind,
                "sha256": digest,
                "package_bytes": size,
                "archive_member_count": len(package.names),
                "archive_unpacked_bytes": package.unpacked_bytes,
                "archive_workspace_bytes": package.workspace_bytes,
                "package_schema": package.manifest.get("schema_version"),
                "history_mode": package.manifest.get("history_mode"),
                "conversation_continuity_summary": package.manifest.get(
                    "conversation_continuity_summary"
                ),
                "status": "staged",
                "staged_at": utc_now_iso(),
                "credential_status": credential_status,
                "access_scope_plan": access_scope_plan,
                "retained_identity": retained_identity,
                "replaces_dormant_source": bool(dormant),
                "dormant_package_id": (
                    str((dormant or {}).get("transfer_package_id") or "") or None
                ),
                "warnings": _stage_warnings(
                    root,
                    package,
                    credential_status,
                    target_environment=target_kind,
                    access_scope_plan=access_scope_plan,
                    replaces_dormant_source=bool(dormant),
                ),
                "reboot_required": False,
            }
            if history_mapping_downgraded:
                # Governed target could not resolve a verified destination user.
                # Complete the Agent transfer without conversation history; the
                # history is never silently attributed to another user.
                record["history_mode"] = "none"
                record["conversation_history_downgraded"] = True
                record["conversation_continuity_summary"] = None
                record["conversation_owner_binding"] = None
                record["conversation_target_owner_id"] = None
                record["conversation_target_owner_origin"] = None
                warnings = list(record.get("warnings") or [])
                warnings.append(
                    "governed destination user unavailable; conversation history skipped"
                )
                record["warnings"] = warnings
            elif package.conversation_continuity is not None:
                # A local clone has the same authenticated user at both ends.
                # Never replace that principal with an unrelated legacy config.
                local_owner = None
                if source == target and transfer_operation == "clone":
                    local_owner = authenticated_conversation_command_owner(root)
                    if local_owner and local_owner != str(
                        package.conversation_continuity["owner_id"]
                    ).strip():
                        raise AgentMoveError(
                            "local clone history does not match the authenticated command owner"
                        )
                binding = _conversation_owner_binding(
                    root, package, source_instance=source,
                    target_instance=target, target_agent_id=selected_agent_id,
                    target_owner_id=local_owner or governed_target_owner,
                )
                record["conversation_owner_binding"] = asdict(binding)
                # This is transaction data, not a credential. The journal is
                # target-owned and already reached through the transfer boundary.
                record["conversation_target_owner_id"] = binding.target_owner_id
                if local_owner:
                    record["conversation_target_owner_origin"] = "authenticated_local"
                elif governed_target_owner:
                    record["conversation_target_owner_origin"] = "authenticated_governed"
            _atomic_json(record_path, record, mode=0o600)
            incomplete_record_dir = None
            return _public_state(record)
    except Exception:
        upload.unlink(missing_ok=True)
        if incomplete_record_dir is not None and incomplete_record_dir.exists():
            shutil.rmtree(incomplete_record_dir)
        raise


def commit_agent_move(
    hashi_root: Path | str,
    package_id: str,
    *,
    secret_passphrase: str | None = None,
    target_platform: str | None = None,
) -> dict[str, Any]:
    """Import a staged Agent in an inactive state.

    Configuration, schedules and credentials are changed under one receiver
    mutation lock.  Recovery snapshots make an interrupted commit retryable.
    """

    root = _root(hashi_root)
    with _mutation_lock(root):
        record_dir, record = _load_record(root, package_id)
        if record.get("status") in {
            "committed_inactive",
            "activated_pending_reboot",
            "completed",
            "target_cleanup_pending",
        }:
            return _public_state(record)
        if record.get("status") == "rolled_back":
            raise AgentMoveError(
                "rolled-back Agent move packages cannot be committed again"
            )
        if record.get("status") == "committing":
            if str(record.get("history_mode") or "none") not in {"none", "retain_source"}:
                _rollback_target_conversation_continuity(root, str(package_id))
            _restore_recovery_snapshots(root, record_dir)
            _recover_interrupted_workspace_commit(root, record_dir, record)
            record["status"] = "staged"
            _atomic_json(record_dir / "state.json", record, mode=0o600)
        if record.get("status") != "staged":
            raise AgentMoveError(
                f"Agent move is not staged (status={record.get('status')!r})"
            )

        package = read_agent_move_package(
            record_dir / "package.hashi-agent",
            verify=True,
            max_archive_members=_staged_archive_member_limit(record),
            max_unpacked_bytes=_staged_unpacked_byte_limit(record),
            max_workspace_bytes=_staged_workspace_byte_limit(record),
            target_platform=(
                target_platform
                or str(record.get("target_environment") or "")
                or detect_environment_kind()
            ),
        )
        if package.package_id != str(record.get("package_id")):
            raise AgentMoveError("staged Agent move state does not match its package")
        if package_sha256(record_dir / "package.hashi-agent") != str(
            record.get("sha256") or ""
        ).lower():
            raise AgentMoveError("staged Agent move package digest changed")
        operation = _record_operation(record)
        if operation != "legacy_move" and operation != str(
            package.manifest.get("operation") or ""
        ).lower():
            raise AgentMoveError("staged Agent transfer operation changed")
        target_agent_id = str(record.get("target_agent_id") or package.agent_id)
        if (record.get("desired_target_active", True) != package.manifest.get("desired_target_active", True)
                or record.get("retain_telegram_backup", False) != package.manifest.get("retain_telegram_backup", False)):
            raise AgentMoveError("staged target activation policy changed")
        continuity_binding = None
        if package.conversation_continuity is not None and not record.get(
            "conversation_history_downgraded"
        ):
            source_instance = _normalize_instance(record.get("source_instance"))
            target_instance = _normalize_instance(record.get("target_instance"))
            if source_instance != _normalize_instance(
                package.manifest.get("source_instance")
            ) or target_instance != _configured_instance_id(root):
                raise AgentMoveError("staged Agent move instance identity changed")
            saved_binding = record.get("conversation_owner_binding")
            staged_target_owner = record.get("conversation_target_owner_id")
            if staged_target_owner is None and isinstance(saved_binding, Mapping):
                # Older authenticated journals stored the mapping in one place.
                staged_target_owner = saved_binding.get("target_owner_id")
            if staged_target_owner is not None and (
                not isinstance(staged_target_owner, str) or not staged_target_owner.strip()
            ):
                raise AgentMoveError("staged conversation owner binding is invalid")
            if record.get("conversation_target_owner_origin") == "authenticated_local":
                if (operation != "clone" or source_instance != target_instance
                        or staged_target_owner != str(
                            package.conversation_continuity["owner_id"]
                        ).strip()):
                    raise AgentMoveError("staged local conversation owner binding changed")
                current_owner = authenticated_conversation_command_owner(
                    root, allow_expired_context=True,
                )
                if current_owner and current_owner != staged_target_owner:
                    raise AgentMoveError(
                        "authenticated command owner does not match the staged local clone"
                    )
            continuity_binding = _conversation_owner_binding(
                root, package, source_instance=source_instance,
                target_instance=target_instance, target_agent_id=target_agent_id,
                target_owner_id=staged_target_owner,
            )
            if saved_binding is not None and saved_binding != asdict(continuity_binding):
                raise AgentMoveError("staged conversation owner binding changed")
            if record.get("conversation_target_owner_origin") != "authenticated_local":
                # Both copies in a staged journal can be altered together.
                # Compare with the target's independent, stable local data
                # binding, not with the source owner or Telegram account.
                from orchestrator.personal_data_owner import read_personal_data_owner_for_config

                config = _load_json(root / "agents.json")
                global_config = config.get("global", {}) if isinstance(config, Mapping) else None
                if not isinstance(global_config, Mapping):
                    raise AgentMoveError("target deployment configuration is invalid")
                try:
                    from orchestrator.enterprise.profile import resolve_deployment_profile

                    profile = resolve_deployment_profile(dict(global_config)).value
                    local_owner = read_personal_data_owner_for_config(root, global_config)
                    if profile == "personal" and local_owner is None:
                        if saved_binding is not None:
                            raise AgentMoveError("staged conversation target data binding is missing")
                        # A pre-binding legacy journal may establish its target
                        # once; newer staged records must retain their binding.
                        local_owner = establish_personal_target_conversation_owner(root)
                except AgentMoveError:
                    raise
                except ValueError as exc:
                    raise AgentMoveError("target data binding is invalid") from exc
                if local_owner is not None and local_owner != continuity_binding.target_owner_id:
                    raise AgentMoveError("staged conversation target data binding changed")
            try:
                governed_owner = _require_governed_history_mapping(
                    root, package, local_authenticated=(
                        record.get("conversation_target_owner_origin") == "authenticated_local"
                        and operation == "clone" and source_instance == target_instance
                    ),
                )
            except GovernedHistoryMappingUnavailable:
                # Target became governed (or lost its verified destination user)
                # after staging. Do not silently attribute the history to the
                # staged personal owner; skip history and continue the transfer.
                governed_owner = None
                continuity_binding = None
                record["conversation_history_downgraded"] = True
                record["history_mode"] = "none"
                record["conversation_owner_binding"] = None
                record["conversation_target_owner_id"] = None
                record["conversation_target_owner_origin"] = None
                warnings = list(record.get("warnings") or [])
                warnings.append(
                    "governed destination user unavailable; conversation history skipped"
                )
                record["warnings"] = warnings
                _atomic_json(record_dir / "state.json", record, mode=0o600)
            if governed_owner is not None and (
                record.get("conversation_target_owner_origin") != "authenticated_governed"
                or governed_owner != staged_target_owner
            ):
                # The staged governed destination user must still resolve to the
                # exact staged target owner; a changed or stale identity is never
                # silently substituted for the commit.
                raise AgentMoveError("staged governed destination user changed")
            if saved_binding is None and continuity_binding is not None:
                # Older authenticated stage records predate this binding.
                # Freeze it only after the independent target binding succeeds.
                record["conversation_owner_binding"] = asdict(continuity_binding)
                record["conversation_target_owner_id"] = continuity_binding.target_owner_id
                _atomic_json(record_dir / "state.json", record, mode=0o600)
        dormant = (
            _assert_target_available(
                root,
                target_agent_id,
                source_instance=str(record.get("source_instance") or ""),
            )
            if operation == "legacy_move"
            else None
        )
        if bool(dormant) != bool(record.get("replaces_dormant_source")) or (
            dormant
            and str(dormant.get("transfer_package_id") or "")
            != str(record.get("dormant_package_id") or "")
        ):
            raise AgentMoveError(
                "target dormant Agent state changed after the move was staged"
            )
        source_secret_values = decrypt_agent_secrets(package, secret_passphrase)
        backup_clone = operation == "clone" and package.manifest.get("retain_telegram_backup") is True
        source_telegram_key = str(package.access_requirements.get("telegram_secret_key") or "")
        backup_token = source_secret_values.pop(source_telegram_key, None) if backup_clone else None
        secret_values, secret_key_mapping = _remap_agent_secrets(
            source_secret_values,
            source_agent_id=package.agent_id,
            target_agent_id=target_agent_id,
        )
        agents_path = root / "agents.json"
        agents_data = _load_json(agents_path)
        try:
            access_scope_plan = _access_scope_plan(root, package, target_config=agents_data)
        except InvalidTargetScopePolicy as exc:
            if record.get("access_scope_plan") is not None:
                raise AccessScopePreviewChanged(
                    "target access scope policy became invalid after preview; fix it and prepare again"
                ) from exc
            raise
        saved_scope_plan = record.get("access_scope_plan")
        if saved_scope_plan is None:
            # Older journals have no bound preview. Never introduce a restriction
            # that the original confirmation could not have observed.
            if access_scope_plan["effective"] != access_scope_plan["requested"]:
                raise AgentMoveError(
                    "staged transfer has no bound access scope preview; prepare it again"
                )
            record["access_scope_plan"] = access_scope_plan
            _atomic_json(record_dir / "state.json", record, mode=0o600)
        elif saved_scope_plan != access_scope_plan:
            raise AccessScopePreviewChanged(
                "target access scope policy changed after preview; prepare the transfer again"
            )
        imported_config, access_warning = _prepare_import_config(
            root,
            package,
            target_agent_id=target_agent_id,
            operation=operation,
            secret_key_mapping=secret_key_mapping,
            access_scope_plan=access_scope_plan,
        )
        lifecycle_id = str(
            imported_config.get(AGENT_LIFECYCLE_FIELD) or ""
        ).casefold()
        journal_lifecycle = str(
            record.get("imported_agent_lifecycle_id") or ""
        ).casefold()
        if journal_lifecycle:
            if not valid_agent_lifecycle_id(journal_lifecycle):
                raise AgentMoveError(
                    "Agent transfer journal lifecycle identity is malformed"
                )
            if operation != "clone" and lifecycle_id != journal_lifecycle:
                raise AgentMoveError(
                    "Agent transfer lifecycle identity changed across commit retry"
                )
            lifecycle_id = journal_lifecycle
            imported_config[AGENT_LIFECYCLE_FIELD] = lifecycle_id
        record["imported_agent_lifecycle_id"] = lifecycle_id
        if not journal_lifecycle:
            # A backup may be published before the rest of commit. Its
            # lifecycle must remain stable across an interrupted retry.
            _atomic_json(record_dir / "state.json", record, mode=0o600)

        tasks_path = root / "tasks.json"
        secrets_path = root / "secrets.json"
        capabilities_path = root / "agent_capabilities.json"
        tasks_data = _load_json_or_default(
            tasks_path,
            {"version": 1, "heartbeats": [], "crons": [], "nudges": []},
        )
        secrets_data = _load_json_or_default(secrets_path, {})
        if not isinstance(secrets_data, dict):
            raise AgentMoveError("target secrets.json must contain a JSON object")
        if operation == "clone":
            imported_config["telegram_token_key"] = _unbound_clone_telegram_key(
                agents_data, secrets_data, secret_values,
            )
        capabilities_data = _load_json_or_default(
            capabilities_path,
            {"agents": []},
        )

        if dormant:
            _remove_dormant_agent_config(
                agents_data,
                target_agent_id,
                str(record.get("dormant_package_id") or ""),
            )
            tasks_data = _remove_dormant_schedules(
                tasks_data,
                target_agent_id,
                str(record.get("dormant_package_id") or ""),
            )
        added_secret_keys, reused_secret_keys, replaced_secret_keys = (
            _merge_secrets_preview(
                secrets_data,
                secret_values,
                replace_conflicts_for_agent=(target_agent_id if dormant else None),
                preserve_backup_key=backup_clone and backup_token is not None,
            )
        )
        added_secret_hashes = {
            key: _secret_value_hash(secret_values[key]) for key in added_secret_keys
        }
        imported_task_ids, tasks_data = _merge_schedules(
            tasks_data,
            package.schedules,
            package_id=package.package_id,
            source_instance=str(record.get("source_instance") or ""),
            source_agent_id=package.agent_id,
            target_agent_id=target_agent_id,
        )
        imported_task_hashes = _imported_task_hashes(tasks_data, imported_task_ids)
        schedule_intent = _imported_schedule_intent(package.schedules, imported_task_ids)
        from orchestrator.personal_data_owner import read_personal_data_owner_for_config

        global_data = agents_data.get("global") if isinstance(agents_data, dict) else None
        if isinstance(global_data, dict):
            local_data_owner = read_personal_data_owner_for_config(root, global_data)
            if local_data_owner and not str(global_data.get("owner_scope_id") or "").strip():
                # A compatibility projection lets older generations locate the
                # same local data. It does not authorize the transfer.
                global_data["owner_scope_id"] = local_data_owner
            elif local_data_owner and str(global_data.get("owner_scope_id") or "").strip() != local_data_owner:
                warning = (
                    "Legacy local data projection differs; verify older-generation "
                    "history continuity before downgrade."
                )
                warnings = list(record.get("warnings") or [])
                if warning not in warnings:
                    warnings.append(warning)
                record["warnings"] = warnings
        agents_data = _append_agent_config(agents_data, imported_config)
        capabilities_data = _append_agent_capability(
            capabilities_data,
            package.agent_capability,
            target_agent_id=target_agent_id,
            package_id=package.package_id,
        )

        workspace_parent = root / "workspaces"
        workspace_parent.mkdir(parents=True, exist_ok=True)
        final_workspace = workspace_parent / target_agent_id
        staging_workspace = (
            workspace_parent
            / f".{target_agent_id}.agent-move-{package.package_id}.tmp"
        )
        if staging_workspace.exists():
            shutil.rmtree(staging_workspace)
        if final_workspace.exists() and not dormant:
            raise AgentMoveError(
                f"target workspace already exists for Agent '{target_agent_id}'"
            )
        if dormant and not final_workspace.is_dir():
            raise AgentMoveError("dormant target Agent workspace is no longer available")

        if backup_clone and backup_token is not None:
            record["retained_telegram_backup"] = _preserve_encrypted_clone_backup(
                root, record_dir, package, target_agent_id=target_agent_id,
                lifecycle_id=lifecycle_id, token=backup_token, secrets_data=secrets_data,
            )
        _write_recovery_snapshots(root, record_dir)
        record["status"] = "committing"
        record["commit_started_at"] = utc_now_iso()
        _atomic_json(record_dir / "state.json", record, mode=0o600)

        adopted_delivery_lifecycle: str | None = None
        try:
            if dormant:
                dormant_workspace = _dormant_workspace_backup(record_dir)
                if dormant_workspace.exists():
                    raise AgentMoveError(
                        "dormant target workspace recovery backup already exists"
                    )
                os.replace(final_workspace, dormant_workspace)
            extract_agent_workspace(
                package,
                staging_workspace,
                target_platform=(
                    target_platform
                    or str(record.get("target_environment") or "")
                    or detect_environment_kind()
                ),
            )
            os.replace(staging_workspace, final_workspace)
            _atomic_json(agents_path, agents_data)
            _record_recovery_publication(
                record_dir, "agents.json", agents_data.revision
            )
            _atomic_json(tasks_path, tasks_data)
            _record_recovery_publication(
                record_dir, "tasks.json", tasks_data.revision
            )
            if secret_values:
                secrets_data.update(secret_values)
                _atomic_json(secrets_path, secrets_data, mode=0o600)
                _record_recovery_publication(
                    record_dir, "secrets.json", secrets_data.revision
                )
            if package.agent_capability is not None:
                _atomic_json(capabilities_path, capabilities_data)
                _record_recovery_publication(
                    record_dir,
                    "agent_capabilities.json",
                    capabilities_data.revision,
                )

            if operation == "clone":
                delivery_receipt = retire_agent_state(
                    root,
                    target_agent_id,
                    reason="clone_starts_without_source_delivery_state",
                )
                delivery_result = {
                    "imported": False,
                    "operation": "clone",
                    "quarantined_existing": bool(delivery_receipt),
                }
            else:
                token_key = str(imported_config.get("telegram_token_key") or "")
                bot_fingerprint = telegram_bot_fingerprint(
                    secret_values.get(token_key) or secrets_data.get(token_key)
                )
                transferred_delivery = package.access_requirements.get(
                    "telegram_delivery_state"
                )
                if transferred_delivery is not None and not bot_fingerprint:
                    raise AgentMoveError(
                        "owned Telegram delivery state cannot be adopted until "
                        "the transferred Bot credential is available"
                    )
                if bot_fingerprint:
                    delivery_result = adopt_transferred_state(
                        root,
                        target_agent_id,
                        target_owner={
                            "instance_id": _configured_instance_id(root),
                            "agent_lifecycle_id": lifecycle_id,
                            "telegram_bot_fingerprint": bot_fingerprint,
                        },
                        transferred_record=(
                            transferred_delivery
                            if isinstance(transferred_delivery, Mapping)
                            else None
                        ),
                        operation="move",
                    )
                    adopted_delivery_lifecycle = lifecycle_id
                else:
                    delivery_result = {
                        "imported": False,
                        "operation": "move",
                        "reason": "no_telegram_bot_configured",
                    }

            continuity_result: dict[str, Any]
            if package.conversation_continuity is not None and continuity_binding is not None:
                try:
                    continuity_result = _session_store(root).import_conversation_continuity(
                        package.conversation_continuity,
                        owner_id=continuity_binding.target_owner_id,
                        agent_id=target_agent_id,
                        transfer_id=package.package_id,
                        history_mode=str(package.manifest.get("history_mode") or ""),
                        owner_binding=continuity_binding,
                    )
                except (SessionConflict, ValueError) as exc:
                    raise AgentMoveError(
                        f"conversation continuity import failed: {exc}"
                    ) from exc
            else:
                continuity_result = {
                    "transfer_id": package.package_id,
                    "history_mode": str(
                        record.get("history_mode")
                        or package.manifest.get("history_mode")
                        or "none"
                    ),
                    "target_session_ids": [],
                    "created_session_ids": [],
                    "imported_messages": 0,
                    "replayed": False,
                }

            credential_status = _credential_status(
                root,
                package,
                secret_passphrase,
                target_agent_id=target_agent_id,
            )
            warnings = list(record.get("warnings") or [])
            if access_warning and access_warning not in warnings:
                warnings.append(access_warning)
            record.update(
                {
                    "status": "committed_inactive",
                    "committed_at": utc_now_iso(),
                    "workspace": f"workspaces/{target_agent_id}",
                    "imported_task_ids": imported_task_ids,
                    "imported_task_hashes": imported_task_hashes,
                    "schedule_intent": schedule_intent,
                    "imported_config_hash": _agent_config_hash(imported_config),
                    "imported_secret_hashes": {
                        key: _secret_value_hash(value)
                        for key, value in secret_values.items()
                    },
                    "added_secret_keys": added_secret_keys,
                    "added_secret_hashes": added_secret_hashes,
                    "reused_secret_keys": reused_secret_keys,
                    "replaced_secret_keys": replaced_secret_keys,
                    "secret_key_mapping": secret_key_mapping,
                    "imported_required_secret_keys": list(
                        credential_status.get("required_keys") or []
                    ),
                    "credential_status": credential_status,
                    "telegram_delivery_state": delivery_result,
                    "conversation_continuity": continuity_result,
                    "warnings": warnings,
                    "reboot_required": False,
                }
            )
            _atomic_json(record_dir / "state.json", record, mode=0o600)
            return _public_state(record)
        except Exception as exc:
            if adopted_delivery_lifecycle:
                try:
                    retire_agent_state(
                        root,
                        target_agent_id,
                        lifecycle_id=adopted_delivery_lifecycle,
                        reason="agent_transfer_commit_rolled_back",
                    )
                except Exception:
                    pass
            compensation_errors: list[str] = []
            if package.conversation_continuity is not None:
                try:
                    _rollback_target_conversation_continuity(
                        root,
                        package.package_id,
                    )
                except Exception as compensation_exc:
                    compensation_errors.append(
                        "conversation continuity rollback failed: "
                        f"{type(compensation_exc).__name__}: {compensation_exc}"
                    )
            # Configuration persistence conflicts are durability boundaries,
            # not best-effort cleanup. Let either recovery helper surface its
            # classified error and leave the durable journal in ``committing``.
            _restore_recovery_snapshots(root, record_dir)
            _recover_interrupted_workspace_commit(root, record_dir, record)
            record["status"] = "staged"
            record["last_error"] = str(exc)
            if compensation_errors:
                record["compensation_errors"] = compensation_errors
            else:
                record.pop("compensation_errors", None)
            _atomic_json(record_dir / "state.json", record, mode=0o600)
            raise


def activate_agent_move(hashi_root: Path | str, package_id: str) -> dict[str, Any]:
    """Mark a committed Agent active in target config (effective after reboot)."""

    root = _root(hashi_root)
    with _mutation_lock(root):
        record_dir, record = _load_record(root, package_id)
        if record.get("status") in {
            "activated_pending_reboot",
            "completed",
            "target_cleanup_pending",
        }:
            return _public_state(record)
        if record.get("status") != "committed_inactive":
            raise AgentMoveError(
                f"Agent move must be committed inactive before activation (status={record.get('status')!r})"
            )
        if record.get("desired_target_active") is False:
            raise AgentMoveError("this transfer selected an inactive target")
        package = read_agent_move_package(
            record_dir / "package.hashi-agent", verify=True,
            max_archive_members=_staged_archive_member_limit(record),
            max_unpacked_bytes=_staged_unpacked_byte_limit(record),
            max_workspace_bytes=_staged_workspace_byte_limit(record),
        )
        if package.manifest.get("desired_target_active") is False:
            raise AgentMoveError("this transfer selected an inactive target")
        target_agent_id = str(record.get("target_agent_id") or package.agent_id)
        credentials = dict(record.get("credential_status") or {})

        path = root / "agents.json"
        original = path.read_bytes()
        data = _load_json(path)
        original_revision = data.revision
        row = _find_owned_agent(data, target_agent_id, package.package_id)
        row["is_active"] = True
        row["transfer_import_state"] = "activated_pending_reboot"
        try:
            _atomic_json(path, data)
            record.update(
                {
                    "status": "activated_pending_reboot",
                    "activated_at": utc_now_iso(),
                    "credential_status": credentials,
                    "reboot_required": True,
                }
            )
            _atomic_json(record_dir / "state.json", record, mode=0o600)
        except Exception:
            if data.revision != original_revision:
                _restore_json_snapshot(
                    path,
                    original,
                    published_revision=data.revision,
                )
            raise
        return _public_state(record)


def finalize_agent_move(
    hashi_root: Path | str,
    package_id: str,
    *,
    runtime_online: bool | None,
) -> dict[str, Any]:
    """Finish an already-started target without revalidating mutable state.

    Package integrity, compatibility, credentials and importability are
    decided before activation.  Once activation starts, normal runtime writes
    are not a second veto.  ``runtime_online`` is only the receipt that the
    forward start step has completed; false remains retryable.
    """

    root = _root(hashi_root)
    with _mutation_lock(root):
        record_dir, record = _load_record(root, package_id)
        if record.get("status") == "completed":
            return _public_state(record)
        inactive_target = record.get("desired_target_active") is False
        if record.get("status") not in {
            "activated_pending_reboot",
            "target_finalizing",
            "target_cleanup_pending",
        } and not (inactive_target and record.get("status") == "committed_inactive"):
            raise AgentMoveError(
                "Agent transfer target must be active before final verification "
                f"(status={record.get('status')!r})"
            )
        package_path = record_dir / "package.hashi-agent"
        if not record.get("target_verified"):
            if inactive_target:
                if runtime_online is not False:
                    raise AgentMoveError("inactive target runtime is online or its status is unknown")
                target_agent_id = str(record.get("target_agent_id") or record.get("agent_id") or "")
                row = _find_owned_agent(_load_json(root / "agents.json"), target_agent_id, package_id)
                if row.get("is_active") is not False or row.get("transfer_import_state") != "committed_inactive":
                    raise AgentMoveError("target Agent is not the committed inactive transfer")
                verification = {"runtime_online": False, "runtime_present": False,
                                "presence_checked": True, "desired_active": False,
                                "config_inactive": True}
                credential_status = record.get("credential_status")
                if isinstance(credential_status, Mapping) and credential_status.get("telegram_backup_quarantined"):
                    # Do not discard the transport package or claim a backup
                    # until the target-encrypted credential can be recovered.
                    _recover_quarantined_telegram_token_locked(root, package_id)
                    verification["telegram_backup_recoverable"] = True
            elif runtime_online is not True:
                raise AgentMoveError(
                    "target Agent is not online in the Workbench/API runtime"
                )
            else:
                verification = {"runtime_online": True}
            record["target_verified"] = True
            record["target_verification"] = verification
            record["verified_at"] = utc_now_iso()

        record["status"] = "target_finalizing"
        record.setdefault("finalize_started_at", utc_now_iso())
        _atomic_json(record_dir / "state.json", record, mode=0o600)
        try:
            _strip_target_transaction_markers(root, record)
            for payload_path in (
                package_path,
                record_dir / "recovery",
                record_dir / "retained-identity",
            ):
                _remove_path(payload_path)
        except Exception as exc:
            record["status"] = "target_cleanup_pending"
            record["last_error"] = str(exc)
            _atomic_json(record_dir / "state.json", record, mode=0o600)
            raise

        record.update(
            {
                "status": "completed",
                "completed_at": utc_now_iso(),
                "reboot_required": False,
                "rollback_payload_retained": False,
            }
        )
        for key in (
            "last_error",
            "added_secret_hashes",
            "imported_config_hash",
            "imported_secret_hashes",
            "imported_task_hashes",
            "secret_key_mapping",
        ):
            record.pop(key, None)
        _atomic_json(record_dir / "state.json", record, mode=0o600)
        return _public_state(record)


def rollback_agent_move(
    hashi_root: Path | str, package_id: str, *, staged_only: bool = False,
) -> dict[str, Any]:
    """Remove an imported target config and quarantine its workspace."""

    root = _root(hashi_root)
    with _mutation_lock(root):
        record_dir, record = _load_record(root, package_id)
        status = str(record.get("status") or "")
        if status == "rolled_back":
            if staged_only and record.get("staged_abort") is not True:
                raise AgentMoveError("target was not rolled back by the staged-only abort")
            return _public_state(record)
        if staged_only and status != "staged":
            raise AgentMoveError("staged-only abort cannot touch a committed or active target")
        if status == "committing":
            if str(record.get("history_mode") or "none") not in {"none", "retain_source"}:
                _rollback_target_conversation_continuity(root, str(package_id))
            _restore_recovery_snapshots(root, record_dir)
            _recover_interrupted_workspace_commit(root, record_dir, record)
        elif status in {
            "committed_inactive",
            "activated_pending_reboot",
            "rolling_back",
        }:
            if status != "rolling_back":
                _find_owned_agent(
                    _load_json(root / "agents.json"),
                    str(record.get("target_agent_id") or record.get("agent_id") or ""),
                    str(record.get("package_id") or ""),
                )
                record["rollback_agent_config_verified"] = True
            elif not record.get("rollback_agent_config_verified"):
                raise AgentMoveError("rollback ownership journal is incomplete")
            record["rollback_requires_reboot"] = bool(
                record.get("rollback_requires_reboot")
                or status == "activated_pending_reboot"
            )
            record["status"] = "rolling_back"
            record.setdefault("rollback_started_at", utc_now_iso())
            _atomic_json(record_dir / "state.json", record, mode=0o600)
            _rollback_target_conversation_continuity(root, str(package_id))
            if record.get("replaces_dormant_source"):
                _restore_recovery_snapshots(root, record_dir)
                _recover_interrupted_workspace_commit(root, record_dir, record)
            else:
                _surgical_target_rollback(root, record)
        elif status != "staged":
            raise AgentMoveError(
                f"Agent move cannot be rolled back from status {status!r}"
            )
        elif str(record.get("history_mode") or "none") not in {"none", "retain_source"}:
            # A prior compensation failure can leave a fully journaled import
            # behind while the config/workspace transaction safely returns to
            # staged. Explicit rollback must still retire that import.
            _rollback_target_conversation_continuity(root, str(package_id))

        if record.get("imported_agent_lifecycle_id"):
            retire_agent_state(
                root,
                str(record.get("target_agent_id") or record.get("agent_id") or ""),
                lifecycle_id=record.get("imported_agent_lifecycle_id"),
                reason="agent_transfer_rolled_back",
            )
        if staged_only:
            record["staged_abort"] = True
        record.update(
            {
                "status": "rolled_back",
                "rolled_back_at": utc_now_iso(),
                "reboot_required": bool(record.get("rollback_requires_reboot")),
            }
        )
        if isinstance(record.get("retained_identity"), Mapping):
            retained = dict(record["retained_identity"])
            retained.update(
                {
                    "status": "retained_after_rollback",
                    "retained_after_rollback_at": utc_now_iso(),
                }
            )
            record["retained_identity"] = retained
        _atomic_json(record_dir / "state.json", record, mode=0o600)
        return _public_state(record)


def abort_staged_agent_move(hashi_root: Path | str, package_id: str) -> dict[str, Any]:
    """Atomically abort only an unpublished staged target; safe to replay."""
    return rollback_agent_move(hashi_root, package_id, staged_only=True)


def get_agent_move_status(hashi_root: Path | str, package_id: str) -> dict[str, Any]:
    _, record = _load_record(_root(hashi_root), package_id)
    return _public_state(record)


def deactivate_source_agent(
    hashi_root: Path | str,
    agent_id: str,
    package_id: str,
    *,
    target_instance: str,
    history_mode: str | None = None,
    history_source_owner_id: str | None = None,
) -> dict[str, Any]:
    """Disable the source config while retaining its complete workspace."""

    root = _root(hashi_root)
    normalized_history_mode = str(history_mode or "none").strip().lower()
    if normalized_history_mode not in {"none", "move", "retain_source"}:
        raise AgentMoveError("source Move conversation history mode is invalid")
    with _mutation_lock(root):
        state_path = _source_state_path(root, package_id)
        state: dict[str, Any] | None = None
        if state_path.exists():
            state = _load_json(state_path)
            journal_history_mode = str(
                state.get("history_mode") or "none"
            ).strip().lower()
            if journal_history_mode != normalized_history_mode:
                raise AgentMoveError(
                    "source move conversation history mode conflicts with this move"
                )
            frozen_owner = str(state.get("history_source_owner_id") or "")
            if history_source_owner_id and frozen_owner and (
                str(history_source_owner_id) != frozen_owner
            ):
                raise AgentMoveError("source move conversation owner conflicts with its journal")
            if history_source_owner_id and not frozen_owner and normalized_history_mode == "move":
                state["history_source_owner_id"] = str(history_source_owner_id).strip()
                _atomic_json(state_path, state, mode=0o600)
            if state.get("status") == "source_disabled_pending_reboot":
                return state
            if (
                state.get("status") != "source_disabling"
                or state.get("agent_id") != agent_id
                or _normalize_instance(state.get("target_instance"))
                != _normalize_instance(target_instance)
            ):
                raise AgentMoveError("source move state conflicts with this move")

        agents_path = root / "agents.json"
        tasks_path = root / "tasks.json"
        agents_original = agents_path.read_bytes()
        tasks_original = tasks_path.read_bytes() if tasks_path.exists() else None
        agents = _load_json(agents_path)
        rows = _agent_rows(agents)
        row = next(
            (
                item
                for item in rows
                if str(item.get("name") or item.get("id")) == agent_id
            ),
            None,
        )
        if row is None:
            raise AgentMoveError(f"source Agent '{agent_id}' was not found")
        source_lifecycle_id = str(
            row.get(AGENT_LIFECYCLE_FIELD) or ""
        ).strip().casefold()
        if not source_lifecycle_id:
            source_lifecycle_id = new_agent_lifecycle_id()
            row[AGENT_LIFECYCLE_FIELD] = source_lifecycle_id
        if not valid_agent_lifecycle_id(source_lifecycle_id):
            raise AgentMoveError("source Agent lifecycle identity is malformed")
        journal_lifecycle = str(
            (state or {}).get("source_agent_lifecycle_id") or ""
        ).strip().casefold()
        if journal_lifecycle and journal_lifecycle != source_lifecycle_id:
            raise AgentMoveError(
                "source Agent lifecycle identity changed during move"
            )
        current_owner = row.get("transfer_package_id")
        previous_transfer_fields = {
            key: row[key] for key in _SOURCE_TRANSFER_FIELDS if key in row
        }
        if state is not None:
            previous_transfer_fields = {
                key: value
                for key, value in dict(
                    state.get("previous_transfer_fields") or {}
                ).items()
                if key in _SOURCE_TRANSFER_FIELDS
            }
        if current_owner not in {None, package_id}:
            previous_owner = previous_transfer_fields.get("transfer_package_id")
            imported_record = (
                _move_root(root)
                / "incoming"
                / str(current_owner)
                / "state.json"
            )
            imported_state = (
                _load_json(imported_record) if imported_record.is_file() else {}
            )
            is_completed_import = (
                str(previous_owner or "") == str(current_owner)
                and not row.get("transfer_state")
                and row.get("transfer_import_state")
                in {"committed_inactive", "activated_pending_reboot"}
                and str(imported_state.get("package_id") or "")
                == str(current_owner)
                and str(imported_state.get("agent_id") or "") == agent_id
                and imported_state.get("status")
                in {"committed_inactive", "activated_pending_reboot"}
            )
            if not is_completed_import:
                raise AgentMoveError(
                    "source Agent is already associated with another move"
                )

        tasks = _load_json_or_default(
            tasks_path,
            {"version": 1, "heartbeats": [], "crons": [], "nudges": []},
        )
        if not isinstance(tasks, dict):
            raise AgentMoveError("source tasks.json must contain a JSON object")
        if state is None:
            schedule_states: dict[str, bool] = {}
            schedule_enabled_present: dict[str, bool] = {}
            for section in ("heartbeats", "crons", "nudges"):
                for index, item in enumerate(tasks.get(section, [])):
                    if isinstance(item, dict) and item.get("agent") == agent_id:
                        key = _schedule_state_key(section, item, index)
                        schedule_states[key] = bool(item.get("enabled", False))
                        schedule_enabled_present[key] = "enabled" in item
            state = new_config_json(state_path, {
                "schema_version": MOVE_STATE_SCHEMA_VERSION,
                "package_id": package_id,
                "agent_id": agent_id,
                "source_agent_lifecycle_id": source_lifecycle_id,
                "target_instance": _normalize_instance(target_instance),
                "history_mode": normalized_history_mode,
                "history_source_owner_id": (
                    str(history_source_owner_id).strip()
                    if normalized_history_mode == "move" and history_source_owner_id
                    else None
                ),
                "previous_active": bool(row.get("is_active", True)),
                "previous_transfer_fields": previous_transfer_fields,
                "schedule_states": schedule_states,
                "schedule_enabled_present": schedule_enabled_present,
                "status": "source_disabling",
                "disable_started_at": utc_now_iso(),
                "workspace_retained": True,
                "workspace_path": str(_source_workspace_path(root, row, agent_id)),
                "reboot_required": False,
            })
            _atomic_json(state_path, state, mode=0o600)
        elif not journal_lifecycle:
            state["source_agent_lifecycle_id"] = source_lifecycle_id
            _atomic_json(state_path, state, mode=0o600)

        for key in _SOURCE_TRANSFER_FIELDS:
            row.pop(key, None)
        row["is_active"] = False
        row["transfer_state"] = "moved_out_pending_reboot"
        row["transfer_package_id"] = package_id
        row["transfer_target"] = _normalize_instance(target_instance)
        for section in ("heartbeats", "crons", "nudges"):
            for item in tasks.get(section, []):
                if isinstance(item, dict) and item.get("agent") == agent_id:
                    item["enabled"] = False
                    item["transfer_disabled_by"] = package_id

        try:
            agents_published = False
            tasks_published = False
            _atomic_json(agents_path, agents)
            agents_published = True
            _atomic_json(tasks_path, tasks)
            tasks_published = True
            state["status"] = "source_disabled_pending_reboot"
            state["disabled_at"] = utc_now_iso()
            state["reboot_required"] = True
            _atomic_json(state_path, state, mode=0o600)
        except Exception:
            if tasks_published:
                _restore_json_snapshot(
                    tasks_path,
                    tasks_original,
                    published_revision=tasks.revision,
                )
            if agents_published:
                _restore_json_snapshot(
                    agents_path,
                    agents_original,
                    published_revision=agents.revision,
                )
            raise
        return state


def restore_source_agent(hashi_root: Path | str, package_id: str) -> dict[str, Any]:
    """Undo source deactivation after a failed target activation."""

    root = _root(hashi_root)
    with _mutation_lock(root):
        state_path = _source_state_path(root, package_id)
        if not state_path.exists():
            raise AgentMoveError("source move state was not found")
        state = _load_json(state_path)
        if state.get("status") == "source_restored_pending_reboot":
            return state
        if state.get("status") not in {
            "source_disabled_pending_reboot",
            "source_restoring",
        }:
            raise AgentMoveError(
                f"source Agent cannot be restored from status {state.get('status')!r}"
            )

        agents_path = root / "agents.json"
        tasks_path = root / "tasks.json"
        agents = _load_json(agents_path)
        row = next(
            (
                item
                for item in _agent_rows(agents)
                if str(item.get("name") or item.get("id")) == str(state.get("agent_id"))
            ),
            None,
        )
        if row is None:
            raise AgentMoveError("source Agent config no longer exists")
        owner = row.get("transfer_package_id")
        previous_transfer_fields = {
            key: value
            for key, value in dict(
                state.get("previous_transfer_fields") or {}
            ).items()
            if key in _SOURCE_TRANSFER_FIELDS
        }
        already_restored = (
            bool(row.get("is_active", True))
            == bool(state.get("previous_active", True))
            and all(
                (key in row) == (key in previous_transfer_fields)
                and row.get(key) == previous_transfer_fields.get(key)
                for key in _SOURCE_TRANSFER_FIELDS
            )
        )
        if owner == package_id:
            row["is_active"] = bool(state.get("previous_active", True))
            for key in _SOURCE_TRANSFER_FIELDS:
                row.pop(key, None)
            row.update(previous_transfer_fields)
        elif state.get("status") == "source_restoring" and already_restored:
            pass
        else:
            raise AgentMoveError("source Agent config no longer belongs to this move")

        tasks = _load_json_or_default(
            tasks_path,
            {"version": 1, "heartbeats": [], "crons": [], "nudges": []},
        )
        if not isinstance(tasks, dict):
            raise AgentMoveError("source tasks.json must contain a JSON object")
        schedule_states = dict(state.get("schedule_states") or {})
        schedule_enabled_present = dict(state.get("schedule_enabled_present") or {})
        for section in ("heartbeats", "crons", "nudges"):
            for index, item in enumerate(tasks.get(section, [])):
                key = _schedule_state_key(section, item, index)
                if (
                    isinstance(item, dict)
                    and item.get("transfer_disabled_by") == package_id
                ):
                    if key in schedule_enabled_present and not schedule_enabled_present[key]:
                        item.pop("enabled", None)
                    else:
                        item["enabled"] = bool(schedule_states.get(key, False))
                    item.pop("transfer_disabled_by", None)

        if state.get("status") != "source_restoring":
            state["status"] = "source_restoring"
            state["restore_started_at"] = utc_now_iso()
            _atomic_json(state_path, state, mode=0o600)
        _atomic_json(agents_path, agents)
        _atomic_json(tasks_path, tasks)
        state["status"] = "source_restored_pending_reboot"
        state["restored_at"] = utc_now_iso()
        state["reboot_required"] = True
        _atomic_json(state_path, state, mode=0o600)
        return state


def cleanup_source_agent(
    hashi_root: Path | str,
    package_id: str,
    *,
    source_secret_keys: list[str] | tuple[str, ...],
    target_instance: str,
    target_agent_id: str,
    source_secret_snapshot: Mapping[str, Any] | None = None,
    source_schedule_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Remove only source data proven transferred; retain unproven schedules."""

    root = _root(hashi_root)
    with _mutation_lock(root):
        state_path = _source_state_path(root, package_id)
        if not state_path.is_file():
            raise AgentMoveError("source move state was not found")
        state = _load_json(state_path)
        if state.get("status") == "source_cleaned":
            return state
        if state.get("status") not in {
            "source_disabled_pending_reboot",
            "source_cleanup_in_progress",
            "source_cleanup_pending",
        }:
            raise AgentMoveError(
                f"source Agent cannot be cleaned from status {state.get('status')!r}"
            )

        agent_id = str(state.get("agent_id") or "")
        if not agent_id:
            raise AgentMoveError("source cleanup journal has no Agent ID")
        history_mode = str(state.get("history_mode") or "none").strip().lower()
        if history_mode not in {"none", "move", "retain_source"}:
            raise AgentMoveError("source cleanup conversation history mode is invalid")
        # Resolve the configured owner before publishing any destructive source
        # cleanup. An absent owner is a hard provenance boundary, not a reason to
        # archive an inferred/default user's Sessions after config removal.
        history_owner = (
            str(state.get("history_source_owner_id") or "")
            or configured_conversation_owner_id(root, agent_id=agent_id)
            if history_mode == "move" else None
        )
        agents_path = root / "agents.json"
        tasks_path = root / "tasks.json"
        secrets_path = root / "secrets.json"
        capabilities_path = root / "agent_capabilities.json"
        agents = _load_json(agents_path)
        rows = _agent_rows(agents)
        matches = [
            row
            for row in rows
            if str(row.get("name") or row.get("id") or "") == agent_id
        ]
        if len(matches) > 1:
            raise AgentMoveError("source Agent config is not unique during cleanup")
        if matches:
            row = matches[0]
            source_lifecycle_id = str(
                row.get(AGENT_LIFECYCLE_FIELD) or ""
            ).strip().casefold()
            journal_lifecycle = str(
                state.get("source_agent_lifecycle_id") or ""
            ).strip().casefold()
            if journal_lifecycle and source_lifecycle_id != journal_lifecycle:
                raise AgentMoveError(
                    "source Agent lifecycle identity changed before cleanup"
                )
            if (
                row.get("is_active") is not False
                or row.get("transfer_state") != "moved_out_pending_reboot"
                or row.get("transfer_package_id") != package_id
            ):
                raise AgentMoveError(
                    "source Agent config changed before verified move cleanup"
                )
            rows.remove(row)
        else:
            source_lifecycle_id = str(
                state.get("source_agent_lifecycle_id") or ""
            ).strip().casefold()

        tasks = _load_json_or_default(
            tasks_path,
            {"version": 1, "heartbeats": [], "crons": [], "nudges": []},
        )
        if not isinstance(tasks, dict):
            raise AgentMoveError("source tasks.json must contain a JSON object")
        preserved_schedules = _separate_source_schedules(
            tasks, state, source_schedule_snapshot, agent_id=agent_id, package_id=package_id,
        )

        secrets_data = _load_json_or_default(secrets_path, {})
        if not isinstance(secrets_data, dict):
            raise AgentMoveError("source secrets.json must contain a JSON object")
        preserved_secret_keys: list[str] = []
        for raw_key in source_secret_keys:
            key = str(raw_key)
            if not key or key not in secrets_data:
                continue
            # A successfully imported package proves only the values it actually
            # contained. Keep omitted or subsequently changed source credentials.
            if (
                source_secret_snapshot is None
                or key not in source_secret_snapshot
                or source_secret_snapshot[key] != secrets_data[key]
            ):
                preserved_secret_keys.append(key)
                continue
            if _value_references_secret(agents, key):
                preserved_secret_keys.append(key)
                continue
            secrets_data.pop(key, None)

        AgentDirectory.remove_local_memberships(agents, agent_id)
        capabilities = _load_json_or_default(capabilities_path, {"agents": []})
        _remove_source_agent_capability(capabilities, agent_id)

        state["preserved_source_secret_keys"] = sorted(
            set(state.get("preserved_source_secret_keys") or ()) | set(preserved_secret_keys)
        )
        previous_preserved = state.get("preserved_source_schedules") or {}
        merged_preserved = {}
        for section in ("heartbeats", "crons", "nudges"):
            previous = list(previous_preserved.get(section) or [])
            known = {_secret_value_hash(item) for item in previous}
            for item in preserved_schedules[section]:
                digest = _secret_value_hash(item)
                if digest not in known:
                    previous.append(item)
                    known.add(digest)
            merged_preserved[section] = previous
        state["preserved_source_schedules"] = merged_preserved
        state["status"] = "source_cleanup_in_progress"
        state.setdefault("cleanup_started_at", utc_now_iso())
        _atomic_json(state_path, state, mode=0o600)
        try:
            if history_mode == "move":
                history_retirement = _session_store(
                    root
                ).archive_agent_conversation_sessions(
                    owner_id=str(history_owner),
                    agent_id=agent_id,
                    transfer_id=package_id,
                )
            elif history_mode == "retain_source":
                # Retain every owner's records on this instance but archive and
                # unbind them so a future same-name Agent starts independently.
                history_retirement = _session_store(root).archive_retained_agent_sessions(
                    agent_id=agent_id, transfer_id=package_id,
                )
            else:
                history_retirement = {
                    "transfer_id": package_id,
                    "session_ids": [],
                    "replayed": False,
                    "skipped": True,
                }
            state["conversation_history_retirement"] = history_retirement
            _atomic_json(agents_path, agents)
            _atomic_json(tasks_path, tasks)
            if secrets_path.exists() or source_secret_keys:
                _atomic_json(secrets_path, secrets_data, mode=0o600)
            if capabilities_path.exists():
                _atomic_json(capabilities_path, capabilities)
            _preserve_source_workspace(root, state, state_path)
            _record_moved_agent(
                root,
                agent_id=agent_id,
                package_id=package_id,
                target_instance=target_instance,
                target_agent_id=target_agent_id,
                agent_lifecycle_id=source_lifecycle_id,
            )
            if valid_agent_lifecycle_id(source_lifecycle_id):
                def preserve_delivery_record(record: dict[str, Any]) -> None:
                    chats = record.get("per_chat") or {}
                    pending = {
                        str(request_id)
                        for chat in chats.values() if isinstance(chat, dict)
                        for request_id in chat.get("undelivered_request_ids", [])
                        if request_id
                    } if isinstance(chats, dict) else set()
                    state["retained_source_delivery"] = {
                        "record": record,
                        "pending_count": len(pending),
                        "workspace_path": state.get("retained_source_workspace"),
                        "workspace_status": (state.get("workspace_retention") or {}).get("status"),
                        "retained_at": utc_now_iso(),
                        "package_id": package_id,
                    }
                    _atomic_json(state_path, state, mode=0o600)

                retire_agent_state(
                    root,
                    agent_id,
                    lifecycle_id=source_lifecycle_id,
                    reason="verified_move_source_cleaned",
                    before_retire=preserve_delivery_record,
                )
        except Exception as exc:
            state["status"] = "source_cleanup_pending"
            state["last_error"] = str(exc)
            _atomic_json(state_path, state, mode=0o600)
            raise

        state.update(
            {
                "status": "source_cleaned",
                "cleaned_at": utc_now_iso(),
                "workspace_retained": bool(state.get("retained_source_workspace")),
                "reboot_required": False,
            }
        )
        for key in (
            "last_error",
            "previous_transfer_fields",
            "schedule_states",
            "workspace_path",
        ):
            state.pop(key, None)
        _atomic_json(state_path, state, mode=0o600)
        return state


def completed_agent_departures(hashi_root: Path | str) -> dict[str, Any]:
    """Project positive, source-bound retirement facts, never absence from a roster."""
    root = _root(hashi_root)
    source = _configured_instance_id(root)
    audit = _load_json_or_default(root / "state" / "agent_moves" / "moved_agents.json", {})
    events = []
    records = audit.get("agents", {}) if isinstance(audit, dict) else {}
    history = audit.get("departures", {}) if isinstance(audit, dict) else {}
    candidates = [
        *(records.values() if isinstance(records, dict) else []),
        *(history.values() if isinstance(history, dict) else []),
    ]
    seen = set()
    for record in candidates:
        if not isinstance(record, dict) or record.get("source_instance_id") != source:
            continue
        lifecycle = str(record.get("agent_lifecycle_id") or "").strip().casefold()
        package_id = str(record.get("package_id") or "")
        agent_id = str(record.get("agent_id") or "")
        if (package_id in seen or not valid_agent_lifecycle_id(lifecycle) or not agent_id or not package_id
                or len(package_id) > 128
                or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
                       for char in package_id)):
            continue
        try:
            state = _load_json(root / "state" / "agent_moves" / "source" / f"{package_id}.json")
        except (AgentMoveError, OSError):
            continue
        if (not isinstance(state, dict) or state.get("status") != "source_cleaned"
                or state.get("package_id") != package_id
                or state.get("agent_id") != agent_id
                or state.get("source_agent_lifecycle_id") != lifecycle
                or state.get("target_instance") != record.get("target_instance")
                or not state.get("cleaned_at")):
            continue
        seen.add(package_id)
        events.append({
            "package_id": package_id,
            "agent_id": agent_id,
            "agent_lifecycle_id": lifecycle,
            "state": "source_cleaned",
            "completed_at": state["cleaned_at"],
        })
    projection = {"schema_version": 1, "source_instance_id": source, "events": events}
    returns = _completed_agent_returns(root, source, events)
    if returns:
        projection["returns"] = returns
    return projection


def _completed_agent_returns(root: Path, source: str, departures: list[dict]) -> list[dict]:
    """A completed incoming Move can permit a new assignment, never revive an old one."""
    latest = {}
    for event in departures:
        try:
            completed = datetime.fromisoformat(event["completed_at"])
            if completed.tzinfo is None:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        lifecycle = event["agent_lifecycle_id"]
        latest[lifecycle] = max(latest.get(lifecycle, completed), completed)
    if not latest:
        return []
    current = {
        (str(row.get("name") or row.get("id") or ""), row.get(AGENT_LIFECYCLE_FIELD))
        for row in _agent_rows(_load_json(root / "agents.json"))
        if valid_agent_lifecycle_id(row.get(AGENT_LIFECYCLE_FIELD))
        and row[AGENT_LIFECYCLE_FIELD] in latest
    }
    result = []
    for path in (root / "state" / "agent_moves" / "incoming").glob("*/state.json"):
        try:
            receipt = _load_json(path)
            if not isinstance(receipt, dict) or not isinstance(receipt.get("target_verification"), dict):
                continue
            lifecycle = receipt.get("imported_agent_lifecycle_id")
            agent_id = receipt.get("target_agent_id") or receipt.get("agent_id")
            if (receipt.get("status") != "completed" or receipt.get("target_verified") is not True
                    or not (receipt.get("target_verification", {}).get("runtime_online") is True
                        or (receipt.get("desired_target_active") is False
                            and receipt.get("target_verification", {}).get("runtime_online") is False
                            and receipt.get("target_verification", {}).get("runtime_present") is False
                            and receipt.get("target_verification", {}).get("presence_checked") is True
                            and receipt.get("target_verification", {}).get("config_inactive") is True))
                    or receipt.get("target_instance") != source
                    or receipt.get("package_id") != path.parent.name
                    or (agent_id, lifecycle) not in current
                    or datetime.fromisoformat(receipt["completed_at"]) <= latest[lifecycle]):
                continue
        except (AgentMoveError, OSError, KeyError, TypeError, ValueError):
            continue
        result.append({
            "package_id": receipt["package_id"],
            "agent_id": agent_id,
            "agent_lifecycle_id": lifecycle,
            "state": "target_completed",
            "completed_at": receipt["completed_at"],
        })
    return result


def moved_agent_destination(
    hashi_root: Path | str,
    agent_id: str,
) -> dict[str, str] | None:
    """Return the audit-only destination for an Agent removed by a completed move."""

    name = str(agent_id or "").strip()
    if not name:
        return None
    root = _root(hashi_root)
    try:
        agents_data = _load_json(root / "agents.json")
        if any(
            str(row.get("name") or row.get("id") or "").casefold()
            == name.casefold()
            and row.get("is_active", True) is not False
            for row in _agent_rows(agents_data)
        ):
            return None
    except AgentMoveError:
        return None
    path = root / "state" / "agent_moves" / "moved_agents.json"
    data = _load_json_or_default(path, {"agents": {}})
    if not isinstance(data, dict) or not isinstance(data.get("agents"), dict):
        return None
    raw = data["agents"].get(name.casefold())
    if not isinstance(raw, dict):
        return None
    target_agent_id = str(raw.get("target_agent_id") or "").strip()
    target_instance = _normalize_instance(raw.get("target_instance"))
    if not target_agent_id or not target_instance:
        return None
    return {
        "agent_id": str(raw.get("agent_id") or name),
        "target_agent_id": target_agent_id,
        "target_instance": target_instance,
        "address": f"{target_agent_id}@{target_instance}",
        "package_id": str(raw.get("package_id") or ""),
        "moved_at": str(raw.get("moved_at") or ""),
    }


def _preflight_clone_import(
    root: Path, package: AgentMoveArchive, *, target_agent_id: str,
    passphrase: str | None,
) -> None:
    """Exercise the existing import rules without publishing target state.

    Independent known configuration problems are reported together, before
    showing a confirmation. Commit still checks revisions and ownership: a
    competing writer after staging must never be overwritten.
    """
    source_values = decrypt_agent_secrets(package, passphrase)
    backup_clone = package.manifest.get("retain_telegram_backup") is True
    if backup_clone:
        source_values.pop(str(package.access_requirements.get("telegram_secret_key") or ""), None)
    values, mapping = _remap_agent_secrets(
        source_values, source_agent_id=package.agent_id, target_agent_id=target_agent_id,
    )
    errors: list[str] = []
    try:
        _prepare_import_config(
            root, package, target_agent_id=target_agent_id,
            operation="clone", secret_key_mapping=mapping,
        )
    except (AgentMoveError, ValueError, TypeError) as exc:
        errors.append(f"Agent configuration: {exc}")
    try:
        data = _load_json_or_default(root / "secrets.json", {})
        if not isinstance(data, dict):
            raise AgentMoveError("must contain a JSON object")
        _merge_secrets_preview(
            data, values, preserve_backup_key=(
                backup_clone and bool(package.access_requirements.get("telegram_secret_included"))
            ),
        )
    except (AgentMoveError, ValueError, TypeError) as exc:
        errors.append(f"secrets.json: {exc}")
    try:
        data = _load_json_or_default(root / "tasks.json", {"version": 1, "heartbeats": [], "crons": [], "nudges": []})
        if not isinstance(data, dict):
            raise AgentMoveError("must contain a JSON object")
        _merge_schedules(
            data, package.schedules, package_id=package.package_id,
            source_instance=str(package.manifest["source_instance"]),
            source_agent_id=package.agent_id, target_agent_id=target_agent_id,
        )
    except (AgentMoveError, ValueError, TypeError) as exc:
        errors.append(f"tasks.json: {exc}")
    try:
        data = _load_json_or_default(root / "agent_capabilities.json", {"agents": []})
        _append_agent_capability(data, package.agent_capability, target_agent_id=target_agent_id, package_id=package.package_id)
    except (AgentMoveError, ValueError, TypeError) as exc:
        errors.append(f"agent_capabilities.json: {exc}")
    if errors:
        raise CloneImportPreflightError("clone target import preflight failed: " + "; ".join(errors))


def _unbound_clone_telegram_key(
    agents_data: Any,
    existing_secrets: Mapping[str, Any],
    imported_secrets: Mapping[str, Any],
) -> str:
    """Select a vacant reference; a Clone must never adopt another Bot."""
    occupied = set(existing_secrets) | set(imported_secrets) | set(TELEGRAM_PROTECTED_SECRET_KEYS)
    for row in _agent_rows(agents_data):
        name = str(row.get("name") or row.get("id") or "").strip()
        key = str(row.get("telegram_token_key") or name).strip()
        if key:
            occupied.add(key)
    # An unpredictable, vacant reference also prevents a different writer
    # adding the conventional Agent-name secret during the commit window.
    while True:
        candidate = "__hashi_clone_telegram_" + uuid4().hex
        if candidate not in occupied:
            return candidate


def _prepare_import_config(
    root: Path,
    package: AgentMoveArchive,
    *,
    target_agent_id: str,
    operation: str,
    secret_key_mapping: Mapping[str, str],
    access_scope_plan: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], str | None]:
    row = _rewrite_secret_references(dict(package.agent_config), secret_key_mapping)
    row.pop("id", None)
    row.pop("system_md", None)
    row.pop("telegram_token", None)
    for field in _SOURCE_BOUND_CONFIG_FIELDS:
        row.pop(field, None)
    row.pop("transfer_state", None)
    row.pop("transfer_target", None)
    row["name"] = target_agent_id
    row["workspace_dir"] = f"workspaces/{target_agent_id}"
    if operation == "clone":
        row[AGENT_LIFECYCLE_FIELD] = new_agent_lifecycle_id()
        row["telegram_token_key"] = target_agent_id
        extra = row.get("extra")
        if isinstance(extra, dict) and AGENT_LIFECYCLE_FIELD in extra:
            # A legacy shadow belongs to the source incarnation, never Clone.
            row["extra"] = dict(extra)
            row["extra"].pop(AGENT_LIFECYCLE_FIELD)
    else:
        lifecycle_id = row.get(AGENT_LIFECYCLE_FIELD)
        if lifecycle_id is None:
            lifecycle_id = new_agent_lifecycle_id()
            row[AGENT_LIFECYCLE_FIELD] = lifecycle_id
        if not valid_agent_lifecycle_id(lifecycle_id):
            raise AgentMoveError("imported Agent lifecycle identity is malformed")
        source_token_key = str(
            package.access_requirements.get("telegram_secret_key")
            or row.get("telegram_token_key")
            or package.agent_id
        )
        row["telegram_token_key"] = str(
            secret_key_mapping.get(source_token_key) or source_token_key
        )
    row["is_active"] = False
    row["transfer_import_state"] = "committed_inactive"
    row["transfer_package_id"] = package.package_id
    row["transfer_source_instance"] = str(package.manifest.get("source_instance") or "")

    agent_type = str(row.get("type") or "")
    if agent_type not in {"flex", "limited"}:
        raise AgentMoveError(f"unsupported imported Agent type {agent_type!r}")
    try:
        allowed = normalize_allowed_backends(list(row.get("allowed_backends") or []))
        active = migrate_provider_only_active_backend(
            row.get("active_backend"), allowed
        )
    except ValueError as exc:
        raise AgentMoveError(str(exc)) from exc
    if not active or active not in {item.get("engine") for item in allowed}:
        raise AgentMoveError(
            f"imported active_backend {active!r} is not in allowed_backends"
        )
    row["allowed_backends"] = allowed
    row["active_backend"] = active
    configured_mode = str(row.get("default_mode") or "").strip().lower()
    mode = configured_mode or default_agent_mode_for_backend(active)
    if mode not in SUPPORTED_AGENT_MODES:
        raise AgentMoveError(f"unsupported imported default_mode {mode!r}")
    if mode == "fixed" and active not in SESSION_MODE_BACKENDS:
        raise AgentMoveError(
            f"fixed mode is not supported by imported backend {active!r}"
        )
    row["default_mode"] = mode

    plan = access_scope_plan or _access_scope_plan(root, package)
    requested = plan["requested"]
    effective = plan["effective"]
    row["access_scope"] = effective
    warning = None
    if effective != requested:
        warning = (
            f"access_scope was clamped from {requested} to {effective} by target policy"
        )
    return row, warning


def _assert_target_available(
    root: Path,
    agent_id: str,
    *,
    source_instance: str,
) -> dict[str, Any] | None:
    data = _load_json(root / "agents.json")
    folded = agent_id.casefold()
    for row in _agent_rows(data):
        current = str(row.get("name") or row.get("id") or "")
        if current.casefold() == folded:
            if (
                current == agent_id
                and row.get("is_active") is False
                and row.get("transfer_state") == "moved_out_pending_reboot"
                and str(row.get("transfer_package_id") or "")
                and str(row.get("transfer_target") or "").strip().upper()
                == _normalize_instance(source_instance)
                and (root / "workspaces" / agent_id).is_dir()
                and not (root / "workspaces" / agent_id).is_symlink()
            ):
                return dict(row)
            raise AgentMoveError(f"target already has Agent '{current}'")
    workspace_parent = root / "workspaces"
    if workspace_parent.is_dir():
        for workspace in workspace_parent.iterdir():
            if workspace.name.casefold() == folded:
                raise AgentMoveError(
                    f"target workspace already exists for Agent '{workspace.name}'"
                )
    return None


def _occupied_agent_ids(root: Path) -> dict[str, str]:
    occupied: dict[str, str] = {}
    data = _load_json(root / "agents.json")
    for row in _agent_rows(data):
        name = str(row.get("name") or row.get("id") or "").strip()
        if name:
            occupied.setdefault(name.casefold(), name)
    capabilities = _load_json_or_default(root / "agent_capabilities.json", {})
    entries = capabilities.get("agents", []) if isinstance(capabilities, dict) else []
    if isinstance(entries, dict):
        for name in entries:
            occupied.setdefault(str(name).casefold(), str(name))
    elif isinstance(entries, list):
        for row in entries:
            if isinstance(row, dict):
                name = str(row.get("name") or row.get("id") or "").strip()
                if name:
                    occupied.setdefault(name.casefold(), name)
    workspace_parent = root / "workspaces"
    if workspace_parent.is_dir():
        for workspace in workspace_parent.iterdir():
            occupied.setdefault(workspace.name.casefold(), workspace.name)
    incoming = _move_root(root) / "incoming"
    if incoming.is_dir():
        for transaction in incoming.iterdir():
            state_path = transaction / "state.json"
            if not state_path.is_file():
                continue
            state = _load_json(state_path)
            if str(state.get("status") or "") in {"completed", "rolled_back"}:
                continue
            reserved = str(
                state.get("target_agent_id") or state.get("agent_id") or ""
            ).strip()
            if reserved:
                occupied.setdefault(reserved.casefold(), reserved)
    return occupied


def _normalize_operation(value: Any) -> str:
    operation = str(value or "legacy_move").strip().lower()
    if operation not in _TRANSFER_OPERATIONS:
        raise AgentMoveError(f"unsupported Agent transfer operation {operation!r}")
    return operation


def _record_operation(record: Mapping[str, Any]) -> str:
    return _normalize_operation(record.get("operation") or "legacy_move")


def _append_agent_config(data: Any, row: dict[str, Any]) -> Any:
    _agent_rows(data).append(row)
    return data


def _find_owned_agent(data: Any, agent_id: str, package_id: str) -> dict[str, Any]:
    for row in _agent_rows(data):
        if (
            str(row.get("name") or row.get("id") or "") == agent_id
            and row.get("transfer_package_id") == package_id
        ):
            return row
    raise AgentMoveError(
        "imported target Agent config was not found or no longer belongs to this move"
    )


def _merge_secrets_preview(
    target: Mapping[str, Any],
    imported: Mapping[str, Any],
    *,
    replace_conflicts_for_agent: str | None = None,
    preserve_backup_key: bool = False,
) -> tuple[list[str], list[str], list[str]]:
    added: list[str] = []
    reused: list[str] = []
    replaced: list[str] = []
    for key, value in imported.items():
        if key == _RETAINED_BACKUP_KEY and (
            preserve_backup_key or (key in target and target[key] != value)
        ):
            raise AgentMoveError("target credential collides with local encrypted backup key")
        if key in target:
            if target[key] != value:
                if not _agent_owns_secret_key(key, replace_conflicts_for_agent):
                    raise AgentMoveError(f"target credential key collision: {key}")
                replaced.append(str(key))
            else:
                reused.append(str(key))
        else:
            added.append(str(key))
    return sorted(added), sorted(reused), sorted(replaced)


def _agent_owns_secret_key(key: Any, agent_id: str | None) -> bool:
    name = str(key or "")
    owner = str(agent_id or "")
    return bool(
        owner
        and (
            name == owner
            or name.startswith(f"{owner}_")
            or name.startswith(f"{owner}.")
        )
    )


def _remap_secret_key(key: str, source_agent_id: str, target_agent_id: str) -> str:
    name = str(key)
    if source_agent_id == target_agent_id:
        return name
    if name == source_agent_id:
        return target_agent_id
    for separator in ("_", "."):
        prefix = f"{source_agent_id}{separator}"
        if name.startswith(prefix):
            return f"{target_agent_id}{separator}{name[len(prefix):]}"
    return f"{target_agent_id}.{name}"


def _remap_agent_secrets(
    values: Mapping[str, Any],
    *,
    source_agent_id: str,
    target_agent_id: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    remapped: dict[str, Any] = {}
    mapping: dict[str, str] = {}
    for raw_key, value in values.items():
        source_key = str(raw_key)
        target_key = _remap_secret_key(
            source_key,
            source_agent_id,
            target_agent_id,
        )
        if target_key in remapped and remapped[target_key] != value:
            raise AgentMoveError(
                f"Agent credential remapping collision: {source_key} -> {target_key}"
            )
        remapped[target_key] = value
        mapping[source_key] = target_key
    return remapped, mapping


def _rewrite_secret_references(value: Any, mapping: Mapping[str, str]) -> Any:
    if isinstance(value, dict):
        return {
            key: _rewrite_secret_references(item, mapping)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_rewrite_secret_references(item, mapping) for item in value]
    if isinstance(value, str):
        return mapping.get(value, value)
    return value


def _append_agent_capability(
    data: Any,
    capability: Mapping[str, Any] | None,
    *,
    target_agent_id: str,
    package_id: str,
) -> Any:
    if capability is None:
        return data
    if not isinstance(data, dict):
        raise AgentMoveError("target agent_capabilities.json must be a JSON object")
    entries = data.setdefault("agents", [])
    row = dict(capability)
    row.pop("id", None)
    row["name"] = target_agent_id
    row["transfer_package_id"] = package_id
    if isinstance(entries, list):
        for current in entries:
            if isinstance(current, dict) and str(
                current.get("name") or current.get("id") or ""
            ).casefold() == target_agent_id.casefold():
                raise AgentMoveError(
                    f"target already has Agent capability '{target_agent_id}'"
                )
        entries.append(row)
        return data
    if isinstance(entries, dict):
        if any(str(key).casefold() == target_agent_id.casefold() for key in entries):
            raise AgentMoveError(
                f"target already has Agent capability '{target_agent_id}'"
            )
        entries[target_agent_id] = row
        return data
    raise AgentMoveError("target Agent capabilities must be a list or object")


def _remove_owned_agent_capability(
    data: Any,
    *,
    agent_id: str,
    package_id: str,
) -> None:
    if not isinstance(data, dict):
        raise AgentMoveError("target agent_capabilities.json must be a JSON object")
    entries = data.get("agents", [])
    if isinstance(entries, list):
        matches = [
            row
            for row in entries
            if isinstance(row, dict)
            and str(row.get("name") or row.get("id") or "") == agent_id
        ]
        if any(row.get("transfer_package_id") != package_id for row in matches):
            raise AgentMoveError(
                "target Agent capability no longer belongs to this transfer"
            )
        entries[:] = [
            row
            for row in entries
            if not (
                isinstance(row, dict)
                and str(row.get("name") or row.get("id") or "") == agent_id
                and row.get("transfer_package_id") == package_id
            )
        ]
        return
    if isinstance(entries, dict):
        key = next(
            (key for key in entries if str(key).casefold() == agent_id.casefold()),
            None,
        )
        if key is None:
            return
        row = entries[key]
        if not isinstance(row, dict) or row.get("transfer_package_id") != package_id:
            raise AgentMoveError(
                "target Agent capability no longer belongs to this transfer"
            )
        del entries[key]
        return
    raise AgentMoveError("target Agent capabilities must be a list or object")


def _remove_dormant_agent_config(
    data: Any,
    agent_id: str,
    dormant_package_id: str,
) -> None:
    rows = _agent_rows(data)
    matches = [
        row
        for row in rows
        if str(row.get("name") or row.get("id") or "") == agent_id
    ]
    if len(matches) != 1:
        raise AgentMoveError("dormant target Agent config is not unique")
    row = matches[0]
    if (
        row.get("is_active") is not False
        or row.get("transfer_state") != "moved_out_pending_reboot"
        or str(row.get("transfer_package_id") or "") != dormant_package_id
    ):
        raise AgentMoveError("dormant target Agent config changed before commit")
    rows.remove(row)


def _remove_dormant_schedules(
    data: Any,
    agent_id: str,
    dormant_package_id: str,
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise AgentMoveError("target tasks.json must contain a JSON object")
    result = data.copy()
    for section in ("heartbeats", "crons", "nudges"):
        result[section] = [
            item
            for item in list(result.get(section) or [])
            if not (
                isinstance(item, dict)
                and item.get("agent") == agent_id
                and item.get("transfer_disabled_by") == dormant_package_id
            )
        ]
    return result


def _merge_schedules(
    target: Any,
    imported: Mapping[str, Any],
    *,
    package_id: str,
    source_instance: str,
    source_agent_id: str,
    target_agent_id: str,
) -> tuple[dict[str, list[str]], dict[str, Any]]:
    if not isinstance(target, dict):
        raise AgentMoveError("target tasks.json must contain a JSON object")
    result = target.copy()
    imported_ids: dict[str, list[str]] = {}
    for section in ("heartbeats", "crons", "nudges"):
        existing = list(result.get(section) or [])
        ids = {str(item.get("id")) for item in existing if isinstance(item, dict)}
        section_ids: list[str] = []
        for original in imported.get(section, []) or []:
            if not isinstance(original, dict):
                continue
            item = dict(original)
            original_id = str(item.get("id") or f"moved-{uuid4().hex[:12]}")
            candidate = original_id
            if candidate in ids:
                candidate = f"{original_id}--moved-{package_id[:8]}"
                suffix = 1
                while candidate in ids:
                    suffix += 1
                    candidate = f"{original_id}--moved-{package_id[:8]}-{suffix}"
            item["id"] = candidate
            item["agent"] = target_agent_id
            item["enabled"] = False
            item["import_state"] = "disabled_review_draft"
            item["import_package_id"] = package_id
            item["import_source_instance"] = source_instance
            item["source_task_id"] = original_id
            item["source_agent_id"] = source_agent_id
            ids.add(candidate)
            existing.append(item)
            section_ids.append(candidate)
        result[section] = existing
        imported_ids[section] = section_ids
    result.setdefault("version", 1)
    return imported_ids, result


def _credential_status(
    root: Path,
    package: AgentMoveArchive,
    passphrase: str | None,
    *,
    decrypt: bool = True,
    target_agent_id: str | None = None,
) -> dict[str, Any]:
    target = _load_json_or_default(root / "secrets.json", {})
    target_keys = set(target) if isinstance(target, dict) else set()
    packaged: dict[str, Any] = {}
    if decrypt and "secrets/agent.enc" in package.names:
        packaged = decrypt_agent_secrets(package, passphrase)
    selected_target = str(target_agent_id or package.agent_id)
    backup_clone = package.manifest.get("retain_telegram_backup") is True
    if backup_clone:
        packaged.pop(str(package.access_requirements.get("telegram_secret_key") or ""), None)
    packaged, _mapping = _remap_agent_secrets(
        packaged,
        source_agent_id=package.agent_id,
        target_agent_id=selected_target,
    )
    packaged_keys = set(packaged)
    source_telegram_key = str(package.access_requirements.get("telegram_secret_key") or "")
    required = {
        _remap_secret_key(str(item), package.agent_id, selected_target)
        for item in package.access_requirements.get("agent_secret_keys", [])
        if str(item).strip() and not (backup_clone and str(item) == source_telegram_key)
    }
    return {
        "required_keys": sorted(required),
        "packaged_keys": sorted(packaged_keys),
        "target_existing_keys": sorted(required & target_keys),
        "missing_keys": sorted(required - target_keys - packaged_keys),
        "shared_credentials_rebind_required": list(
            package.access_requirements.get("target_rebind_required") or []
        ),
        "telegram_configured": bool(
            not backup_clone and package.access_requirements.get("telegram_secret_included")
        ),
        "telegram_backup_quarantined": bool(
            backup_clone and package.access_requirements.get("telegram_secret_included")
        ),
    }


_RETAINED_BACKUP_KEY = "_hashi_agent_move_backup_key_v1"
_RETAINED_BACKUP_PREFIX = "hashi-agent-backup-v1:"
_RETAINED_BACKUP_FORMAT = "local-instance-v1"


def _backup_cipher(secrets_data: Mapping[str, Any]):
    from cryptography.fernet import Fernet

    raw = secrets_data.get(_RETAINED_BACKUP_KEY)
    if not isinstance(raw, str) or not raw.startswith(_RETAINED_BACKUP_PREFIX):
        raise AgentMoveError("local encrypted backup key is unavailable")
    try:
        return Fernet(raw.removeprefix(_RETAINED_BACKUP_PREFIX).encode("ascii"))
    except (ValueError, TypeError, UnicodeError) as exc:
        raise AgentMoveError("local encrypted backup key is invalid") from exc


def _ensure_backup_cipher(root: Path, secrets_data: dict):
    from cryptography.fernet import Fernet

    if _RETAINED_BACKUP_KEY not in secrets_data:
        # Use the existing instance credential store, with its normal private
        # publication and revision checks. The key survives transport rotation.
        secrets_data[_RETAINED_BACKUP_KEY] = (
            _RETAINED_BACKUP_PREFIX + Fernet.generate_key().decode("ascii")
        )
        _atomic_json(root / "secrets.json", secrets_data, mode=0o600)
    return _backup_cipher(secrets_data)


def _read_local_backup(path: Path, cipher, expected: Mapping[str, Any]) -> str:
    from cryptography.fernet import InvalidToken

    try:
        if _workspace_path_is_link(path) or not path.is_file():
            raise ValueError("missing or linked backup")
        data = json.loads(cipher.decrypt(path.read_bytes()).decode("utf-8"))
        if not isinstance(data, dict) or set(data) != set(expected) | {"token"}:
            raise ValueError("invalid backup shape")
        if any(data.get(name) != value for name, value in expected.items()):
            raise ValueError("backup binding differs")
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise ValueError("invalid backup credential")
        return token
    except (InvalidToken, ValueError, TypeError, UnicodeError, OSError) as exc:
        raise AgentMoveError("local encrypted Telegram backup could not be verified") from exc


def _preserve_encrypted_clone_backup(
    root: Path, record_dir: Path, package: AgentMoveArchive, *,
    target_agent_id: str, lifecycle_id: str, token: Any, secrets_data: dict,
) -> dict[str, Any]:
    """Retain a target-encrypted credential without binding a live connector."""
    if not isinstance(token, str) or not token:
        raise AgentMoveError("Telegram backup credential is invalid")
    with zipfile.ZipFile(package.package_path, "r") as archive:
        transport = archive.read("secrets/agent.enc")
    transport_digest = hashlib.sha256(transport).hexdigest()
    if transport_digest != package.checksums.get("secrets/agent.enc"):
        raise AgentMoveError("encrypted Telegram backup checksum changed")
    directory = record_dir / "retained-telegram-backup"
    if _workspace_path_is_link(directory):
        raise AgentMoveError("encrypted Telegram backup directory is a link")
    directory.mkdir(mode=0o700, exist_ok=True)

    def publish_once(path: Path, payload: bytes, verify_existing) -> None:
        if _workspace_path_is_link(path):
            raise AgentMoveError("existing encrypted Telegram backup is a link")
        if path.exists():
            verify_existing()
            return
        fd, temporary_name = tempfile.mkstemp(prefix="." + path.name + "-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                os.chmod(temporary_name, 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary_name, path)
            except FileExistsError:
                verify_existing()
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    transport_path = directory / "agent.enc"

    def verify_transport() -> None:
        if (not transport_path.is_file()
                or hashlib.sha256(transport_path.read_bytes()).hexdigest() != transport_digest):
            raise AgentMoveError("existing encrypted Telegram transport backup differs")

    publish_once(transport_path, transport, verify_transport)
    cipher = _ensure_backup_cipher(root, secrets_data)
    binding = {
        "version": 1,
        "package_id": package.package_id,
        "source_instance": str(package.manifest.get("source_instance") or "").upper(),
        "target_instance": _configured_instance_id(root),
        "source_agent_id": package.agent_id,
        "target_agent_id": target_agent_id,
        "agent_lifecycle_id": lifecycle_id,
    }
    local_path = directory / "local-telegram.enc"

    def verify_local() -> None:
        if _read_local_backup(local_path, cipher, binding) != token:
            raise AgentMoveError("existing local encrypted Telegram backup differs")

    local_payload = json.dumps({**binding, "token": token}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    publish_once(local_path, cipher.encrypt(local_payload), verify_local)
    verify_local()
    return {
        "status": "quarantined",
        "storage_path": local_path.relative_to(root).as_posix(),
        "transport_backup_path": transport_path.relative_to(root).as_posix(),
        "encryption": _RETAINED_BACKUP_FORMAT,
        "sha256": hashlib.sha256(local_path.read_bytes()).hexdigest(),
        "source_agent_id": package.agent_id,
        "target_agent_id": target_agent_id,
        "agent_lifecycle_id": lifecycle_id,
        "source_instance": package.manifest.get("source_instance"),
        "source_credential_key": package.access_requirements.get("telegram_secret_key"),
        "bot_fingerprint": telegram_bot_fingerprint(token),
        "live_connector": False,
    }


def _recover_quarantined_telegram_token(hashi_root: Path | str, package_id: str) -> str:
    """Internal target-side read; never starts or changes a Telegram connector."""
    root = _root(hashi_root)
    with _mutation_lock(root):
        return _recover_quarantined_telegram_token_locked(root, package_id)


def _recover_quarantined_telegram_token_locked(root: Path, package_id: str) -> str:
    record_dir, record = _load_record(root, package_id)
    if record.get("status") not in {
        "committed_inactive", "target_finalizing", "target_cleanup_pending", "completed",
    }:
        raise AgentMoveError("quarantined Telegram backup is not available for this transfer")
    backup = record.get("retained_telegram_backup")
    if (not isinstance(backup, Mapping) or backup.get("status") != "quarantined"
            or backup.get("live_connector") is not False
            or backup.get("encryption") != _RETAINED_BACKUP_FORMAT):
        raise AgentMoveError("local encrypted Telegram backup is unavailable")
    agent_id = str(record.get("target_agent_id") or record.get("agent_id") or "")
    rows = [row for row in _agent_rows(_load_json(root / "agents.json"))
            if str(row.get("name") or row.get("id") or "") == agent_id]
    if (len(rows) != 1 or str(rows[0].get(AGENT_LIFECYCLE_FIELD) or "").casefold()
            != str(record.get("imported_agent_lifecycle_id") or "").casefold()):
        raise AgentMoveError("Telegram backup target lifecycle changed")
    directory = record_dir / "retained-telegram-backup"
    path = directory / "local-telegram.enc"
    if (_workspace_path_is_link(directory)
            or backup.get("storage_path") != path.relative_to(root).as_posix()
            or _workspace_path_is_link(path) or not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != backup.get("sha256")):
        raise AgentMoveError("local encrypted Telegram backup changed")
    binding = {
        "version": 1,
        "package_id": str(record["package_id"]),
        "source_instance": str(record.get("source_instance") or "").upper(),
        "target_instance": _configured_instance_id(root),
        "source_agent_id": str(record.get("source_agent_id") or record.get("agent_id") or ""),
        "target_agent_id": agent_id,
        "agent_lifecycle_id": str(record.get("imported_agent_lifecycle_id") or "").casefold(),
    }
    token = _read_local_backup(path, _backup_cipher(_load_json(root / "secrets.json")), binding)
    if telegram_bot_fingerprint(token) != backup.get("bot_fingerprint"):
        raise AgentMoveError("local encrypted Telegram backup fingerprint changed")
    return token


def _preserve_retained_identity(
    root: Path,
    record_dir: Path,
    package: AgentMoveArchive,
) -> dict[str, Any] | None:
    metadata = package.retained_identity
    if metadata is None:
        return None
    content = read_retained_identity_bytes(package)
    if content is None:  # pragma: no cover - archive validation keeps these coupled
        raise AgentMoveError("schema 2 package is missing retained AGENT.md content")
    attachment_dir = record_dir / "retained-identity"
    attachment_path = attachment_dir / "AGENT.md"
    metadata_path = attachment_dir / "metadata.json"
    _atomic_bytes(attachment_path, content, mode=0o600)
    state = {
        **metadata,
        "storage_path": attachment_path.relative_to(root).as_posix(),
        "metadata_path": metadata_path.relative_to(root).as_posix(),
        "retention_policy": "persistent_transaction_attachment",
        "live_pcm": False,
        "status": "retained",
        "retained_at": utc_now_iso(),
    }
    _atomic_json(metadata_path, state, mode=0o600)
    return state


def _stage_warnings(
    root: Path,
    package: AgentMoveArchive,
    credential_status: Mapping[str, Any],
    *,
    target_environment: str,
    access_scope_plan: Mapping[str, str] | None = None,
    replaces_dormant_source: bool = False,
) -> list[str]:
    warnings = [str(item) for item in package.manifest.get("warnings", [])]
    if str(package.manifest.get("operation") or "").lower() == "clone":
        if package.manifest.get("retain_telegram_backup") is True:
            warnings.append(
                "inactive clone retains an encrypted Telegram credential backup without installing or starting its connector"
                if package.access_requirements.get("telegram_secret_included")
                else "inactive clone has no source Telegram credential to retain"
            )
        else:
            warnings.append(
                "clone excludes the Telegram token; the new Agent uses local Workbench/API until a distinct token is configured"
            )
    if package.manifest.get("source_environment") != target_environment:
        warnings.append(
            "cross-platform move: target rebuilt filesystem paths and permissions from portable metadata"
        )
    if package.retained_identity is not None:
        warnings.append(
            "root AGENT.md was preserved outside the workspace as a non-authoritative "
            "attachment; only agent.md is installed as the live PCM identity"
        )
    if replaces_dormant_source:
        warnings.append(
            "return move: the target's inactive retained source copy will be replaced with rollback recovery"
        )
    if credential_status.get("missing_keys"):
        warnings.append(
            "target credentials are incomplete; configure them before using dependent backends"
        )
    plan = access_scope_plan or _access_scope_plan(root, package)
    requested, effective = plan["requested"], plan["effective"]
    if requested != effective:
        warnings.append(
            f"target policy will clamp access_scope from {requested} to {effective}"
        )
    schedule_count = sum(
        len(package.schedules.get(section, []) or [])
        for section in ("heartbeats", "crons", "nudges")
    )
    if schedule_count:
        warnings.append(
            f"{schedule_count} schedule(s) will be imported disabled for review"
        )
    excluded_count = len(package.workspace_metadata.get("excluded") or [])
    if excluded_count:
        warnings.append(
            f"{excluded_count} non-portable, runtime, project, cache, or credential path(s) were excluded"
        )
    return list(dict.fromkeys(warnings))


def _strip_target_transaction_markers(
    root: Path,
    record: Mapping[str, Any],
) -> None:
    package_id = str(record.get("package_id") or "")
    agent_id = str(record.get("target_agent_id") or record.get("agent_id") or "")
    agents_path = root / "agents.json"
    agents = _load_json(agents_path)
    matches = [
        row
        for row in _agent_rows(agents)
        if str(row.get("name") or row.get("id") or "") == agent_id
    ]
    row = matches[0] if len(matches) == 1 else None
    if row is not None and row.get("transfer_package_id") == package_id:
        for key in _SOURCE_TRANSFER_FIELDS:
            row.pop(key, None)
        _atomic_json(agents_path, agents)

    tasks_path = root / "tasks.json"
    tasks = _load_json_or_default(
        tasks_path,
        {"version": 1, "heartbeats": [], "crons": [], "nudges": []},
    )
    changed_tasks = False
    for section in ("heartbeats", "crons", "nudges"):
        for item in tasks.get(section, []) or []:
            if not isinstance(item, dict) or item.get("import_package_id") != package_id:
                continue
            for key in ("import_package_id", "import_source_instance"):
                item.pop(key, None)
            changed_tasks = True
    if changed_tasks:
        _atomic_json(tasks_path, tasks)

    capabilities_path = root / "agent_capabilities.json"
    if capabilities_path.exists():
        capabilities = _load_json_or_default(capabilities_path, {"agents": []})
        capability = _find_agent_capability(capabilities, agent_id)
        if (
            capability is not None
            and capability.get("transfer_package_id") == package_id
        ):
            capability.pop("transfer_package_id", None)
            _atomic_json(capabilities_path, capabilities)


def _find_agent_capability(data: Any, agent_id: str) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        raise AgentMoveError("agent_capabilities.json must be a JSON object")
    entries = data.get("agents", [])
    if isinstance(entries, list):
        matches = [
            row
            for row in entries
            if isinstance(row, dict)
            and str(row.get("name") or row.get("id") or "") == agent_id
        ]
        if len(matches) > 1:
            raise AgentMoveError("Agent capability declaration is not unique")
        return matches[0] if matches else None
    if isinstance(entries, dict):
        key = next(
            (key for key in entries if str(key).casefold() == agent_id.casefold()),
            None,
        )
        value = entries.get(key) if key is not None else None
        return value if isinstance(value, dict) else None
    raise AgentMoveError("Agent capabilities must be a list or object")


def _surgical_target_rollback(root: Path, record: Mapping[str, Any]) -> None:
    package_id = str(record.get("package_id") or "")
    agent_id = str(record.get("target_agent_id") or record.get("agent_id") or "")
    agents_path = root / "agents.json"
    tasks_path = root / "tasks.json"
    secrets_path = root / "secrets.json"
    capabilities_path = root / "agent_capabilities.json"
    agents = _load_json(agents_path)
    rows = _agent_rows(agents)
    matching_rows = [
        row for row in rows if str(row.get("name") or row.get("id") or "") == agent_id
    ]
    if any(row.get("transfer_package_id") != package_id for row in matching_rows):
        raise AgentMoveError("target Agent config no longer belongs to this move")
    rows[:] = [
        row
        for row in rows
        if not (
            str(row.get("name") or row.get("id") or "") == agent_id
            and row.get("transfer_package_id") == package_id
        )
    ]

    tasks = _load_json_or_default(
        tasks_path,
        {"version": 1, "heartbeats": [], "crons": [], "nudges": []},
    )
    for section in ("heartbeats", "crons", "nudges"):
        tasks[section] = [
            item
            for item in tasks.get(section, [])
            if not (
                isinstance(item, dict) and item.get("import_package_id") == package_id
            )
        ]
    secrets = _load_json_or_default(secrets_path, {})
    if not isinstance(secrets, dict):
        raise AgentMoveError("target secrets.json must contain a JSON object")
    expected_hashes = dict(record.get("added_secret_hashes") or {})
    retained_secret_keys: list[str] = []
    for key in record.get("added_secret_keys", []) or []:
        name = str(key)
        if name not in secrets:
            continue
        expected = str(expected_hashes.get(name) or "")
        if expected and _secret_value_hash(secrets[name]) == expected:
            secrets.pop(name, None)
        else:
            retained_secret_keys.append(name)

    workspace = root / "workspaces" / agent_id
    quarantine = _move_root(root) / "rolled_back" / f"{package_id}-{agent_id}"
    quarantine.parent.mkdir(parents=True, exist_ok=True)
    if workspace.exists():
        if quarantine.exists():
            raise AgentMoveError("rollback workspace and quarantine both exist")
        os.replace(workspace, quarantine)

    _atomic_json(agents_path, agents)
    _atomic_json(tasks_path, tasks)
    if secrets_path.exists() or record.get("added_secret_keys"):
        _atomic_json(secrets_path, secrets, mode=0o600)
    if capabilities_path.exists():
        capabilities = _load_json_or_default(capabilities_path, {"agents": []})
        _remove_owned_agent_capability(
            capabilities,
            agent_id=agent_id,
            package_id=package_id,
        )
        _atomic_json(capabilities_path, capabilities)
    if retained_secret_keys and isinstance(record, dict):
        record["rollback_retained_secret_keys"] = retained_secret_keys
        warnings = list(record.get("warnings") or [])
        warning = (
            "rollback retained credential keys changed after import: "
            + ", ".join(retained_secret_keys)
        )
        if warning not in warnings:
            warnings.append(warning)
        record["warnings"] = warnings


def _secret_value_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _agent_config_hash(value: Mapping[str, Any]) -> str:
    durable = dict(value)
    durable.pop("is_active", None)
    for field in _SOURCE_TRANSFER_FIELDS:
        durable.pop(field, None)
    return _secret_value_hash(durable)


def _separate_source_schedules(
    tasks: dict[str, Any],
    state: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
    *,
    agent_id: str,
    package_id: str,
) -> dict[str, list[dict[str, Any]]]:
    """Keep unproven source schedules disabled, with exact originals in the journal."""
    originals = snapshot.get("source_originals") if isinstance(snapshot, Mapping) else None
    source_enabled = snapshot.get("source_enabled") if isinstance(snapshot, Mapping) else None
    states = dict(state.get("schedule_states") or {})
    present = dict(state.get("schedule_enabled_present") or {})
    retained: dict[str, list[dict[str, Any]]] = {}
    reserved = {"import_state", "import_package_id", "import_source_instance",
                "source_task_id", "source_agent_id"}
    for section in ("heartbeats", "crons", "nudges"):
        source_rows = originals.get(section) if isinstance(originals, Mapping) else None
        packaged_rows = snapshot.get(section) if isinstance(snapshot, Mapping) else None
        flags = source_enabled.get(section) if isinstance(source_enabled, Mapping) else None
        proven = []
        if (isinstance(source_rows, list) and isinstance(packaged_rows, list)
                and isinstance(flags, list)
                and len(source_rows) == len(packaged_rows) == len(flags)):
            for original, packaged, enabled in zip(source_rows, packaged_rows, flags):
                if not isinstance(original, dict) or not isinstance(packaged, dict) or type(enabled) is not bool:
                    continue
                expected = dict(original)
                expected["enabled"] = False
                expected["import_state"] = "disabled_review_draft"
                if (original.get("agent") == agent_id and not reserved.intersection(original)
                        and bool(original.get("enabled", False)) == enabled and expected == packaged):
                    proven.append(original)

        kept = []
        retained[section] = []
        for index, raw in enumerate(list(tasks.get(section) or [])):
            if not isinstance(raw, dict) or raw.get("agent") != agent_id:
                kept.append(raw)
                continue
            key = _schedule_state_key(section, raw, index)
            candidate = dict(raw)
            candidate.pop("transfer_disabled_by", None)
            if key in present and key in states:
                if present[key]:
                    candidate["enabled"] = states[key]
                else:
                    candidate.pop("enabled", None)
            is_owned = raw.get("transfer_disabled_by") == package_id and raw.get("enabled") is False
            if is_owned and key in present and key in states and candidate in proven:
                proven.remove(candidate)
                continue
            retained[section].append(dict(raw))
            item = dict(raw)
            item["enabled"] = False
            kept.append(item)
        tasks[section] = kept
    return retained


def _imported_schedule_intent(
    schedules: Mapping[str, Any], imported_ids: Mapping[str, list[str]]
) -> dict[str, dict[str, bool]] | None:
    source_enabled = schedules.get("source_enabled")
    if source_enabled is None:
        # Older packages erased this information. Never infer disabled.
        return None
    if not isinstance(source_enabled, dict):
        raise AgentMoveError("imported schedule intent is malformed")
    intent: dict[str, dict[str, bool]] = {}
    for section in ("heartbeats", "crons", "nudges"):
        originals = [item for item in schedules.get(section, []) or [] if isinstance(item, dict)]
        target_ids = imported_ids.get(section, [])
        values = source_enabled.get(section)
        if (not isinstance(values, list) or len(values) != len(originals)
                or len(originals) != len(target_ids) or any(type(value) is not bool for value in values)):
            raise AgentMoveError(f"imported schedule intent is incomplete for {section}")
        intent[section] = dict(zip(target_ids, values))
    return intent


def _imported_task_hashes(
    tasks: Mapping[str, Any],
    imported_task_ids: Mapping[str, list[str]],
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for section in ("heartbeats", "crons", "nudges"):
        selected = {str(item) for item in imported_task_ids.get(section, [])}
        result[section] = {
            str(item.get("id")): _secret_value_hash(item)
            for item in tasks.get(section, []) or []
            if isinstance(item, Mapping) and str(item.get("id")) in selected
        }
        if set(result[section]) != selected:
            raise AgentMoveError(
                f"imported schedule journal is incomplete for {section}"
            )
    return result


def _value_references_secret(value: Any, secret_key: str) -> bool:
    """Check runtime credential consumers, never arbitrary config strings."""
    for row in _agent_rows(value):
        name = str(row.get("name") or row.get("id") or "")
        if str(row.get("telegram_token_key") or name) == secret_key:
            return True
        engines = {str(row.get("active_backend") or row.get("engine") or "")}
        for backend in row.get("allowed_backends", []) or []:
            if isinstance(backend, Mapping):
                engines.add(str(backend.get("engine") or ""))
        if any(secret_key in get_secret_lookup_order(engine, name) for engine in engines if engine):
            return True
    return False


def _source_workspace_path(
    root: Path,
    agent_config: Mapping[str, Any],
    agent_id: str,
) -> Path:
    raw = str(
        agent_config.get("workspace_dir")
        or agent_config.get("workspace")
        or f"workspaces/{agent_id}"
    )
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = root / path
    if path.is_symlink():
        raise AgentMoveError("source Agent workspace cannot be a symlink")
    return path.resolve()


def _rename_workspace_without_replacement(source: Path, target: Path) -> None:
    """Move on the same filesystem without replacing a different object."""
    if os.name == "nt":
        os.rename(source, target)
        return
    import ctypes
    import errno
    import sys
    if sys.platform.startswith("linux"):
        library = ctypes.CDLL(None, use_errno=True)
        rename = getattr(library, "renameat2", None)
        if rename is not None:
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) == 0:
                return
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number), str(target))
    raise OSError(errno.ENOTSUP, "atomic no-replace directory rename is unavailable")


def _workspace_path_is_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    # Windows junctions and other reparse points are not always reported as
    # symlinks by older Python versions. Never follow or move one as a directory.
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & reparse)


def _preserve_source_workspace(root: Path, state: dict[str, Any], state_path: Path) -> None:
    """Retain the entire source directory; packaging may omit or predate files.

    The reservation is journaled before the rename so an interrupted cleanup
    recovers the same directory rather than moving or replacing another object.
    """
    raw = str(state.get("workspace_path") or "").strip()
    if not raw:
        raise AgentMoveError("source cleanup journal has no workspace path")
    candidate = Path(raw).expanduser()
    if _workspace_path_is_link(candidate):
        raise AgentMoveError("refusing to relocate a linked source workspace")
    workspace = candidate.resolve()
    protected = {root, root.parent, Path.home().resolve(), Path(workspace.anchor)}
    if workspace in protected:
        raise AgentMoveError("refusing to relocate a broad source workspace path")
    if workspace.exists() and not workspace.is_dir():
        raise AgentMoveError("source workspace is no longer a directory")
    retention = state.get("workspace_retention")
    if not isinstance(retention, dict):
        if not workspace.exists():
            state["workspace_retention"] = {"status": "missing", "original_path": str(workspace)}
            return
        container = workspace.parent / f".hashi-move-retained-{uuid4().hex}"
        container.mkdir(mode=0o700)
        retention = {
            "status": "preparing", "original_path": str(workspace),
            "container": str(container), "path": str(container / workspace.name),
        }
        state["workspace_retention"] = retention
        _atomic_json(state_path, state, mode=0o600)
    if retention.get("original_path") != str(workspace):
        raise AgentMoveError("source workspace retention journal changed")
    container = Path(str(retention.get("container") or ""))
    destination = Path(str(retention.get("path") or ""))
    if retention.get("status") == "missing":
        return
    if retention.get("status") == "in_place":
        if not workspace.is_dir():
            raise AgentMoveError("retained source workspace is unavailable")
        state["retained_source_workspace"] = str(workspace)
        return
    if (container.parent != workspace.parent
            or not container.name.startswith(".hashi-move-retained-")
            or destination != container / workspace.name
            or _workspace_path_is_link(container)
            or not container.is_dir()):
        raise AgentMoveError("source workspace retention journal is invalid")
    if retention.get("status") == "preserved":
        if not destination.is_dir() or _workspace_path_is_link(destination):
            raise AgentMoveError("retained source workspace is unavailable")
    elif destination.exists():
        if workspace.exists() or not destination.is_dir() or _workspace_path_is_link(destination):
            raise AgentMoveError("source workspace retention outcome is ambiguous")
        retention["status"] = "preserved"
    elif workspace.exists():
        try:
            _rename_workspace_without_replacement(workspace, destination)
            retention["status"] = "preserved"
        except OSError:
            if not workspace.is_dir() or os.path.lexists(destination):
                raise AgentMoveError("source workspace retention outcome is ambiguous")
            # Some mounts cannot rename a live directory. Keep it in place.
            retention["status"] = "in_place"
    else:
        raise AgentMoveError("source workspace retention outcome is unknown")
    state["workspace_retention"] = retention
    state["retained_source_workspace"] = str(workspace if retention["status"] == "in_place" else destination)
    _atomic_json(state_path, state, mode=0o600)


def _remove_source_agent_capability(data: Any, agent_id: str) -> None:
    if not isinstance(data, dict):
        raise AgentMoveError("source agent_capabilities.json must be a JSON object")
    entries = data.get("agents", [])
    if isinstance(entries, list):
        entries[:] = [
            row
            for row in entries
            if not (
                isinstance(row, dict)
                and str(row.get("name") or row.get("id") or "") == agent_id
            )
        ]
        return
    if isinstance(entries, dict):
        for key in list(entries):
            if str(key).casefold() == agent_id.casefold():
                del entries[key]
        return
    raise AgentMoveError("source Agent capabilities must be a list or object")


def _record_moved_agent(
    root: Path,
    *,
    agent_id: str,
    package_id: str,
    target_instance: str,
    target_agent_id: str,
    agent_lifecycle_id: str,
) -> None:
    path = _move_root(root) / "moved_agents.json"
    data = _load_json_or_default(path, {"schema_version": 1, "agents": {}})
    if not isinstance(data, dict):
        data = {"schema_version": 1, "agents": {}}
    agents = data.setdefault("agents", {})
    if not isinstance(agents, dict):
        agents = {}
        data["agents"] = agents
    agents[agent_id.casefold()] = {
        "source_instance_id": _configured_instance_id(root),
        "agent_lifecycle_id": agent_lifecycle_id,
        "agent_id": agent_id,
        "package_id": package_id,
        "target_agent_id": target_agent_id,
        "target_instance": _normalize_instance(target_instance),
        "moved_at": utc_now_iso(),
    }
    departures = data.setdefault("departures", {})
    if not isinstance(departures, dict):
        raise AgentMoveError("source departure history is invalid")
    departures[package_id] = dict(agents[agent_id.casefold()])
    _atomic_json(path, data, mode=0o600)


def _schedule_state_key(section: str, item: Mapping[str, Any], index: int) -> str:
    task_id = str(item.get("id") or "").strip()
    return f"{section}:{task_id}" if task_id else f"{section}:@{index}"


def _remove_owned_workspace(root: Path, record: Mapping[str, Any]) -> None:
    agent_id = str(record.get("target_agent_id") or record.get("agent_id") or "")
    if not agent_id:
        return
    workspace = root / "workspaces" / agent_id
    if workspace.exists():
        shutil.rmtree(workspace)


def _dormant_workspace_backup(record_dir: Path) -> Path:
    return record_dir / "recovery" / "dormant-workspace"


def _recover_interrupted_workspace_commit(
    root: Path,
    record_dir: Path,
    record: Mapping[str, Any],
) -> None:
    agent_id = str(record.get("target_agent_id") or record.get("agent_id") or "")
    package_id = str(record.get("package_id") or "")
    if not agent_id or not package_id:
        raise AgentMoveError("Agent move recovery state is incomplete")
    workspace_parent = root / "workspaces"
    final_workspace = workspace_parent / agent_id
    staging_workspace = workspace_parent / f".{agent_id}.agent-move-{package_id}.tmp"
    _remove_path(staging_workspace)

    if record.get("replaces_dormant_source"):
        dormant_workspace = _dormant_workspace_backup(record_dir)
        if dormant_workspace.exists():
            _remove_path(final_workspace)
            os.replace(dormant_workspace, final_workspace)
        elif not final_workspace.is_dir() or final_workspace.is_symlink():
            raise AgentMoveError(
                "dormant target workspace could not be recovered"
            )
        return
    _remove_path(final_workspace)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _write_recovery_snapshots(root: Path, record_dir: Path) -> None:
    recovery = record_dir / "recovery"
    recovery.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, Any] = {"created_at": utc_now_iso(), "files": {}}
    for name in (
        "agents.json",
        "tasks.json",
        "secrets.json",
        "agent_capabilities.json",
    ):
        source = root / name
        target = recovery / name
        if source.exists():
            data = source.read_bytes()
            _atomic_bytes(target, data, mode=0o600)
            metadata["files"][name] = {
                "existed": True,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        else:
            metadata["files"][name] = {"existed": False}
    _atomic_json(recovery / "manifest.json", metadata, mode=0o600)


def _record_recovery_publication(
    record_dir: Path,
    name: str,
    published_revision: str | None,
) -> None:
    """Journal one completed config publication for guarded recovery.

    A crash between publishing a config and this journal update deliberately
    leaves recovery unable to prove ownership.  In that case recovery fails
    closed instead of replacing a possible external writer's value.
    """

    if not published_revision:
        raise AgentMoveError(f"missing published revision for {name}")
    manifest_path = record_dir / "recovery" / "manifest.json"
    manifest = _load_json(manifest_path)
    files = manifest.get("files")
    item = files.get(name) if isinstance(files, Mapping) else None
    if not isinstance(item, dict):
        raise AgentMoveError(f"recovery manifest has no snapshot for {name}")
    item["published_revision"] = published_revision
    _atomic_json(manifest_path, manifest, mode=0o600)


def _restore_recovery_snapshots(root: Path, record_dir: Path) -> None:
    recovery = record_dir / "recovery"
    manifest_path = recovery / "manifest.json"
    if not manifest_path.exists():
        raise AgentMoveError("interrupted commit has no recovery snapshot")
    manifest = _load_json(manifest_path)
    for name, item in (manifest.get("files") or {}).items():
        if name not in {
            "agents.json",
            "tasks.json",
            "secrets.json",
            "agent_capabilities.json",
        }:
            continue
        if not isinstance(item, Mapping):
            raise AgentMoveError(f"invalid recovery manifest entry for {name}")
        target = root / name
        try:
            current = target.read_bytes()
        except FileNotFoundError:
            if not item.get("existed"):
                continue
            raise AgentMoveError(
                f"cannot recover {name}: live file was removed by another writer"
            )
        current_revision = hashlib.sha256(current).hexdigest()
        original_revision = str(item.get("sha256") or "")
        if item.get("existed") and current_revision == original_revision:
            continue
        published_revision = str(item.get("published_revision") or "")
        if current_revision != published_revision:
            raise AgentMoveError(
                f"cannot recover {name}: live file is not the transaction publication"
            )
        if item.get("existed"):
            original = (recovery / name).read_bytes()
            if hashlib.sha256(original).hexdigest() != original_revision:
                raise AgentMoveError(f"recovery snapshot checksum failed for {name}")
            _restore_json_snapshot(
                target,
                original,
                published_revision=published_revision,
            )
        else:
            delete_config_json(
                target,
                expected_revision=published_revision,
            )


def _target_max_access_scope(root: Path, *, target_config: Any = None) -> str:
    data = target_config if target_config is not None else _load_json(root / "agents.json")
    if not isinstance(data, dict):
        return "drive"  # Legacy list-shaped Agent configuration has no global policy.
    global_cfg = data.get("global", {})
    if not isinstance(global_cfg, dict):
        raise AgentMoveError("target global configuration is malformed")
    move_cfg = global_cfg.get("agent_move", {})
    if not isinstance(move_cfg, dict):
        raise InvalidTargetScopePolicy("target Agent move policy is malformed")
    raw = move_cfg.get("max_access_scope", "drive")
    value = raw.strip().lower() if isinstance(raw, str) else ""
    if value not in VALID_ACCESS_SCOPES:
        raise InvalidTargetScopePolicy("target max_access_scope is invalid")
    return value


def _access_scope_plan(
    root: Path, package: AgentMoveArchive, *, target_config: Any = None
) -> dict[str, str]:
    requirements = package.access_requirements
    config = package.agent_config
    raw = requirements.get("access_scope", config.get("access_scope", "project"))
    requested = raw.strip().lower() if isinstance(raw, str) else ""
    if requested not in VALID_ACCESS_SCOPES:
        raise AgentMoveError("imported Agent access_scope is invalid")
    maximum = _target_max_access_scope(root, target_config=target_config)
    effective = requested if _ACCESS_RANK[requested] <= _ACCESS_RANK[maximum] else maximum
    return {"requested": requested, "target_maximum": maximum, "effective": effective}


def _configured_instance_id(root: Path) -> str:
    try:
        data = _load_json(root / "agents.json")
        if isinstance(data, dict):
            value = str((data.get("global") or {}).get("instance_id") or "").strip()
            if value:
                return value.upper()
    except AgentMoveError:
        return "HASHI"
    return "HASHI"


def _target_deployment_profile(root: Path) -> str | None:
    """Return the target's effective deployment profile value, or None."""
    from orchestrator.enterprise.profile import resolve_deployment_profile

    config = _load_json(root / "agents.json")
    global_config = config.get("global", {})
    if not isinstance(global_config, Mapping):
        raise AgentMoveError("target deployment configuration is invalid")
    return resolve_deployment_profile(dict(global_config)).value


def _governed_target_owner_identity(root: Path) -> str | None:
    """Resolve the authenticated destination user for a governed target.

    Reuses the authenticated command principal already established by the
    Backend API / Worker boundary for this instance; it is the target's existing
    authentication mechanism, not a new pairing system. Governed users carry a
    canonical `enterprise:<user_id>` owner; any other form is not a governed
    destination user and must not be treated as one.
    """
    owner = authenticated_conversation_command_owner(root)
    if not owner:
        return None
    if not owner.startswith("enterprise:"):
        return None
    user_id = owner.removeprefix("enterprise:").strip()
    return owner if user_id else None


class GovernedHistoryMappingUnavailable(AgentMoveError):
    """A governed target has history but no verified destination user yet.

    Recoverable: the Clone may proceed without conversation history, or wait for
    a destination identity. It is never a reason to reject the whole transfer.
    """


def _require_governed_history_mapping(
    root: Path, package: AgentMoveArchive, *, local_authenticated: bool
) -> str | None:
    """Return the verified destination owner, or None when no mapping is needed.

    Personal deployments and locally-authenticated history do not need a governed
    mapping. A governed target with nonempty history must resolve the authenticated
    destination user; when it cannot, the caller downgrades to an Agent-only Clone
    rather than rejecting the whole operation.
    """
    capsule = package.conversation_continuity
    if capsule is None or local_authenticated:
        return None
    summary = capsule.get("summary") or {}
    if int(summary.get("session_count") or 0) == 0:
        return None
    if _target_deployment_profile(root) == "personal":
        return None
    owner = _governed_target_owner_identity(root)
    if owner is None:
        raise GovernedHistoryMappingUnavailable(
            "governed target needs an authenticated user mapping for conversation history"
        )
    return owner


def _configured_session_owner_id(root: Path) -> str:
    return configured_conversation_owner_id(root, instance_role="target")


def _conversation_owner_binding(
    root: Path,
    package: AgentMoveArchive,
    *,
    source_instance: str,
    target_instance: str,
    target_agent_id: str,
    target_owner_id: str | None = None,
) -> ConversationContinuityBinding:
    capsule = package.conversation_continuity
    if capsule is None:
        raise AgentMoveError("conversation continuity capsule is missing")
    return ConversationContinuityBinding(
        source_owner_id=str(capsule["owner_id"]).strip(),
        target_owner_id=target_owner_id or _configured_session_owner_id(root),
        source_instance=source_instance,
        target_instance=target_instance,
        source_agent_id=package.agent_id.lower(),
        target_agent_id=target_agent_id.lower(),
        transfer_id=package.package_id,
        history_mode=str(package.manifest.get("history_mode") or "").lower(),
        capsule_digest=str(capsule["capsule_digest"]).lower(),
    )


def _session_store(root: Path) -> SessionStore:
    return SessionStore(
        root / "state" / "sessions.sqlite3",
        instance_id=_configured_instance_id(root),
    )


def _rollback_target_conversation_continuity(
    root: Path,
    package_id: str,
) -> dict[str, Any]:
    return _session_store(root).rollback_conversation_continuity(package_id)


def _public_state(record: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "schema_version",
        "package_id",
        "agent_id",
        "source_agent_id",
        "target_agent_id",
        "operation",
        "desired_target_active",
        "source_instance",
        "target_instance",
        "source_environment",
        "target_environment",
        "sha256",
        "package_bytes",
        "package_schema",
        "history_mode",
        "conversation_continuity_summary",
        "status",
        "staged_at",
        "committed_at",
        "activated_at",
        "verified_at",
        "completed_at",
        "rolled_back_at",
        "staged_abort",
        "workspace",
        "imported_task_ids",
        "schedule_intent",
        "access_scope_plan",
        "credential_status",
        "telegram_delivery_state",
        "retained_telegram_backup",
        "retain_telegram_backup",
        "conversation_continuity",
        "retained_identity",
        "replaces_dormant_source",
        "dormant_package_id",
        "warnings",
        "reboot_required",
        "target_verified",
        "target_verification",
        "rollback_payload_retained",
    }
    return {key: value for key, value in record.items() if key in allowed} | {
        "ok": True
    }


def _root(value: Path | str) -> Path:
    root = Path(value).expanduser().resolve()
    if not (root / "agents.json").is_file():
        raise AgentMoveError("HASHI root does not contain agents.json")
    return root


def _move_root(root: Path) -> Path:
    path = root / "state" / "agent_moves"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _record_dir(root: Path, package_id: str) -> Path:
    if not package_id or any(
        char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
        for char in package_id
    ):
        raise AgentMoveError("invalid Agent move package_id")
    return _move_root(root) / "incoming" / package_id


def _load_record(root: Path, package_id: str) -> tuple[Path, dict[str, Any]]:
    directory = _record_dir(root, package_id)
    path = directory / "state.json"
    if not path.exists():
        raise AgentMoveError("Agent move package was not staged on this instance")
    record = _load_json(path)
    if str(record.get("package_id") or "") != package_id:
        raise AgentMoveError("Agent move state package_id mismatch")
    return directory, record


def _source_state_path(root: Path, package_id: str) -> Path:
    directory = _record_dir(root, package_id).parent.parent / "source"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{package_id}.json"


def _agent_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.setdefault("agents", [])
    else:
        raise AgentMoveError("agents.json must contain an object or list")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise AgentMoveError("agents.json agents must be a list of objects")
    return rows


def _load_json(path: Path) -> Any:
    try:
        return read_managed_json(path)
    except Exception as exc:
        raise AgentMoveError(f"invalid JSON file: {path.name}") from exc


def _load_json_or_default(path: Path, default: Any) -> Any:
    if not path.exists():
        if not isinstance(default, dict):
            raise AgentMoveError("managed JSON defaults must be objects")
        return new_config_json(path, json.loads(json.dumps(default)))
    return _load_json(path)


def _credential_publication_options(path: Path) -> dict:
    if path.name.casefold() != "secrets.json":
        return {}
    from orchestrator.workbench_auth import resolve_remote_runtime_sids

    return {"private_full_control_sids": resolve_remote_runtime_sids(path.parent)}


def _atomic_json(path: Path, value: Any, *, mode: int | None = None) -> None:
    del mode  # the shared primitive applies the platform-private policy
    path.parent.mkdir(parents=True, exist_ok=True)
    if not isinstance(value, (ConfigDocument, ConfigList)):
        if path.exists():
            raise AgentMoveError(
                f"revision-bearing read required before updating {path.name}"
            )
        if not isinstance(value, dict):
            raise AgentMoveError("new managed JSON documents must be objects")
        value = new_config_json(path, value)
    publisher = write_agent_config_json if path.name == "agents.json" else write_config_json
    publisher(path, value, **_credential_publication_options(path))


def _restore_json_snapshot(
    path: Path,
    original: bytes | None,
    *,
    published_revision: str,
) -> None:
    """Rollback only our own still-current publication, never another writer."""

    if original is None:
        delete_config_json(path, expected_revision=published_revision)
        return
    try:
        value = json.loads(original.decode("utf-8-sig"))
    except Exception as exc:
        raise AgentMoveError(f"invalid recovery JSON file: {path.name}") from exc
    if isinstance(value, dict):
        publisher = write_agent_config_json if path.name == "agents.json" else write_config_json
        publisher(
            path, value, expected_revision=published_revision,
            **_credential_publication_options(path),
        )
        return
    if isinstance(value, list):
        snapshot = ConfigList(
            value,
            source=path.resolve(),
            revision=published_revision,
        )
        write_config_json(path, snapshot)
        return
    raise AgentMoveError(f"recovery JSON must be an object or list: {path.name}")


def _atomic_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None and os.name != "nt":
            temporary.chmod(mode)
        os.replace(temporary, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _normalize_instance(value: Any) -> str:
    result = str(value or "").strip().upper()
    if not result:
        raise AgentMoveError("source instance id is required")
    return result


@contextmanager
def _mutation_lock(root: Path) -> Iterator[None]:
    with ExitStack() as locks:
        try:
            locks.enter_context(file_write_lock(_move_root(root) / "mutation-journal"))
        except TimeoutError as exc:
            raise AgentMoveError("target transfer journal is busy; retry the saved transfer") from exc
        yield
# HASHI-Fingerprint: hfp_e0f657401a3249898852ec4351f13388
