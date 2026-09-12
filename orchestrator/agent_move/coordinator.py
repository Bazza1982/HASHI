"""Source-side coordinator for two-phase HASHI Agent moves."""

from __future__ import annotations

import inspect
import json
import os
import secrets
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError
from uuid import uuid4

from orchestrator.process_execution import process_is_alive
from remote.security.shared_token import load_shared_token
from orchestrator.agent_incarnation import ensure_agent_lifecycle_id

from .package import (
    AgentMoveError,
    archive_snapshot_fingerprint,
    create_agent_move_package,
    normalize_agent_id,
    package_sha256,
    read_agent_move_package,
    utc_now_iso,
)
from .remote_client import connect_agent_move_receiver
from .service import (
    activate_agent_move,
    cleanup_source_agent,
    commit_agent_move,
    finalize_agent_move,
    get_agent_move_status,
    receiver_capabilities,
    resolve_agent_transfer_target,
    rollback_agent_move,
    stage_agent_move,
    deactivate_source_agent,
    restore_source_agent,
)
from .source_guard import (
    begin_source_quiesce,
    end_source_quiesce,
    source_disabled_for_move,
)

_ACTIVE_OUTBOUND_STATUSES = {
    "packaging",
    "uploading",
    "staged_remote",
    "committing_target",
    "disabling_source",
    "moved_pending_reboots",
    "source_disabled_target_committed",
    "activating_target",
    "move_completed_cleanup_pending",
    "clone_activating",
    "clone_completed_cleanup_pending",
    "clone_failed",
    "cutover_failed",
}


class _LocalAgentTransferClient:
    """Use the target-owned service directly for a same-instance clone."""

    def __init__(self, root: Path, instance_id: str, passphrase: str) -> None:
        self.root = root
        self.source_instance = str(instance_id).strip().upper()
        self.target_instance = self.source_instance
        self.shared_token = passphrase
        self.capabilities = receiver_capabilities(root)

    def resolve_target_id(
        self,
        source_agent_id: str,
        *,
        operation: str,
        requested_agent_id: str | None = None,
    ) -> dict[str, Any]:
        return resolve_agent_transfer_target(
            self.root,
            source_agent_id,
            operation=operation,
            requested_agent_id=requested_agent_id,
        )

    def ensure_package_compatible(self, package: Any) -> None:
        schema = int(package.manifest.get("schema_version") or 0)
        if not int(self.capabilities["schema_min"]) <= schema <= int(
            self.capabilities["schema_max"]
        ):
            raise AgentMoveError("local clone receiver does not accept this package")
        advertised = set(self.capabilities.get("capabilities") or [])
        missing = set(package.manifest.get("required_receiver_capabilities") or []) - advertised
        if missing:
            raise AgentMoveError(
                "local clone receiver lacks capabilities: " + ", ".join(sorted(missing))
            )

    def stage(
        self,
        package_path: Path | str,
        *,
        operation: str,
        target_agent_id: str | None = None,
    ) -> dict[str, Any]:
        path = Path(package_path)
        return stage_agent_move(
            self.root,
            path,
            expected_sha256=package_sha256(path),
            source_instance=self.source_instance,
            target_instance=self.target_instance,
            secret_passphrase=self.shared_token,
            operation=operation,
            target_agent_id=target_agent_id,
        )

    def commit(self, package_id: str) -> dict[str, Any]:
        return commit_agent_move(
            self.root,
            package_id,
            secret_passphrase=self.shared_token,
        )

    def activate(self, package_id: str) -> dict[str, Any]:
        return activate_agent_move(self.root, package_id)

    def rollback(self, package_id: str) -> dict[str, Any]:
        return rollback_agent_move(self.root, package_id)

    def status(self, package_id: str) -> dict[str, Any]:
        return get_agent_move_status(self.root, package_id)

    def start(self, package_id: str) -> dict[str, Any]:
        state = self.status(package_id)
        return _local_workbench_lifecycle(
            self.root,
            str(state.get("target_agent_id") or state.get("agent_id") or ""),
            "start",
        )

    def stop(self, package_id: str) -> dict[str, Any]:
        state = self.status(package_id)
        return _local_workbench_lifecycle(
            self.root,
            str(state.get("target_agent_id") or state.get("agent_id") or ""),
            "stop",
        )

    def finalize(self, package_id: str) -> dict[str, Any]:
        state = self.status(package_id)
        target = str(state.get("target_agent_id") or state.get("agent_id") or "")
        return finalize_agent_move(
            self.root,
            package_id,
            runtime_online=target in _local_running_agents(self.root),
        )


def preview_outbound_move(
    hashi_root: Path | str,
    instances: Mapping[str, Any],
    agent_id: str,
    target_instance: str,
    *,
    source_instance: str,
    transfer_mode: str | None = None,
) -> dict[str, Any]:
    """Validate both sides and build a disposable package; target is untouched."""

    return _preview_outbound_transfer(
        hashi_root,
        instances,
        agent_id,
        target_instance,
        source_instance=source_instance,
        operation="move",
        transfer_mode=transfer_mode,
    )


def preview_outbound_clone(
    hashi_root: Path | str,
    instances: Mapping[str, Any],
    agent_id: str,
    target_instance: str,
    *,
    source_instance: str,
    target_agent_id: str | None = None,
    transfer_mode: str | None = "workspace",
) -> dict[str, Any]:
    return _preview_outbound_transfer(
        hashi_root,
        instances,
        agent_id,
        target_instance,
        source_instance=source_instance,
        operation="clone",
        transfer_mode=transfer_mode,
        requested_agent_id=target_agent_id,
    )


