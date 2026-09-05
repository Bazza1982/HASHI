"""Source-side coordinator for two-phase HASHI Agent moves."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from orchestrator.process_execution import process_is_alive
from remote.security.shared_token import load_shared_token

from .package import (
    AgentMoveError,
    archive_snapshot_fingerprint,
    create_agent_move_package,
    normalize_agent_id,
    package_sha256,
    read_agent_move_package,
    utc_now_iso,
)
from .remote_client import AgentMoveRemoteClient, connect_agent_move_receiver
from .service import deactivate_source_agent, restore_source_agent
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
    "cutover_failed",
}


def preview_outbound_move(
    hashi_root: Path | str,
    instances: Mapping[str, Any],
    agent_id: str,
    target_instance: str,
    *,
    source_instance: str,
) -> dict[str, Any]:
    """Validate both sides and build a disposable package; target is untouched."""

    root = Path(hashi_root).expanduser().resolve()
    name = normalize_agent_id(agent_id)
    token = load_shared_token(root)
    client = connect_agent_move_receiver(
        instances,
        target_instance,
        source_instance=source_instance,
        shared_token=token,
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
        )
        exclusion_summary = _exclusion_summary(package.workspace_metadata)
        return {
            "ok": True,
            "preview": True,
            "agent_id": package.agent_id,
            "target_instance": client.target_instance,
            "target_environment": client.capabilities.get("environment_kind"),
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
) -> dict[str, Any]:
    """Build and remotely stage a package, ready for explicit confirmation."""

    root = Path(hashi_root).expanduser().resolve()
    name = normalize_agent_id(agent_id)
    token = load_shared_token(root)
    client = connect_agent_move_receiver(
        instances,
        target_instance,
        source_instance=source_instance,
        shared_token=token,
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
            "status": "packaging",
            "created_at": utc_now_iso(),
        }
        _atomic_json(state_path, state)
    try:
        package = create_agent_move_package(
            root,
            name,
            package_path,
            source_instance=source_instance,
            include_agent_secrets=True,
            secret_passphrase=token,
            package_id=package_id,
            max_package_bytes=_receiver_package_limit(client),
        )
        exclusion_summary = _exclusion_summary(package.workspace_metadata)
        state.update(
            {
                "status": "uploading",
                "sha256": package_sha256(package_path),
                "package_bytes": package_path.stat().st_size,
                "source_environment": package.manifest.get("source_environment"),
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
            }
        )
        _atomic_json(state_path, state)
        remote = client.stage(package_path)
        state.update(
            {
                "status": "staged_remote",
                "staged_at": utc_now_iso(),
                "remote_status": remote.get("status"),
                "credential_status": remote.get("credential_status") or {},
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
    }:
        raise AgentMoveError(
            f"this Agent move cannot be cancelled (status={state.get('status')!r})"
        )
    client = _connect_for_state(root, instances, state)
    source_disabled = bool(
        state.get("source_disabled")
        or source_disabled_for_move(root, str(state.get("agent_id") or ""), package_id)
    )
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
            "reboot_required": False,
        }
    )
    _atomic_json(directory / "state.json", state)
    return _public_outbound(state)


def get_outbound_move(hashi_root: Path | str, package_id: str) -> dict[str, Any]:
    _, state = _load_outbound(Path(hashi_root).expanduser().resolve(), package_id)
    return _public_outbound(state)


def _connect_for_state(
    root: Path,
    instances: Mapping[str, Any],
    state: Mapping[str, Any],
) -> AgentMoveRemoteClient:
    return connect_agent_move_receiver(
        instances,
        str(state.get("target_instance") or ""),
        source_instance=str(state.get("source_instance") or ""),
        shared_token=load_shared_token(root),
    )


def _receiver_package_limit(client: AgentMoveRemoteClient) -> int:
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
    client: AgentMoveRemoteClient,
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


def _public_outbound(state: Mapping[str, Any]) -> dict[str, Any]:
    private = {"last_error", "rollback_errors", "source_snapshot_fingerprint"}
    return {key: value for key, value in state.items() if key not in private} | {
        "ok": True
    }
