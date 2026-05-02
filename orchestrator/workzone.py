from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any


STATE_FILENAME = "workzone.json"
logger = logging.getLogger("Bridge.Workzone")


_WINDOWS_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")
_WSL_UNC_RE = re.compile(r"^\\\\(?:wsl\$|wsl\.localhost)\\[^\\]+\\(.*)$", re.IGNORECASE)


def state_path(workspace_dir: Path) -> Path:
    return workspace_dir / STATE_FILENAME


def load_workzone(workspace_dir: Path) -> Path | None:
    path = state_path(workspace_dir)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        value = str(raw.get("path") or "").strip()
        if not value:
            return None
        zone = Path(value).expanduser().resolve()
        return zone if zone.is_dir() else None
    except Exception as exc:
        logger.warning("Failed to load workzone state from %s: %s", path, exc)
        return None


def _normalize_workzone_input(raw_path: str) -> Path:
    raw = str(raw_path or "").strip()
    drive_match = _WINDOWS_DRIVE_RE.match(raw)
    if drive_match:
        drive = drive_match.group(1).lower()
        rest = drive_match.group(2).replace("\\", "/")
        return Path("/mnt") / drive / rest
    unc_match = _WSL_UNC_RE.match(raw)
    if unc_match:
        return Path("/") / unc_match.group(1).replace("\\", "/")
    return Path(raw.replace("\\", "/")).expanduser()


def resolve_workzone_input(raw_path: str, project_root: Path, workspace_dir: Path) -> Path:
    raw = (raw_path or "").strip()
    if not raw:
        raise ValueError("missing path")
    candidate = _normalize_workzone_input(raw)
    was_relative = not candidate.is_absolute()
    if was_relative:
        candidate = (project_root / candidate).resolve()
    else:
        candidate = candidate.resolve()
    if candidate.is_file():
        raise ValueError(f"path is a file, not a directory: {candidate}")
    if not candidate.is_dir():
        raise ValueError(f"path is not an existing directory: {candidate}")
    if was_relative:
        try:
            candidate.relative_to(project_root.resolve())
        except ValueError:
            logger.warning("Relative workzone path resolved outside project_root: %s", candidate)
    if candidate == workspace_dir.resolve():
        raise ValueError("path is the agent home workspace; use /workzone off instead")
    if candidate == workspace_dir.resolve().parent:
        logger.warning("Workzone path is the workspaces parent, which is usually not a task directory: %s", candidate)
    return candidate


def save_workzone(workspace_dir: Path, zone: Path, source: str = "telegram") -> None:
    workspace_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "path": str(zone.resolve()),
        "source": source,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    path = state_path(workspace_dir)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def clear_workzone(workspace_dir: Path) -> None:
    state_path(workspace_dir).unlink(missing_ok=True)


def build_workzone_prompt(zone: Path | None, workspace_dir: Path, can_access_files: bool = True) -> tuple[str, str] | None:
    if zone is None:
        return None
    if not can_access_files:
        return (
            "WORKZONE",
            "\n".join(
                [
                    f"Active workzone: {zone}",
                    f"Agent home workspace: {workspace_dir}",
                    "Treat the active workzone as conversation context and the intended project location.",
                    "This backend does not currently have filesystem tools for direct access; do not claim to inspect files unless the user provides content or switches to a tool-capable backend.",
                    "Ignore the agent home workspace for task files unless the user explicitly asks for agent memory, identity, logs, or workspace state.",
                ]
            ),
        )
    return (
        "WORKZONE",
        "\n".join(
            [
                f"Active workzone: {zone}",
                f"Agent home workspace: {workspace_dir}",
                "Use the active workzone as the working directory and first place to inspect.",
                "Ignore the agent home workspace for task files unless the user explicitly asks for agent memory, identity, logs, or workspace state.",
            ]
        ),
    )


def access_root_for_workzone(default_access_root: Path, zone: Path | None) -> Path:
    if zone is None:
        return default_access_root
    default_root = default_access_root.resolve()
    zone_root = zone.resolve()
    try:
        zone_root.relative_to(default_root)
        return default_root
    except ValueError:
        return zone_root