def _preview_outbound_transfer(
    hashi_root: Path | str,
    instances: Mapping[str, Any],
    agent_id: str,
    target_instance: str,
    *,
    source_instance: str,
    operation: str,
    requested_agent_id: str | None = None,
    transfer_mode: str | None = None,
) -> dict[str, Any]:

    root = Path(hashi_root).expanduser().resolve()
    name = normalize_agent_id(agent_id)
    if operation == "move":
        _assert_source_can_move(root, name)
    token = _transfer_passphrase(
        root,
        local=str(target_instance).strip().upper()
        == str(source_instance).strip().upper(),
        persist=False,
    )
    client = _connect_transfer_receiver(
        root,
        instances,
        target_instance,
        source_instance=source_instance,
        operation=operation,
        passphrase=token,
    )
    resolved = _resolve_client_target(
        client,
        name,
        operation=operation,
        requested_agent_id=requested_agent_id,
    )
    with tempfile.TemporaryDirectory(prefix="hashi-agent-move-preview-") as temp_name:
        path = Path(temp_name) / f"{name}.hashi-agent"
        package = create_agent_move_package(
            root,
            name,
            path,
            source_instance=source_instance,
            include_agent_secrets=True,
            secret_passphrase=token,
            max_package_bytes=_receiver_package_limit(client),
            operation=operation,
            include_telegram_secret=operation == "move",
            transfer_mode=transfer_mode,
        )
        compatibility_check = getattr(client, "ensure_package_compatible", None)
        if callable(compatibility_check):
            compatibility_check(package)
        exclusion_summary = _exclusion_summary(package.workspace_metadata)
        return {
            "ok": True,
            "preview": True,
            "agent_id": package.agent_id,
            "source_agent_id": package.agent_id,
            "target_agent_id": resolved["target_agent_id"],
            "operation": operation,
            "target_instance": client.target_instance,
            "target_environment": client.capabilities.get("environment_kind"),
            "package_schema": package.manifest.get("schema_version"),
            "transfer_mode": package.manifest.get("transfer_mode"),
            "workspace_inventory": package.workspace_metadata.get("inventory", []),
            "discarded_files": package.workspace_metadata.get("discarded", []),
            "total_workspace_bytes": package.workspace_metadata.get("total_workspace_bytes"),
            "retained_identity": _retained_identity_preview(package.retained_identity),
            "package_bytes": path.stat().st_size,
            "workspace_files": len(package.workspace_metadata.get("files") or []),
            "workspace_source_bytes": int(
                package.workspace_metadata.get("source_bytes") or 0
            ),
            "excluded_count": sum(exclusion_summary.values()),
            "exclusion_reasons": exclusion_summary,
            "schedule_count": _schedule_count(package.schedules),
            "encrypted_agent_secrets": "secrets/agent.enc" in package.names,
            "target_rebind_required": list(
                package.access_requirements.get("target_rebind_required") or []
            ),
            "source_environment": package.manifest.get("source_environment"),
            "warnings": list(package.manifest.get("warnings") or []),
            "receiver": client.capabilities,
        }


def prepare_outbound_move(
    hashi_root: Path | str,
    instances: Mapping[str, Any],
    agent_id: str,
    target_instance: str,
    *,
    source_instance: str,
    keep_source: bool = False,
    transfer_mode: str | None = None,
) -> dict[str, Any]:
    """Build and remotely stage a package, ready for explicit confirmation."""

    return _prepare_outbound_transfer(
        hashi_root,
        instances,
        agent_id,
        target_instance,
        source_instance=source_instance,
        operation="legacy_move" if keep_source else "move",
        transfer_mode=transfer_mode,
        keep_source=keep_source,
    )


def prepare_outbound_clone(
    hashi_root: Path | str,
    instances: Mapping[str, Any],
    agent_id: str,
    target_instance: str,
    *,
    source_instance: str,
    target_agent_id: str | None = None,
    transfer_mode: str | None = "workspace",
) -> dict[str, Any]:
    return _prepare_outbound_transfer(
        hashi_root,
        instances,
        agent_id,
        target_instance,
        source_instance=source_instance,
        operation="clone",
        transfer_mode=transfer_mode,
        requested_agent_id=target_agent_id,
    )


def _prepare_outbound_transfer(
    hashi_root: Path | str,
    instances: Mapping[str, Any],
    agent_id: str,
    target_instance: str,
    *,
    source_instance: str,
    operation: str,
    requested_agent_id: str | None = None,
    keep_source: bool = False,
    transfer_mode: str | None = None,
) -> dict[str, Any]:

    root = Path(hashi_root).expanduser().resolve()
    name = normalize_agent_id(agent_id)
    if operation == "move":
        _assert_source_can_move(root, name)
    local = str(target_instance).strip().upper() == str(source_instance).strip().upper()
    token = _transfer_passphrase(root, local=local, persist=local)
    client = _connect_transfer_receiver(
        root,
        instances,
        target_instance,
        source_instance=source_instance,
        operation=operation,
        passphrase=token,
    )
    resolved = (
        {"target_agent_id": name, "renamed": False}
        if operation == "legacy_move"
        else _resolve_client_target(
            client,
            name,
            operation=operation,
            requested_agent_id=requested_agent_id,
        )
    )
    with _outbound_lock(root):
        _assert_no_active_outbound(root, name)
        package_id = str(uuid4())
        directory = _outbound_dir(root, package_id)
        directory.mkdir(parents=True, exist_ok=False)
        if os.name != "nt":
            directory.chmod(0o700)
        package_path = directory / f"{name}.hashi-agent"
        state_path = directory / "state.json"
        state: dict[str, Any] = {
            "schema_version": 1,
            "package_id": package_id,
            "agent_id": name,
            "source_instance": client.source_instance,
            "target_instance": client.target_instance,
            "keep_source": bool(keep_source),
            "operation": operation,
            "source_agent_id": name,
            "target_agent_id": str(resolved["target_agent_id"]),
            "status": "packaging",
            "created_at": utc_now_iso(),
        }
        _atomic_json(state_path, state)
    try:
        # Preview remains read-only; only an authorized transfer preparation
        # upgrades an older Agent with its stable incarnation identity.
        ensure_agent_lifecycle_id(root / "agents.json", name)
        package = create_agent_move_package(
            root,
            name,
            package_path,
            source_instance=source_instance,
            include_agent_secrets=True,
            secret_passphrase=token,
            package_id=package_id,
            max_package_bytes=_receiver_package_limit(client),
            operation="clone" if operation == "clone" else "move",
            include_telegram_secret=operation != "clone",
            transfer_mode=transfer_mode,
        )
        compatibility_check = getattr(client, "ensure_package_compatible", None)
        if callable(compatibility_check):
            compatibility_check(package)
        exclusion_summary = _exclusion_summary(package.workspace_metadata)
        state.update(
            {
                "status": "uploading",
                "sha256": package_sha256(package_path),
                "package_bytes": package_path.stat().st_size,
                "source_environment": package.manifest.get("source_environment"),
                "package_schema": package.manifest.get("schema_version"),
                "transfer_mode": package.manifest.get("transfer_mode"),
                "workspace_inventory": package.workspace_metadata.get("inventory", []),
                "discarded_files": package.workspace_metadata.get("discarded", []),
                "total_workspace_bytes": package.workspace_metadata.get("total_workspace_bytes"),
                "retained_identity": _retained_identity_preview(
                    package.retained_identity
                ),
                "target_environment": client.capabilities.get("environment_kind"),
                "workspace_files": len(package.workspace_metadata.get("files") or []),
                "workspace_source_bytes": int(
                    package.workspace_metadata.get("source_bytes") or 0
                ),
                "excluded_count": sum(exclusion_summary.values()),
                "exclusion_reasons": exclusion_summary,
                "schedule_count": _schedule_count(package.schedules),
                "encrypted_agent_secrets": "secrets/agent.enc" in package.names,
                "source_snapshot_fingerprint": archive_snapshot_fingerprint(
                    package,
                    secret_passphrase=token,
                ),
                "target_rebind_required": list(
                    package.access_requirements.get("target_rebind_required") or []
                ),
                "source_secret_keys": list(
                    package.access_requirements.get("agent_secret_keys") or []
                ),
            }
        )
        _atomic_json(state_path, state)
        remote = _stage_client(
            client,
            package_path,
            operation=operation,
            target_agent_id=str(resolved["target_agent_id"]),
        )
        remote_target_agent_id = str(
            remote.get("target_agent_id") or resolved["target_agent_id"]
        )
        if remote_target_agent_id != str(resolved["target_agent_id"]):
            raise AgentMoveError(
                "target Agent ID changed between resolution and staging"
            )
        state.update(
            {
                "status": "staged_remote",
                "staged_at": utc_now_iso(),
                "remote_status": remote.get("status"),
                "target_agent_id": remote_target_agent_id,
                "credential_status": remote.get("credential_status") or {},
                "retained_identity": remote.get("retained_identity")
                or state.get("retained_identity"),
                "warnings": list(remote.get("warnings") or []),
            }
        )
        _atomic_json(state_path, state)
        return _public_outbound(state)
    except Exception as exc:
        state["status"] = "prepare_failed"
        state["last_error"] = str(exc)
        _atomic_json(state_path, state)
        raise


