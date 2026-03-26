from __future__ import annotations
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


HOME_PREFIX = "@home/"


@dataclass(frozen=True)
class BridgePaths:
    code_root: Path
    bridge_home: Path
    config_path: Path
    secrets_path: Path
    tasks_path: Path
    state_path: Path
    lock_path: Path
    pid_path: Path
    workspaces_root: Path


def expand_path_string(value: str) -> str:
    return os.path.expanduser(os.path.expandvars(value.strip()))


def resolve_bridge_home(code_root: Path, override: str | Path | None = None) -> Path:
    raw = str(override).strip() if override is not None else os.environ.get("BRIDGE_HOME", "").strip()
    # Defensively sanitize accidental cmd quoting artifacts.
    # On Windows, %~dp0 ends with \ so "path\" causes \" to be parsed as an
    # escaped quote by the C runtime, potentially swallowing later arguments
    # into this value.  A quote character is never valid in a Windows path, so
    # truncate at the first one.
    if '"' in raw:
        raw = raw[:raw.index('"')]
    raw = raw.strip().rstrip("'")
    if not raw:
        return code_root
    return Path(expand_path_string(raw)).resolve()


def resolve_home_file(
    bridge_home: Path,
    code_root: Path,
    filename: str,
    validator: Optional[Callable[[Path], bool]] = None,
) -> Path:
    home_path = bridge_home / filename
    legacy_path = code_root / filename
    if home_path.exists():
        if validator is None or validator(home_path):
            return home_path
        # File exists but content is invalid — fall through to legacy
    if bridge_home == code_root or not legacy_path.exists():
        return home_path
    return legacy_path


# ── Validators for resolve_home_file ──────────────────────────────

PLACEHOLDER_TOKENS = frozenset({"WORKBENCH_ONLY_NO_TOKEN", ""})


def _secrets_validator(path: Path) -> bool:
    """Return True if secrets.json contains at least one real token."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict) or not data:
        return False
    # Check that at least one agent-level token is real
    skip_keys = {"authorized_telegram_id", "openrouter_key"}
    for key, value in data.items():
        if key in skip_keys:
            continue
        if isinstance(value, str) and value.strip() not in PLACEHOLDER_TOKENS:
            return True
    return False


def _json_validator(path: Path) -> bool:
    """Return True if the file is valid, non-empty JSON."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return False
    if isinstance(data, dict):
        return bool(data)
    return data is not None


def build_bridge_paths(code_root: Path, bridge_home: str | Path | None = None) -> BridgePaths:
    resolved_code_root = code_root.resolve()
    resolved_home = resolve_bridge_home(resolved_code_root, bridge_home)
    return BridgePaths(
        code_root=resolved_code_root,
        bridge_home=resolved_home,
        config_path=resolve_home_file(resolved_home, resolved_code_root, "agents.json", validator=_json_validator),
        secrets_path=resolve_home_file(resolved_home, resolved_code_root, "secrets.json", validator=_secrets_validator),
        tasks_path=resolve_home_file(resolved_home, resolved_code_root, "tasks.json"),
        state_path=resolved_home / "scheduler_state.json",
        lock_path=resolved_home / ".bridge_u_f.lock",
        pid_path=resolved_home / ".bridge_u_f.pid",
        workspaces_root=resolved_home / "workspaces",
    )


def resolve_path_value(
    value: str | Path | None,
    *,
    config_dir: Path,
    bridge_home: Path,
) -> Path | None:
    if value is None:
        return None
    raw = expand_path_string(str(value))
    if not raw:
        return None
    normalized = raw.replace("\\", "/")
    if normalized.startswith(HOME_PREFIX):
        suffix = normalized[len(HOME_PREFIX):]
        return (bridge_home / Path(suffix)).resolve()
    candidate = Path(normalized)
    if candidate.is_absolute():
        return candidate.resolve()
    return (config_dir / candidate).resolve()


def resolve_command_value(
    value: str | None,
    *,
    config_dir: Path,
    bridge_home: Path,
) -> str:
    if value is None:
        return ""
    raw = expand_path_string(str(value))
    if not raw:
        return ""
    normalized = raw.replace("\\", "/")
    looks_like_path = normalized.startswith(HOME_PREFIX) or "/" in raw or "\\" in raw or Path(raw).is_absolute()
    if looks_like_path:
        resolved = resolve_path_value(raw, config_dir=config_dir, bridge_home=bridge_home)
        return str(resolved) if resolved is not None else ""
    # On Windows, asyncio.create_subprocess_exec won't find .cmd/.ps1 wrappers.
    # Use shutil.which() to resolve bare command names to their full path.
    if os.name == "nt":
        full = shutil.which(raw)
        if full:
            return full
    return raw


def to_home_relative(path: str | Path, *, bridge_home: Path) -> str:
    candidate = Path(path).resolve()
    try:
        rel = candidate.relative_to(bridge_home.resolve())
    except ValueError:
        return str(candidate)
    return HOME_PREFIX + rel.as_posix()
