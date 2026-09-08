"""Target-owned, recoverable Agent move transactions.

The source sends an opaque ``agent-move-v1`` archive.  The receiver validates
and stages it without touching live configuration, then performs an explicit
inactive import.  Activation is a separate operation so a source can be
disabled first and restored if the target refuses activation.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from orchestrator.config import (
    SESSION_MODE_BACKENDS,
    SUPPORTED_AGENT_MODES,
    VALID_ACCESS_SCOPES,
    default_agent_mode_for_backend,
)
from orchestrator.flexible_backend_registry import (
    migrate_provider_only_active_backend,
    normalize_allowed_backends,
)
from orchestrator.process_execution import process_is_alive

from .package import (
    AGENT_MOVE_CAPABILITY,
    MAX_UNPACKED_BYTES,
    PACKAGE_SCHEMA_MIN_VERSION,
    PACKAGE_SCHEMA_VERSION,
    RETAINED_IDENTITY_CAPABILITY,
    AgentMoveArchive,
    AgentMoveError,
    decrypt_agent_secrets,
    detect_environment_kind,
    extract_agent_workspace,
    read_retained_identity_bytes,
    read_agent_move_package,
    utc_now_iso,
)
from .transport_crypto import ENVELOPE_SCHEME

MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MOVE_STATE_SCHEMA_VERSION = 1
_ACCESS_RANK = {"workspace": 0, "project": 1, "drive": 2}
_SOURCE_TRANSFER_FIELDS = (
    "transfer_import_state",
    "transfer_package_id",
    "transfer_source_instance",
    "transfer_state",
    "transfer_target",
)


def receiver_capabilities(hashi_root: Path | str) -> dict[str, Any]:
    root = _root(hashi_root)
    return {
        "ok": True,
        "capability": AGENT_MOVE_CAPABILITY,
        "capabilities": [
            AGENT_MOVE_CAPABILITY,
            RETAINED_IDENTITY_CAPABILITY,
        ],
        "package_type": "hashi-agent-move",
        "schema_min": PACKAGE_SCHEMA_MIN_VERSION,
        "schema_max": PACKAGE_SCHEMA_VERSION,
        "max_package_bytes": MAX_PACKAGE_BYTES,
        "max_unpacked_bytes": MAX_UNPACKED_BYTES,
        "environment_kind": detect_environment_kind(),
        "instance_id": _configured_instance_id(root),
        "target_owned_import": True,
        "staged_inactive": True,
        "encrypted_agent_secrets": True,
        "package_encryption": [ENVELOPE_SCHEME],
        "source_workspace_retained": True,
        "retained_identity_attachment": True,
        "max_access_scope": _target_max_access_scope(root),
    }


def stage_agent_move(
    hashi_root: Path | str,
    package_bytes: bytes,
    *,
    expected_sha256: str,
    source_instance: str,
    target_instance: str | None = None,
    secret_passphrase: str | None = None,
    target_platform: str | None = None,
) -> dict[str, Any]:
    """Verify and persist an archive without changing Agent configuration."""

    root = _root(hashi_root)
    source = _normalize_instance(source_instance)
    if not package_bytes:
        raise AgentMoveError("Agent move package is empty")
    if len(package_bytes) > MAX_PACKAGE_BYTES:
        raise AgentMoveError("Agent move package exceeds the receiver size limit")
    digest = hashlib.sha256(package_bytes).hexdigest()
    if not expected_sha256 or digest.lower() != str(expected_sha256).strip().lower():
        raise AgentMoveError("Agent move package SHA-256 does not match the request")

    move_root = _move_root(root)
    target_kind = target_platform or detect_environment_kind()
    inbox = move_root / "incoming"
    inbox.mkdir(parents=True, exist_ok=True)
    upload = inbox / f".upload-{uuid4().hex}.hashi-agent"
    _atomic_bytes(upload, package_bytes, mode=0o600)
    incomplete_record_dir: Path | None = None
    try:
        package = read_agent_move_package(
            upload,
            verify=True,
            target_platform=target_kind,
        )
        manifest_source = _normalize_instance(package.manifest.get("source_instance"))
        if manifest_source != source:
            raise AgentMoveError(
                f"package source_instance {manifest_source!r} does not match authenticated sender {source!r}"
            )
        target = _normalize_instance(
            target_instance or _configured_instance_id(root) or "HASHI"
        )
        if target == source:
            raise AgentMoveError("source and target HASHI instances must be different")
        if "secrets/agent.enc" in package.names:
            # Decrypt during staging so a bad/mismatched shared key fails before
            # any target configuration is touched.
            decrypt_agent_secrets(package, secret_passphrase)

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
                upload.unlink(missing_ok=True)
                return _public_state(current)

            dormant = _assert_target_available(
                root,
                package.agent_id,
                source_instance=source,
            )
            record_dir.mkdir(parents=True, exist_ok=False)
            incomplete_record_dir = record_dir
            os.replace(upload, package_path)
            package = read_agent_move_package(
                package_path,
                verify=True,
                target_platform=target_kind,
            )
            retained_identity = _preserve_retained_identity(
                root,
                record_dir,
                package,
            )
            credential_status = _credential_status(root, package, secret_passphrase)
            record = {
                "schema_version": MOVE_STATE_SCHEMA_VERSION,
                "package_id": package.package_id,
                "agent_id": package.agent_id,
                "source_instance": source,
                "target_instance": target,
                "source_environment": package.manifest.get("source_environment"),
                "target_environment": target_kind,
                "sha256": digest,
                "package_bytes": len(package_bytes),
                "package_schema": package.manifest.get("schema_version"),
                "status": "staged",
                "staged_at": utc_now_iso(),
                "credential_status": credential_status,
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
                    replaces_dormant_source=bool(dormant),
                ),
                "reboot_required": False,
            }
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
        if record.get("status") in {"committed_inactive", "activated_pending_reboot"}:
            return _public_state(record)
        if record.get("status") == "rolled_back":
            raise AgentMoveError(
                "rolled-back Agent move packages cannot be committed again"
            )
        if record.get("status") == "committing":
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
            target_platform=(
                target_platform
                or str(record.get("target_environment") or "")
                or detect_environment_kind()
            ),
        )
        if package.package_id != str(record.get("package_id")):
            raise AgentMoveError("staged Agent move state does not match its package")
        dormant = _assert_target_available(
            root,
            package.agent_id,
            source_instance=str(record.get("source_instance") or ""),
        )
        if bool(dormant) != bool(record.get("replaces_dormant_source")) or (
            dormant
            and str(dormant.get("transfer_package_id") or "")
            != str(record.get("dormant_package_id") or "")
        ):
            raise AgentMoveError(
                "target dormant Agent state changed after the move was staged"
            )
        secret_values = decrypt_agent_secrets(package, secret_passphrase)
        imported_config, access_warning = _prepare_import_config(root, package)

        agents_path = root / "agents.json"
        tasks_path = root / "tasks.json"
        secrets_path = root / "secrets.json"
        agents_data = _load_json(agents_path)
        tasks_data = _load_json_or_default(
            tasks_path,
            {"version": 1, "heartbeats": [], "crons": [], "nudges": []},
        )
        secrets_data = _load_json_or_default(secrets_path, {})
        if not isinstance(secrets_data, dict):
            raise AgentMoveError("target secrets.json must contain a JSON object")

        if dormant:
            _remove_dormant_agent_config(
                agents_data,
                package.agent_id,
                str(record.get("dormant_package_id") or ""),
            )
            tasks_data = _remove_dormant_schedules(
                tasks_data,
                package.agent_id,
                str(record.get("dormant_package_id") or ""),
            )
        added_secret_keys, reused_secret_keys, replaced_secret_keys = (
            _merge_secrets_preview(
                secrets_data,
                secret_values,
                replace_conflicts_for_agent=(package.agent_id if dormant else None),
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
        )
        agents_data = _append_agent_config(agents_data, imported_config)

        workspace_parent = root / "workspaces"
        workspace_parent.mkdir(parents=True, exist_ok=True)
        final_workspace = workspace_parent / package.agent_id
        staging_workspace = (
            workspace_parent
            / f".{package.agent_id}.agent-move-{package.package_id}.tmp"
        )
        if staging_workspace.exists():
            shutil.rmtree(staging_workspace)
        if final_workspace.exists() and not dormant:
            raise AgentMoveError(
                f"target workspace already exists for Agent '{package.agent_id}'"
            )
        if dormant and not final_workspace.is_dir():
            raise AgentMoveError("dormant target Agent workspace is no longer available")

        _write_recovery_snapshots(root, record_dir)
        record["status"] = "committing"
        record["commit_started_at"] = utc_now_iso()
        _atomic_json(record_dir / "state.json", record, mode=0o600)

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
            _atomic_json(tasks_path, tasks_data)
            if secret_values:
                secrets_data.update(secret_values)
                _atomic_json(secrets_path, secrets_data, mode=0o600)

            credential_status = _credential_status(root, package, secret_passphrase)
            warnings = list(record.get("warnings") or [])
            if access_warning and access_warning not in warnings:
                warnings.append(access_warning)
            record.update(
                {
                    "status": "committed_inactive",
                    "committed_at": utc_now_iso(),
                    "workspace": f"workspaces/{package.agent_id}",
                    "imported_task_ids": imported_task_ids,
                    "added_secret_keys": added_secret_keys,
                    "added_secret_hashes": added_secret_hashes,
                    "reused_secret_keys": reused_secret_keys,
                    "replaced_secret_keys": replaced_secret_keys,
                    "credential_status": credential_status,
                    "warnings": warnings,
                    "reboot_required": False,
                }
            )
            _atomic_json(record_dir / "state.json", record, mode=0o600)
            return _public_state(record)
        except Exception as exc:
            _restore_recovery_snapshots(root, record_dir)
            _recover_interrupted_workspace_commit(root, record_dir, record)
            record["status"] = "staged"
            record["last_error"] = str(exc)
            _atomic_json(record_dir / "state.json", record, mode=0o600)
            raise


def activate_agent_move(hashi_root: Path | str, package_id: str) -> dict[str, Any]:
    """Mark a committed Agent active in target config (effective after reboot)."""

    root = _root(hashi_root)
    with _mutation_lock(root):
        record_dir, record = _load_record(root, package_id)
        if record.get("status") == "activated_pending_reboot":
            return _public_state(record)
        if record.get("status") != "committed_inactive":
            raise AgentMoveError(
                f"Agent move must be committed inactive before activation (status={record.get('status')!r})"
            )
        package = read_agent_move_package(
            record_dir / "package.hashi-agent", verify=True
        )
        credentials = _credential_status(root, package, None, decrypt=False)
        if credentials["missing_keys"]:
            raise AgentMoveError(
                "target credentials are incomplete: "
                + ", ".join(credentials["missing_keys"])
            )

        path = root / "agents.json"
        original = path.read_bytes()
        data = _load_json(path)
        row = _find_owned_agent(data, package.agent_id, package.package_id)
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
            _atomic_bytes(path, original)
            raise
        return _public_state(record)


def rollback_agent_move(hashi_root: Path | str, package_id: str) -> dict[str, Any]:
    """Remove an imported target config and quarantine its workspace."""

    root = _root(hashi_root)
    with _mutation_lock(root):
        record_dir, record = _load_record(root, package_id)
        status = str(record.get("status") or "")
        if status == "rolled_back":
            return _public_state(record)
        if status == "committing":
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
                    str(record.get("agent_id") or ""),
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
            if record.get("replaces_dormant_source"):
                _restore_recovery_snapshots(root, record_dir)
                _recover_interrupted_workspace_commit(root, record_dir, record)
            else:
                _surgical_target_rollback(root, record)
        elif status != "staged":
            raise AgentMoveError(
                f"Agent move cannot be rolled back from status {status!r}"
            )

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


def get_agent_move_status(hashi_root: Path | str, package_id: str) -> dict[str, Any]:
    _, record = _load_record(_root(hashi_root), package_id)
    return _public_state(record)


def deactivate_source_agent(
    hashi_root: Path | str,
    agent_id: str,
    package_id: str,
    *,
    target_instance: str,
) -> dict[str, Any]:
    """Disable the source config while retaining its complete workspace."""

    root = _root(hashi_root)
    with _mutation_lock(root):
        state_path = _source_state_path(root, package_id)
        state: dict[str, Any] | None = None
        if state_path.exists():
            state = _load_json(state_path)
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
            for section in ("heartbeats", "crons", "nudges"):
                for index, item in enumerate(tasks.get(section, [])):
                    if isinstance(item, dict) and item.get("agent") == agent_id:
                        schedule_states[_schedule_state_key(section, item, index)] = (
                            bool(item.get("enabled", True))
                        )
            state = {
                "schema_version": MOVE_STATE_SCHEMA_VERSION,
                "package_id": package_id,
                "agent_id": agent_id,
                "target_instance": _normalize_instance(target_instance),
                "previous_active": bool(row.get("is_active", True)),
                "previous_transfer_fields": previous_transfer_fields,
                "schedule_states": schedule_states,
                "status": "source_disabling",
                "disable_started_at": utc_now_iso(),
                "workspace_retained": True,
                "reboot_required": False,
            }
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
            _atomic_json(agents_path, agents)
            _atomic_json(tasks_path, tasks)
            state["status"] = "source_disabled_pending_reboot"
            state["disabled_at"] = utc_now_iso()
            state["reboot_required"] = True
            _atomic_json(state_path, state, mode=0o600)
        except Exception:
            _atomic_bytes(agents_path, agents_original)
            if tasks_original is None:
                tasks_path.unlink(missing_ok=True)
            else:
                _atomic_bytes(tasks_path, tasks_original)
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
        for section in ("heartbeats", "crons", "nudges"):
            for index, item in enumerate(tasks.get(section, [])):
                key = _schedule_state_key(section, item, index)
                if (
                    isinstance(item, dict)
                    and item.get("transfer_disabled_by") == package_id
                ):
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


def _prepare_import_config(
    root: Path, package: AgentMoveArchive
) -> tuple[dict[str, Any], str | None]:
    row = dict(package.agent_config)
    row.pop("id", None)
    row.pop("system_md", None)
    row.pop("telegram_token", None)
    row.pop("transfer_state", None)
    row.pop("transfer_target", None)
    row["name"] = package.agent_id
    row["workspace_dir"] = f"workspaces/{package.agent_id}"
    row["telegram_token_key"] = str(row.get("telegram_token_key") or package.agent_id)
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

    requested = str(
        package.access_requirements.get("access_scope")
        or row.get("access_scope")
        or "project"
    ).lower()
    if requested not in VALID_ACCESS_SCOPES:
        requested = "workspace"
    maximum = _target_max_access_scope(root)
    effective = (
        requested if _ACCESS_RANK[requested] <= _ACCESS_RANK[maximum] else maximum
    )
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
) -> tuple[list[str], list[str], list[str]]:
    added: list[str] = []
    reused: list[str] = []
    replaced: list[str] = []
    for key, value in imported.items():
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
    result = dict(data)
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
) -> tuple[dict[str, list[str]], dict[str, Any]]:
    if not isinstance(target, dict):
        raise AgentMoveError("target tasks.json must contain a JSON object")
    result = dict(target)
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
            item["enabled"] = False
            item["import_state"] = "disabled_review_draft"
            item["import_package_id"] = package_id
            item["import_source_instance"] = source_instance
            item["source_task_id"] = original_id
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
) -> dict[str, Any]:
    target = _load_json_or_default(root / "secrets.json", {})
    target_keys = set(target) if isinstance(target, dict) else set()
    packaged: dict[str, Any] = {}
    if decrypt and "secrets/agent.enc" in package.names:
        packaged = decrypt_agent_secrets(package, passphrase)
    packaged_keys = set(packaged)
    required = {
        str(item)
        for item in package.access_requirements.get("agent_secret_keys", [])
        if str(item).strip()
    }
    return {
        "required_keys": sorted(required),
        "packaged_keys": sorted(packaged_keys),
        "target_existing_keys": sorted(required & target_keys),
        "missing_keys": sorted(required - target_keys - packaged_keys),
        "shared_credentials_rebind_required": list(
            package.access_requirements.get("target_rebind_required") or []
        ),
    }


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
    replaces_dormant_source: bool = False,
) -> list[str]:
    warnings = [str(item) for item in package.manifest.get("warnings", [])]
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
            "target credentials are incomplete; activation will remain blocked"
        )
    requested = str(
        package.access_requirements.get("access_scope") or "project"
    ).lower()
    maximum = _target_max_access_scope(root)
    if requested in _ACCESS_RANK and _ACCESS_RANK[requested] > _ACCESS_RANK[maximum]:
        warnings.append(
            f"target policy will clamp access_scope from {requested} to {maximum}"
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


def _surgical_target_rollback(root: Path, record: Mapping[str, Any]) -> None:
    package_id = str(record.get("package_id") or "")
    agent_id = str(record.get("agent_id") or "")
    agents_path = root / "agents.json"
    tasks_path = root / "tasks.json"
    secrets_path = root / "secrets.json"
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


def _schedule_state_key(section: str, item: Mapping[str, Any], index: int) -> str:
    task_id = str(item.get("id") or "").strip()
    return f"{section}:{task_id}" if task_id else f"{section}:@{index}"


def _remove_owned_workspace(root: Path, record: Mapping[str, Any]) -> None:
    agent_id = str(record.get("agent_id") or "")
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
    agent_id = str(record.get("agent_id") or "")
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
    for name in ("agents.json", "tasks.json", "secrets.json"):
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


def _restore_recovery_snapshots(root: Path, record_dir: Path) -> None:
    recovery = record_dir / "recovery"
    manifest_path = recovery / "manifest.json"
    if not manifest_path.exists():
        raise AgentMoveError("interrupted commit has no recovery snapshot")
    manifest = _load_json(manifest_path)
    for name, item in (manifest.get("files") or {}).items():
        if name not in {"agents.json", "tasks.json", "secrets.json"}:
            continue
        target = root / name
        if item.get("existed"):
            _atomic_bytes(
                target,
                (recovery / name).read_bytes(),
                mode=0o600 if name == "secrets.json" else None,
            )
        else:
            target.unlink(missing_ok=True)


def _target_max_access_scope(root: Path) -> str:
    try:
        data = _load_json(root / "agents.json")
        global_cfg = data.get("global") if isinstance(data, dict) else {}
        move_cfg = global_cfg.get("agent_move") if isinstance(global_cfg, dict) else {}
        value = str((move_cfg or {}).get("max_access_scope") or "drive").lower()
    except AgentMoveError:
        value = "drive"
    return value if value in VALID_ACCESS_SCOPES else "workspace"


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


def _public_state(record: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "schema_version",
        "package_id",
        "agent_id",
        "source_instance",
        "target_instance",
        "source_environment",
        "target_environment",
        "sha256",
        "package_bytes",
        "package_schema",
        "status",
        "staged_at",
        "committed_at",
        "activated_at",
        "rolled_back_at",
        "workspace",
        "imported_task_ids",
        "credential_status",
        "retained_identity",
        "replaces_dormant_source",
        "dormant_package_id",
        "warnings",
        "reboot_required",
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
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise AgentMoveError(f"invalid JSON file: {path.name}") from exc


def _load_json_or_default(path: Path, default: Any) -> Any:
    if not path.exists():
        return json.loads(json.dumps(default))
    return _load_json(path)


def _atomic_json(path: Path, value: Any, *, mode: int | None = None) -> None:
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_bytes(path, data, mode=mode)


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
    path = _move_root(root) / ".mutation.lock"
    descriptor: int | None = None
    for attempt in range(2):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError as exc:
            if attempt == 0 and _orphaned_mutation_lock(path):
                path.unlink(missing_ok=True)
                continue
            raise AgentMoveError(
                "another Agent move mutation is already in progress"
            ) from exc
    if descriptor is None:  # pragma: no cover - the loop either opens or raises
        raise AgentMoveError("could not acquire the Agent move mutation lock")
    try:
        os.write(descriptor, f"pid={os.getpid()} at={utc_now_iso()}\n".encode())
        os.close(descriptor)
        descriptor = None
        yield
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        path.unlink(missing_ok=True)


def _orphaned_mutation_lock(path: Path) -> bool:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        pid_text = next(
            (
                part.removeprefix("pid=")
                for part in content.split()
                if part.startswith("pid=")
            ),
            "",
        )
        pid = int(pid_text)
    except (OSError, TypeError, ValueError):
        return False
    return not process_is_alive(pid)