def confirm_outbound_move(
    hashi_root: Path | str,
    instances: Mapping[str, Any],
    package_id: str,
) -> dict[str, Any]:
    """Commit target inactive; optionally perform the reversible config cutover."""

    root = Path(hashi_root).expanduser().resolve()
    with _outbound_lock(root):
        return _confirm_outbound_move_locked(root, instances, package_id)


def _confirm_outbound_move_locked(
    root: Path,
    instances: Mapping[str, Any],
    package_id: str,
) -> dict[str, Any]:
    directory, state = _load_outbound(root, package_id)
    operation = _state_operation(state)
    if operation == "clone":
        return _confirm_outbound_clone_locked(
            root,
            instances,
            directory,
            state,
        )
    if operation == "move":
        return _confirm_new_outbound_move_locked(
            root,
            instances,
            directory,
            state,
        )
    if state.get("status") in {"copied_inactive", "moved_pending_reboots"}:
        return _public_outbound(state)
    if state.get("status") == "cutover_failed" and state.get("target_active") is None:
        # A prior target response was uncertain. Re-running confirmation is
        # safe: target commit/activation are idempotent, while a target that
        # actually rolled back will reject recommit and leave the source safe.
        state["status"] = (
            "disabling_source"
            if state.get("source_disabled")
            else "committing_target"
        )
        _atomic_json(directory / "state.json", state)
    if state.get("status") not in {
        "staged_remote",
        "committing_target",
        "disabling_source",
    }:
        raise AgentMoveError(
            f"outbound move is not ready (status={state.get('status')!r})"
        )
    client = _connect_for_state(root, instances, state)
    if state.get("status") == "staged_remote":
        state["status"] = "committing_target"
        _atomic_json(directory / "state.json", state)
    quiesced = False
    try:
        if not state.get("keep_source"):
            begin_source_quiesce(
                root,
                str(state["agent_id"]),
                package_id,
                target_instance=str(state["target_instance"]),
            )
            quiesced = True
        remote = client.commit(package_id)
        state["remote_status"] = remote.get("status")
        state["credential_status"] = remote.get("credential_status") or {}
        state["warnings"] = list(remote.get("warnings") or [])
        if state.get("keep_source"):
            state.update(
                {
                    "status": "copied_inactive",
                    "completed_at": utc_now_iso(),
                    "source_disabled": False,
                    "target_active": False,
                    "reboot_required": False,
                }
            )
            _atomic_json(directory / "state.json", state)
            return _public_outbound(state)

        already_disabled = source_disabled_for_move(
            root,
            str(state["agent_id"]),
            package_id,
        )
        if not already_disabled:
            _assert_source_snapshot_current(root, directory, state, client)
        state["status"] = "disabling_source"
        _atomic_json(directory / "state.json", state)
        if not already_disabled:
            deactivate_source_agent(
                root,
                str(state["agent_id"]),
                package_id,
                target_instance=str(state["target_instance"]),
            )
        state["source_disabled"] = True
        _atomic_json(directory / "state.json", state)
        end_source_quiesce(root, str(state["agent_id"]), package_id)
        quiesced = False
        remote = client.activate(package_id)
        state.update(
            {
                "status": "moved_pending_reboots",
                "remote_status": remote.get("status"),
                "completed_at": utc_now_iso(),
                "target_active": True,
                "reboot_required": True,
                "reboot_order": [
                    str(state["source_instance"]),
                    str(state["target_instance"]),
                ],
            }
        )
        _atomic_json(directory / "state.json", state)
        return _public_outbound(state)
    except Exception as exc:
        rollback_errors: list[str] = []
        target_rolled_back = False
        try:
            client.rollback(package_id)
            target_rolled_back = True
        except Exception as rollback_exc:  # noqa: BLE001 - both rollback legs must be attempted
            rollback_errors.append(f"target rollback failed: {rollback_exc}")

        # Activation may have succeeded even when its HTTP response was lost.
        # Confirm target rollback before restoring an already-disabled source,
        # otherwise the same delivery credential could become active twice.
        source_restored = not bool(state.get("source_disabled"))
        if state.get("source_disabled") and target_rolled_back:
            try:
                restore_source_agent(root, package_id)
                source_restored = True
            except Exception as rollback_exc:  # noqa: BLE001 - preserve the original cutover failure
                rollback_errors.append(f"source restore failed: {rollback_exc}")
        elif state.get("source_disabled"):
            rollback_errors.append(
                "source remains disabled because target rollback was not confirmed"
            )

        if quiesced and source_restored:
            try:
                end_source_quiesce(root, str(state["agent_id"]), package_id)
                quiesced = False
            except Exception as rollback_exc:  # noqa: BLE001 - preserve both failures
                rollback_errors.append(
                    f"source quiesce cleanup failed: {rollback_exc}"
                )
        state.update(
            {
                "status": "cutover_failed",
                "last_error": str(exc),
                "rollback_errors": rollback_errors,
                "source_disabled": not source_restored,
                "target_active": False if target_rolled_back else None,
                "rollback_completed": (
                    target_rolled_back and source_restored and not rollback_errors
                ),
                "reboot_required": bool(rollback_errors or not source_restored),
            }
        )
        _atomic_json(directory / "state.json", state)
        suffix = (
            f"; rollback warning: {'; '.join(rollback_errors)}"
            if rollback_errors
            else ""
        )
        raise AgentMoveError(f"Agent move cutover failed: {exc}{suffix}") from exc


