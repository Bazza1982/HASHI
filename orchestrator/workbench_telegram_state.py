"""Server-authoritative per-owner Workbench Telegram mirror state.

Owned by the Function layer.  The single persisted fact is whether Runs
submitted by the Workbench (api_chat) for one Session owner mirror their
replies to Telegram.  Defaults to ON.  Clients never declare this value:
admission consults this store server-side, and only the /telegram slash
command (api_chat channel) mutates it.

State file: <bridge_home>/state/workbench_telegram_state.json
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

STATE_FILENAME = "workbench_telegram_state.json"
DEFAULT_MIRROR = True


def state_path(bridge_home: Any) -> Path:
    """Return the canonical state file path for one instance bridge home."""

    return Path(bridge_home) / "state" / STATE_FILENAME


def _empty_state() -> dict[str, Any]:
    return {"revision": 0, "owners": {}}


def _load_state_unsafe(path: Path) -> dict[str, Any]:
    """Read the state file, tolerating a missing or corrupt file.

    Failures degrade to the empty state so admission keeps the historical
    default (mirror on).
    """

    if not path.is_file():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    owners = data.get("owners")
    if not isinstance(owners, dict):
        owners = {}
    normalized_owners = {
        str(owner): bool(value)
        for owner, value in owners.items()
        if str(owner).strip()
    }
    revision = data.get("revision")
    if not isinstance(revision, int) or revision < 0:
        revision = 0
    return {"revision": revision, "owners": normalized_owners}


def load_state(bridge_home: Any) -> dict[str, Any]:
    return _load_state_unsafe(state_path(bridge_home))


def mirror_enabled(
    bridge_home: Any,
    owner_id: Any,
    *,
    default: bool = DEFAULT_MIRROR,
) -> bool:
    """Return the persisted mirror decision for one owner.

    An owner without an explicit entry keeps the default (ON), matching the
    historical non-TUI admission behavior.
    """

    state = load_state(bridge_home)
    return bool(state["owners"].get(str(owner_id), bool(default)))


def _write_state_atomic(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def set_mirror(bridge_home: Any, owner_id: Any, enabled: bool) -> dict[str, Any]:
    """Persist one owner's mirror decision and return the new state snapshot.

    The read-modify-write cycle is guarded by an advisory lock file so
    concurrent /telegram commands from multiple Workers serialize and the
    revision advances monotonically.
    """

    path = state_path(bridge_home)
    lock_path = Path(str(path) + ".lock")
    owner = str(owner_id).strip()
    if not owner:
        raise ValueError("owner_id is required")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            state = _load_state_unsafe(path)
            owners = dict(state["owners"])
            owners[owner] = bool(enabled)
            state["owners"] = owners
            state["revision"] = int(state["revision"]) + 1
            _write_state_atomic(path, state)
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    return state


def parse_mirror_arg(args: Any) -> bool | None:
    """Parse /telegram arguments.

    Returns None for a bare status query, True for on and False for off.
    Anything else raises ValueError with the usage line.
    """

    tokens = [str(token).strip() for token in (args or [])]
    if not tokens:
        return None
    if len(tokens) == 1:
        value = tokens[0].casefold()
        if value == "on":
            return True
        if value == "off":
            return False
    raise ValueError("Usage: /telegram on|off")


__all__ = [
    "DEFAULT_MIRROR",
    "STATE_FILENAME",
    "load_state",
    "mirror_enabled",
    "parse_mirror_arg",
    "set_mirror",
    "state_path",
]
