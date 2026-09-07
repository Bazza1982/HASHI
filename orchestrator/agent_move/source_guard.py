"""Source-side quiesce and moved-out guards for Agent cutover."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .package import AgentMoveError, normalize_agent_id, utc_now_iso


def begin_source_quiesce(
    hashi_root: Path | str,
    agent_id: str,
    package_id: str,
    *,
    target_instance: str,
) -> dict[str, Any]:
    root = Path(hashi_root).expanduser().resolve()
    path = _quiesce_path(root, agent_id)
    state = {
        "schema_version": 1,
        "agent_id": normalize_agent_id(agent_id),
        "package_id": str(package_id),
        "target_instance": str(target_instance or "").strip().upper(),
        "status": "cutover_quiesce",
        "created_at": utc_now_iso(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        current = _read_json(path)
        if (
            current.get("package_id") == package_id
            and current.get("agent_id") == state["agent_id"]
        ):
            return current
        raise AgentMoveError(
            f"Agent '{agent_id}' is already quiesced by another move"
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise
    return state


def end_source_quiesce(
    hashi_root: Path | str,
    agent_id: str,
    package_id: str,
) -> None:
    root = Path(hashi_root).expanduser().resolve()
    path = _quiesce_path(root, agent_id)
    if not path.exists():
        return
    current = _read_json(path)
    if current.get("package_id") != package_id:
        raise AgentMoveError("refusing to clear another Agent move quiesce marker")
    path.unlink()


def source_move_guard_state(
    hashi_root: Path | str,
    agent_id: str,
) -> dict[str, Any] | None:
    """Return a safe user-facing block while an Agent is moving or moved out."""

    try:
        root = Path(hashi_root).expanduser().resolve()
        marker = _quiesce_path(root, agent_id)
        if marker.is_file():
            state = _read_json(marker)
            return {
                "status": "cutover_quiesce",
                "target_instance": str(state.get("target_instance") or ""),
            }
        data = _read_json(root / "agents.json", require_object=False)
        rows = data if isinstance(data, list) else data.get("agents", [])
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("name") or row.get("id") or "") != agent_id:
                continue
            if (
                row.get("is_active") is False
                and row.get("transfer_state") == "moved_out_pending_reboot"
            ):
                return {
                    "status": "moved_out_pending_reboot",
                    "target_instance": str(row.get("transfer_target") or ""),
                }
            return None
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    return None


def source_disabled_for_move(
    hashi_root: Path | str,
    agent_id: str,
    package_id: str,
) -> bool:
    root = Path(hashi_root).expanduser().resolve()
    try:
        data = _read_json(root / "agents.json", require_object=False)
        rows = data if isinstance(data, list) else data.get("agents", [])
    except (OSError, UnicodeError, ValueError, TypeError):
        return False
    return any(
        isinstance(row, dict)
        and str(row.get("name") or row.get("id") or "") == agent_id
        and row.get("is_active") is False
        and row.get("transfer_state") == "moved_out_pending_reboot"
        and row.get("transfer_package_id") == package_id
        for row in rows
    )


def _quiesce_path(root: Path, agent_id: str) -> Path:
    name = normalize_agent_id(agent_id)
    return root / "state" / "agent_moves" / "quiesce" / f"{name}.json"


def _read_json(path: Path, *, require_object: bool = True) -> Any:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if require_object and not isinstance(value, dict):
        raise ValueError(f"invalid Agent move guard state: {path.name}")
    if not require_object and not isinstance(value, (dict, list)):
        raise ValueError(f"invalid Agent configuration state: {path.name}")
    return value