def _confirm_new_outbound_move_locked(
    root: Path,
    instances: Mapping[str, Any],
    directory: Path,
    state: dict[str, Any],
) -> dict[str, Any]:
    if state.get("status") in {
        "source_disabled_target_committed",
        "activating_target",
        "move_completed_cleanup_pending",
        "completed",
    }:
        return _public_outbound(state)
    if state.get("status") not in {
        "staged_remote",
        "committing_target",
        "disabling_source",
        "cutover_failed",
    }:
        raise AgentMoveError(
            f"outbound move is not ready (status={state.get('status')!r})"
        )
    client = _connect_for_state(root, instances, state)
    quiesced = False
    try:
        begin_source_quiesce(
            root,
            str(state["agent_id"]),
            str(state["package_id"]),
            target_instance=str(state["target_instance"]),
        )
        quiesced = True
        if state.get("status") in {"staged_remote", "committing_target", "cutover_failed"}:
            state["status"] = "committing_target"
            _atomic_json(directory / "state.json", state)
            remote = client.commit(str(state["package_id"]))
            state["remote_status"] = remote.get("status")
            state["credential_status"] = remote.get("credential_status") or {}
            state["warnings"] = list(remote.get("warnings") or [])

        already_disabled = source_disabled_for_move(
            root,
            str(state["agent_id"]),
            str(state["package_id"]),
        )
        if not already_disabled:
            _assert_source_snapshot_current(root, directory, state, client)
            state["status"] = "disabling_source"
            _atomic_json(directory / "state.json", state)
            deactivate_source_agent(
                root,
                str(state["agent_id"]),
                str(state["package_id"]),
                target_instance=str(state["target_instance"]),
            )
        state.update(
            {
                "status": "source_disabled_target_committed",
                "source_disabled": True,
                "target_active": False,
                "source_connector_stop_required": True,
                "continuation_required": True,
                "reboot_required": True,
                "reboot_order": [str(state["source_instance"])],
            }
        )
        _atomic_json(directory / "state.json", state)
        end_source_quiesce(root, str(state["agent_id"]), str(state["package_id"]))
        quiesced = False
        return _public_outbound(state)
    except Exception as exc:
        _rollback_new_move_cutover(
            root,
            client,
            directory,
            state,
            exc,
            quiesced=quiesced,
        )
        raise  # pragma: no cover - helper always raises


def _confirm_outbound_clone_locked(
    root: Path,
    instances: Mapping[str, Any],
    directory: Path,
    state: dict[str, Any],
) -> dict[str, Any]:
    if state.get("status") == "completed":
        return _public_outbound(state)
    if state.get("status") == "clone_completed_cleanup_pending":
        _remove_outbound_package(directory, state)
        state.update(
            {
                "status": "completed",
                "completed_at": state.get("completed_at") or utc_now_iso(),
                "source_disabled": False,
                "target_active": True,
                "target_verified": True,
                "reboot_required": False,
            }
        )
        state.pop("last_error", None)
        _atomic_json(directory / "state.json", state)
        return _public_outbound(state)
    if state.get("status") not in {
        "staged_remote",
        "committing_target",
        "clone_activating",
    }:
        raise AgentMoveError(
            f"outbound clone is not ready (status={state.get('status')!r})"
        )
    client = _connect_for_state(root, instances, state)
    target_finalized = False
    target_start_attempted = False
    try:
        if state.get("status") in {"staged_remote", "committing_target"}:
            state["status"] = "committing_target"
            _atomic_json(directory / "state.json", state)
            remote = client.commit(str(state["package_id"]))
            state["remote_status"] = remote.get("status")
            state["credential_status"] = remote.get("credential_status") or {}
        state["status"] = "clone_activating"
        _atomic_json(directory / "state.json", state)
        client.activate(str(state["package_id"]))
        state["target_active"] = True
        _atomic_json(directory / "state.json", state)
        target_start_attempted = True
        client.start(str(state["package_id"]))
        remote = client.finalize(str(state["package_id"]))
        target_finalized = remote.get("status") == "completed"
        if not target_finalized:
            raise AgentMoveError("target clone did not complete final verification")
        _remove_outbound_package(directory, state)
        state.update(
            {
                "status": "completed",
                "remote_status": remote.get("status"),
                "target_verified": True,
                "target_verification": remote.get("target_verification") or {},
                "completed_at": utc_now_iso(),
                "source_disabled": False,
                "target_active": True,
                "reboot_required": False,
            }
        )
        _atomic_json(directory / "state.json", state)
        return _public_outbound(state)
    except Exception as exc:
        target_status: dict[str, Any] = {}
        try:
            target_status = client.status(str(state["package_id"]))
            target_finalized = target_status.get("status") == "completed"
        except Exception:
            pass
        if target_finalized:
            try:
                _remove_outbound_package(directory, state)
                state.update(
                    {
                        "status": "completed",
                        "remote_status": "completed",
                        "target_verified": True,
                        "target_verification": target_status.get(
                            "target_verification"
                        )
                        or state.get("target_verification")
                        or {},
                        "completed_at": utc_now_iso(),
                        "source_disabled": False,
                        "target_active": True,
                        "reboot_required": False,
                    }
                )
                state.pop("last_error", None)
                _atomic_json(directory / "state.json", state)
                return _public_outbound(state)
            except Exception as cleanup_exc:
                state.update(
                    {
                        "status": "clone_completed_cleanup_pending",
                        "last_error": str(cleanup_exc),
                        "target_active": True,
                        "target_verified": True,
                        "source_disabled": False,
                        "reboot_required": False,
                    }
                )
                _atomic_json(directory / "state.json", state)
                raise AgentMoveError(
                    "Agent clone completed but local transaction cleanup is "
                    f"pending: {cleanup_exc}"
                ) from cleanup_exc
        if target_status.get("status") == "target_cleanup_pending":
            state.update(
                {
                    "status": "clone_activating",
                    "remote_status": "target_cleanup_pending",
                    "last_error": str(exc),
                    "target_active": True,
                    "target_verified": bool(target_status.get("target_verified")),
                    "source_disabled": False,
                    "reboot_required": False,
                }
            )
            _atomic_json(directory / "state.json", state)
            raise AgentMoveError(
                "Agent clone target is verified but final cleanup is pending; "
                "retry confirmation"
            ) from exc
        if not target_finalized:
            rollback_errors: list[str] = []
            target_stop_confirmed = True
            if (
                target_start_attempted
                or state.get("target_active")
                or target_status.get("status") == "activated_pending_reboot"
            ):
                try:
                    client.stop(str(state["package_id"]))
                except Exception as stop_exc:  # noqa: BLE001
                    target_stop_confirmed = False
                    rollback_errors.append(f"target stop failed: {stop_exc}")
            if target_stop_confirmed:
                try:
                    client.rollback(str(state["package_id"]))
                except Exception as rollback_exc:  # noqa: BLE001
                    rollback_errors.append(f"target rollback failed: {rollback_exc}")
            state.update(
                {
                    "status": "clone_failed",
                    "last_error": str(exc),
                    "rollback_errors": rollback_errors,
                    "source_disabled": False,
                    "target_active": None if rollback_errors else False,
                    "rollback_completed": not rollback_errors,
                    "target_stop_required": not target_stop_confirmed,
                    "reboot_required": bool(rollback_errors),
                }
            )
            _atomic_json(directory / "state.json", state)
            suffix = (
                f"; rollback warning: {'; '.join(rollback_errors)}"
                if rollback_errors
                else ""
            )
            raise AgentMoveError(f"Agent clone failed: {exc}{suffix}") from exc
def continue_outbound_move(
    hashi_root: Path | str,
    instances: Mapping[str, Any],
    package_id: str,
) -> dict[str, Any]:
    """Finish a move only after the source connector is demonstrably stopped."""

    root = Path(hashi_root).expanduser().resolve()
    with _outbound_lock(root):
        directory, state = _load_outbound(root, package_id)
        if _state_operation(state) not in {"move", "legacy_move"}:
            raise AgentMoveError("this transaction is not an Agent move")
        if state.get("status") == "completed":
            return _public_outbound(state)
        if state.get("status") == "move_completed_cleanup_pending":
            return _finish_source_cleanup(root, directory, state)
        if state.get("status") not in {
            "source_disabled_target_committed",
            "activating_target",
            "moved_pending_reboots",
        }:
            raise AgentMoveError(
                f"outbound move cannot continue from status {state.get('status')!r}"
            )
        source_agent = str(state.get("source_agent_id") or state.get("agent_id") or "")
        if source_agent in _local_running_agents(root):
            raise AgentMoveError(
                f"source Agent '{source_agent}' is still running; stop it or adopt "
                "the inactive registry change before continuing"
            )

        client = _connect_for_state(root, instances, state)
        target_finalized = False
        target_start_attempted = False
        try:
            state["status"] = "activating_target"
            _atomic_json(directory / "state.json", state)
            if not state.get("target_active"):
                remote = client.activate(package_id)
                state["remote_status"] = remote.get("status")
                state["target_active"] = True
                _atomic_json(directory / "state.json", state)
            target_start_attempted = True
            client.start(package_id)
            remote = client.finalize(package_id)
            target_finalized = remote.get("status") == "completed"
            if not target_finalized:
                raise AgentMoveError("target Agent did not complete final verification")
            state.update(
                {
                    "remote_status": remote.get("status"),
                    "target_verified": True,
                    "target_verification": remote.get("target_verification") or {},
                    "target_active": True,
                }
            )
            _atomic_json(directory / "state.json", state)
            return _finish_source_cleanup(root, directory, state)
        except Exception as exc:
            target_status: dict[str, Any] = {}
            if not target_finalized:
                try:
                    target_status = client.status(package_id)
                    target_finalized = target_status.get("status") == "completed"
                except Exception:
                    pass
            if target_finalized:
                state.update(
                    {
                        "status": "move_completed_cleanup_pending",
                        "last_error": str(exc),
                        "target_active": True,
                        "source_disabled": True,
                        "reboot_required": False,
                    }
                )
                _atomic_json(directory / "state.json", state)
                raise AgentMoveError(
                    f"Agent move completed but source cleanup is pending: {exc}"
                ) from exc
            if target_status.get("status") == "target_cleanup_pending":
                state.update(
                    {
                        "status": "activating_target",
                        "remote_status": "target_cleanup_pending",
                        "last_error": str(exc),
                        "target_active": True,
                        "target_verified": bool(
                            target_status.get("target_verified")
                        ),
                        "source_disabled": True,
                        "reboot_required": False,
                    }
                )
                _atomic_json(directory / "state.json", state)
                raise AgentMoveError(
                    "Agent move target is verified but final cleanup is pending; "
                    "retry continuation"
                ) from exc

            rollback_errors: list[str] = []
            target_stop_confirmed = True
            if (
                target_start_attempted
                or state.get("target_active")
                or target_status.get("status") == "activated_pending_reboot"
            ):
                try:
                    client.stop(package_id)
                except Exception as stop_exc:  # noqa: BLE001
                    target_stop_confirmed = False
                    rollback_errors.append(f"target stop failed: {stop_exc}")
            target_rolled_back = False
            if target_stop_confirmed:
                try:
                    client.rollback(package_id)
                    target_rolled_back = True
                except Exception as rollback_exc:  # noqa: BLE001
                    rollback_errors.append(f"target rollback failed: {rollback_exc}")
            source_restored = False
            if target_rolled_back and target_stop_confirmed:
                try:
                    restore_source_agent(root, package_id)
                    source_restored = True
                    if source_agent not in _local_running_agents(root):
                        _local_workbench_lifecycle(root, source_agent, "start")
                        if source_agent not in _local_running_agents(root):
                            raise AgentMoveError(
                                "source Agent registry was restored but its Worker "
                                "did not come online"
                            )
                except Exception as restore_exc:  # noqa: BLE001
                    rollback_errors.append(f"source restore failed: {restore_exc}")
            else:
                rollback_errors.append(
                    "source remains disabled because target rollback was not confirmed"
                )
            state.update(
                {
                    "status": "cutover_failed",
                    "last_error": str(exc),
                    "rollback_errors": rollback_errors,
                    "source_disabled": not source_restored,
                    "target_active": False if target_rolled_back else None,
                    "rollback_completed": (
                        target_rolled_back and source_restored and not rollback_errors
                    ),
                    "reboot_required": bool(
                        rollback_errors or not source_restored
                    ),
                }
            )
            _atomic_json(directory / "state.json", state)
            suffix = (
                f"; rollback warning: {'; '.join(rollback_errors)}"
                if rollback_errors
                else ""
            )
            raise AgentMoveError(f"Agent move activation failed: {exc}{suffix}") from exc


def _finish_source_cleanup(
    root: Path,
    directory: Path,
    state: dict[str, Any],
) -> dict[str, Any]:
    try:
        cleanup_source_agent(
            root,
            str(state["package_id"]),
            source_secret_keys=tuple(state.get("source_secret_keys") or ()),
            target_instance=str(state["target_instance"]),
            target_agent_id=str(
                state.get("target_agent_id") or state.get("agent_id") or ""
            ),
        )
        _remove_outbound_package(directory, state)
    except Exception as exc:
        state.update(
            {
                "status": "move_completed_cleanup_pending",
                "last_error": str(exc),
                "source_disabled": True,
                "target_active": True,
                "reboot_required": False,
            }
        )
        _atomic_json(directory / "state.json", state)
        raise AgentMoveError(
            f"Agent move completed but source cleanup is pending: {exc}"
        ) from exc
    state.update(
        {
            "status": "completed",
            "completed_at": utc_now_iso(),
            "source_disabled": True,
            "source_removed": True,
            "target_active": True,
            "reboot_required": False,
            "continuation_required": False,
        }
    )
    for key in ("last_error", "rollback_errors", "source_snapshot_fingerprint"):
        state.pop(key, None)
    _atomic_json(directory / "state.json", state)
    return _public_outbound(state)


def _rollback_new_move_cutover(
    root: Path,
    client: Any,
    directory: Path,
    state: dict[str, Any],
    exc: Exception,
    *,
    quiesced: bool,
) -> None:
    rollback_errors: list[str] = []
    target_rolled_back = False
    try:
        client.rollback(str(state["package_id"]))
        target_rolled_back = True
    except Exception as rollback_exc:  # noqa: BLE001
        rollback_errors.append(f"target rollback failed: {rollback_exc}")
    source_disabled = bool(
        state.get("source_disabled")
        or source_disabled_for_move(
            root,
            str(state.get("agent_id") or ""),
            str(state.get("package_id") or ""),
        )
    )
    source_restored = not source_disabled
    if source_disabled and target_rolled_back:
        try:
            restore_source_agent(root, str(state["package_id"]))
            source_restored = True
        except Exception as restore_exc:  # noqa: BLE001
            rollback_errors.append(f"source restore failed: {restore_exc}")
    elif source_disabled:
        rollback_errors.append(
            "source remains disabled because target rollback was not confirmed"
        )
    if quiesced and source_restored:
        try:
            end_source_quiesce(
                root,
                str(state["agent_id"]),
                str(state["package_id"]),
            )
        except Exception as quiesce_exc:  # noqa: BLE001
            rollback_errors.append(f"source quiesce cleanup failed: {quiesce_exc}")
    state.update(
        {
            "status": "cutover_failed",
            "last_error": str(exc),
            "rollback_errors": rollback_errors,
            "source_disabled": not source_restored,
            "target_active": False if target_rolled_back else None,
            "rollback_completed": (
                target_rolled_back and source_restored and not rollback_errors
            ),
            "reboot_required": bool(rollback_errors or not source_restored),
        }
    )
    _atomic_json(directory / "state.json", state)
    suffix = (
        f"; rollback warning: {'; '.join(rollback_errors)}"
        if rollback_errors
        else ""
    )
    raise AgentMoveError(f"Agent move cutover failed: {exc}{suffix}") from exc


def cancel_outbound_move(
    hashi_root: Path | str,
    instances: Mapping[str, Any],
    package_id: str,
) -> dict[str, Any]:
    root = Path(hashi_root).expanduser().resolve()
    with _outbound_lock(root):
        return _cancel_outbound_move_locked(root, instances, package_id)


def _cancel_outbound_move_locked(
    root: Path,
    instances: Mapping[str, Any],
    package_id: str,
) -> dict[str, Any]:
    directory, state = _load_outbound(root, package_id)
    if state.get("status") == "cancelled":
        return _public_outbound(state)
    if state.get("status") not in {
        "staged_remote",
        "committing_target",
        "disabling_source",
        "cutover_failed",
        "copied_inactive",
        "source_disabled_target_committed",
        "activating_target",
        "clone_activating",
        "clone_failed",
    }:
        raise AgentMoveError(
            f"this Agent move cannot be cancelled (status={state.get('status')!r})"
        )
    client = _connect_for_state(root, instances, state)
    source_disabled = bool(
        state.get("source_disabled")
        or source_disabled_for_move(root, str(state.get("agent_id") or ""), package_id)
    )
    target_stop_required = bool(
        state.get("target_stop_required")
        or state.get("target_active") is True
        or state.get("status") in {"activating_target", "clone_activating"}
        or (source_disabled and state.get("target_active") is not False)
    )
    if target_stop_required:
        try:
            client.stop(package_id)
            state["target_stop_required"] = False
        except Exception as exc:
            state.update(
                {
                    "status": "cutover_failed",
                    "last_error": str(exc),
                    "rollback_errors": [f"target stop failed: {exc}"],
                    "source_disabled": source_disabled,
                    "target_active": None,
                    "target_stop_required": True,
                    "reboot_required": source_disabled,
                }
            )
            _atomic_json(directory / "state.json", state)
            raise AgentMoveError(
                "Agent transfer cancellation is waiting for confirmed target "
                f"stop: {exc}"
            ) from exc
    try:
        client.rollback(package_id)
    except Exception as exc:
        state.update(
            {
                "status": "cutover_failed",
                "last_error": str(exc),
                "rollback_errors": [f"target rollback failed: {exc}"],
                "source_disabled": source_disabled,
                "target_active": None,
                "reboot_required": source_disabled,
            }
        )
        _atomic_json(directory / "state.json", state)
        suffix = (
            "; source remains disabled until target rollback can be confirmed"
            if source_disabled
            else ""
        )
        raise AgentMoveError(f"Agent move cancellation failed: {exc}{suffix}") from exc

    if source_disabled:
        try:
            restore_source_agent(root, package_id)
            source_disabled = False
            source_agent = str(
                state.get("source_agent_id") or state.get("agent_id") or ""
            )
            if source_agent not in _local_running_agents(root):
                _local_workbench_lifecycle(root, source_agent, "start")
                if source_agent not in _local_running_agents(root):
                    raise AgentMoveError(
                        "source Agent registry was restored but its Worker did not "
                        "come online"
                    )
        except Exception as exc:
            state.update(
                {
                    "status": "cutover_failed",
                    "last_error": str(exc),
                    "rollback_errors": [f"source restore failed: {exc}"],
                    "source_disabled": True,
                    "target_active": False,
                    "reboot_required": True,
                }
            )
            _atomic_json(directory / "state.json", state)
            raise AgentMoveError(
                f"target rolled back but source restoration failed: {exc}"
            ) from exc
    try:
        end_source_quiesce(root, str(state.get("agent_id") or ""), package_id)
    except Exception as exc:
        state.update(
            {
                "status": "cutover_failed",
                "last_error": str(exc),
                "rollback_errors": [f"source quiesce cleanup failed: {exc}"],
                "source_disabled": False,
                "target_active": False,
                "reboot_required": True,
            }
        )
        _atomic_json(directory / "state.json", state)
        raise AgentMoveError(f"source quiesce cleanup failed: {exc}") from exc
    state.update(
        {
            "status": "cancelled",
            "cancelled_at": utc_now_iso(),
            "remote_status": "rolled_back",
            "source_disabled": False,
            "target_active": False,
            "rollback_errors": [],
            "target_stop_required": False,
            "reboot_required": False,
        }
    )
    _atomic_json(directory / "state.json", state)
    return _public_outbound(state)


def get_outbound_move(hashi_root: Path | str, package_id: str) -> dict[str, Any]:
    _, state = _load_outbound(Path(hashi_root).expanduser().resolve(), package_id)
    return _public_outbound(state)


def _state_operation(state: Mapping[str, Any]) -> str:
    operation = str(state.get("operation") or "legacy_move").strip().lower()
    if operation not in {"legacy_move", "move", "clone"}:
        raise AgentMoveError(f"invalid outbound transfer operation {operation!r}")
    return operation


def _assert_source_can_move(root: Path, agent_id: str) -> None:
    try:
        data = json.loads((root / "agents.json").read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise AgentMoveError("source agents.json cannot be read") from exc
    rows = data if isinstance(data, list) else data.get("agents", [])
    active = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and row.get("is_active", True) is not False
        and row.get("transfer_state") != "moved_out_pending_reboot"
    ]
    selected = next(
        (
            row
            for row in active
            if str(row.get("name") or row.get("id") or "") == agent_id
        ),
        None,
    )
    if selected is None:
        raise AgentMoveError(f"source Agent '{agent_id}' is not active")
    if len(active) <= 1:
        raise AgentMoveError(
            f"cannot move Agent '{agent_id}': it is the source instance's last "
            "active Agent; create or clone another Agent here first"
        )


def _connect_transfer_receiver(
    root: Path,
    instances: Mapping[str, Any],
    target_instance: str,
    *,
    source_instance: str,
    operation: str,
    passphrase: str,
) -> Any:
    target = str(target_instance or "").strip().upper()
    source = str(source_instance or "").strip().upper()
    if target == source:
        if operation != "clone":
            raise AgentMoveError("source and target HASHI instances must be different")
        return _LocalAgentTransferClient(root, source, passphrase)
    return connect_agent_move_receiver(
        instances,
        target,
        source_instance=source,
        shared_token=passphrase,
    )


def _resolve_client_target(
    client: Any,
    source_agent_id: str,
    *,
    operation: str,
    requested_agent_id: str | None,
) -> dict[str, Any]:
    resolver = getattr(client, "resolve_target_id", None)
    if callable(resolver):
        result = resolver(
            source_agent_id,
            operation=operation,
            requested_agent_id=requested_agent_id,
        )
        if not isinstance(result, Mapping):
            raise AgentMoveError("target Agent ID resolver returned invalid data")
        selected = normalize_agent_id(str(result.get("target_agent_id") or ""))
        return {**dict(result), "target_agent_id": selected}
    selected = normalize_agent_id(requested_agent_id or source_agent_id)
    return {
        "ok": True,
        "operation": operation,
        "source_agent_id": source_agent_id,
        "target_agent_id": selected,
        "renamed": selected != source_agent_id,
    }


def _stage_client(
    client: Any,
    package_path: Path,
    *,
    operation: str,
    target_agent_id: str,
) -> dict[str, Any]:
    parameters = inspect.signature(client.stage).parameters
    if "operation" in parameters:
        return client.stage(
            package_path,
            operation=operation,
            target_agent_id=target_agent_id,
        )
    return client.stage(package_path)


def _transfer_passphrase(root: Path, *, local: bool, persist: bool) -> str:
    shared = str(load_shared_token(root) or "").strip()
    if shared:
        return shared
    if not local:
        raise AgentMoveError(
            "HASHI Remote shared-token pairing is required for Agent transfers"
        )
    if not persist:
        return secrets.token_urlsafe(48)
    path = root / "state" / "agent_moves" / "local_transfer.key"
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(48)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        existing = path.read_text(encoding="utf-8").strip()
        if not existing:
            raise AgentMoveError("local Agent transfer key is empty")
        return existing
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return value


def _local_workbench_base_url(root: Path) -> str:
    endpoint_path = root / "state" / "service_endpoints.json"
    try:
        endpoint_data = json.loads(endpoint_path.read_text(encoding="utf-8-sig"))
        workbench = (endpoint_data.get("services") or {}).get("workbench") or {}
        base_url = str(workbench.get("base_url") or "").strip()
        if base_url:
            return base_url.rstrip("/")
    except Exception:
        pass
    data = json.loads((root / "agents.json").read_text(encoding="utf-8-sig"))
    port = int((data.get("global") or {}).get("workbench_port") or 18801)
    return f"http://127.0.0.1:{port}"


def _local_workbench_json(
    root: Path,
    path: str,
    *,
    method: str = "GET",
    payload: Mapping[str, Any] | None = None,
    timeout: float = 30,
) -> dict[str, Any]:
    data = (
        json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
        if payload is not None
        else None
    )
    headers = {"Content-Type": "application/json"} if data is not None else {}
    try:
        secret_data = json.loads((root / "secrets.json").read_text(encoding="utf-8-sig"))
    except Exception:
        secret_data = {}
    admin_token = str((secret_data or {}).get("workbench_admin_token") or "")
    if admin_token:
        headers["X-Workbench-Token"] = admin_token
    url = f"{_local_workbench_base_url(root)}{path}"
    request = urllib_request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            result = json.loads(exc.read().decode("utf-8"))
        except Exception:
            result = {"error": str(exc)}
        message = str(result.get("message") or result.get("error") or exc)
        if (method == "POST" and "already" in message.lower()) or (
            method == "POST" and "not running" in message.lower()
        ):
            return {"ok": True, "message": message}
        raise AgentMoveError(f"local Workbench request failed: {message}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise AgentMoveError(f"local Workbench is unavailable: {exc}") from exc
    if not isinstance(result, dict) or result.get("ok") is False:
        raise AgentMoveError(
            "local Workbench request failed: "
            + str(result.get("error") if isinstance(result, dict) else result)
        )
    return result


def _local_workbench_lifecycle(root: Path, agent_id: str, action: str) -> dict[str, Any]:
    from orchestrator.agent_lifecycle import AGENT_LIFECYCLE_REQUEST_TIMEOUT_SECONDS

    if action not in {"start", "stop"}:
        raise AgentMoveError("unsupported local Agent lifecycle action")
    return _local_workbench_json(
        root,
        f"/api/admin/{action}-agent",
        method="POST",
        payload={"agent": agent_id},
        timeout=AGENT_LIFECYCLE_REQUEST_TIMEOUT_SECONDS,
    )


def _local_running_agents(root: Path) -> set[str]:
    health = _local_workbench_json(root, "/api/health")
    return {str(item) for item in health.get("agents", []) or [] if str(item)}


def _remove_outbound_package(directory: Path, state: Mapping[str, Any]) -> None:
    agent_id = str(state.get("source_agent_id") or state.get("agent_id") or "")
    package_path = directory / f"{agent_id}.hashi-agent"
    package_path.unlink(missing_ok=True)


def _connect_for_state(
    root: Path,
    instances: Mapping[str, Any],
    state: Mapping[str, Any],
) -> Any:
    target = str(state.get("target_instance") or "")
    source = str(state.get("source_instance") or "")
    local = target.strip().upper() == source.strip().upper()
    return _connect_transfer_receiver(
        root,
        instances,
        target,
        source_instance=source,
        operation=_state_operation(state),
        passphrase=_transfer_passphrase(root, local=local, persist=local),
    )


def _receiver_package_limit(client: Any) -> int:
    try:
        limit = int(client.capabilities.get("max_package_bytes") or 0)
    except (TypeError, ValueError) as exc:
        raise AgentMoveError(
            "target receiver reported an invalid package limit"
        ) from exc
    if limit <= 0:
        raise AgentMoveError("target receiver did not report a usable package limit")
    return limit


def _assert_source_snapshot_current(
    root: Path,
    directory: Path,
    state: Mapping[str, Any],
    client: Any,
) -> None:
    token = load_shared_token(root)
    if not token:
        raise AgentMoveError(
            "HASHI Remote shared-token pairing disappeared before cutover"
        )
    agent_id = str(state.get("agent_id") or "")
    original_path = directory / f"{agent_id}.hashi-agent"
    original = read_agent_move_package(original_path, verify=True)
    expected = str(state.get("source_snapshot_fingerprint") or "") or (
        archive_snapshot_fingerprint(original, secret_passphrase=token)
    )
    with tempfile.TemporaryDirectory(prefix="hashi-agent-move-freshness-") as name:
        current_path = Path(name) / f"{agent_id}.hashi-agent"
        current = create_agent_move_package(
            root,
            agent_id,
            current_path,
            source_instance=str(state.get("source_instance") or ""),
            include_agent_secrets=True,
            secret_passphrase=token,
            max_package_bytes=_receiver_package_limit(client),
            operation=str(original.manifest.get("operation") or "move"),
            include_telegram_secret=True,
            schema_version=int(original.manifest.get("schema_version") or 1),
            transfer_mode=original.manifest.get("transfer_mode"),
        )
        actual = archive_snapshot_fingerprint(current, secret_passphrase=token)
    if actual != expected:
        raise AgentMoveError(
            "source Agent durable state changed after staging; prepare a fresh move "
            "so no newer memory or configuration is lost"
        )


def _assert_no_active_outbound(root: Path, agent_id: str) -> None:
    outbound = root / "state" / "agent_moves" / "outbound"
    if not outbound.is_dir():
        return
    folded = agent_id.casefold()
    for state_path in sorted(outbound.glob("*/state.json")):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(state, dict):
            continue
        if str(state.get("agent_id") or "").casefold() != folded:
            continue
        status = str(state.get("status") or "")
        if status in _ACTIVE_OUTBOUND_STATUSES:
            if status == "cutover_failed" and not (
                state.get("rollback_errors")
                or state.get("source_disabled")
                or state.get("reboot_required")
            ):
                continue
            package_id = str(state.get("package_id") or state_path.parent.name)
            raise AgentMoveError(
                f"Agent '{agent_id}' already has an unfinished outbound move "
                f"{package_id} (status={status})"
            )


def _outbound_dir(root: Path, package_id: str) -> Path:
    if not package_id or any(
        character
        not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
        for character in package_id
    ):
        raise AgentMoveError("invalid outbound Agent move package_id")
    return root / "state" / "agent_moves" / "outbound" / package_id


def _load_outbound(root: Path, package_id: str) -> tuple[Path, dict[str, Any]]:
    directory = _outbound_dir(root, package_id)
    state_path = directory / "state.json"
    if not state_path.is_file():
        raise AgentMoveError("outbound Agent move state was not found")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AgentMoveError("outbound Agent move state is invalid") from exc
    if not isinstance(state, dict) or state.get("package_id") != package_id:
        raise AgentMoveError("outbound Agent move state does not match package_id")
    return directory, state


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, path)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


@contextmanager
def _outbound_lock(root: Path) -> Iterator[None]:
    lock_dir = root / "state" / "agent_moves"
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / ".outbound.lock"
    descriptor: int | None = None
    for attempt in range(2):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError as exc:
            if attempt == 0 and _orphaned_lock(path):
                path.unlink(missing_ok=True)
                continue
            raise AgentMoveError(
                "another outbound Agent move operation is already in progress"
            ) from exc
    if descriptor is None:  # pragma: no cover - the loop either opens or raises
        raise AgentMoveError("could not acquire the outbound Agent move lock")
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


def _orphaned_lock(path: Path) -> bool:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        match = next(
            (
                part.removeprefix("pid=")
                for part in content.split()
                if part.startswith("pid=")
            ),
            "",
        )
        pid = int(match)
    except (OSError, TypeError, ValueError):
        return False
    return not process_is_alive(pid)


def _schedule_count(schedules: Mapping[str, Any]) -> int:
    return sum(
        len(schedules.get(section) or [])
        for section in ("heartbeats", "crons", "nudges")
    )


def _exclusion_summary(workspace_metadata: Mapping[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in workspace_metadata.get("excluded", []) or []:
        if not isinstance(item, Mapping):
            continue
        reason = str(item.get("reason") or "unspecified")
        result[reason] = result.get(reason, 0) + 1
    return dict(sorted(result.items()))


def _retained_identity_preview(
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if metadata is None:
        return None
    return {
        **dict(metadata),
        "target_policy": (
            "persistent transaction attachment; never installed as live PCM"
        ),
    }


def _public_outbound(state: Mapping[str, Any]) -> dict[str, Any]:
    private = {
        "last_error",
        "rollback_errors",
        "source_snapshot_fingerprint",
        "source_secret_keys",
    }
    return {key: value for key, value in state.items() if key not in private} | {
        "ok": True
    }
