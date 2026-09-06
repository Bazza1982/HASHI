"""Path-component validation shared by Nagare persistence boundaries."""

from __future__ import annotations

import re
from pathlib import Path

_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}\Z")


def validate_path_component(value: str, *, label: str) -> str:
    """Return a safe single path component or raise ``ValueError``."""
    if not isinstance(value, str) or _SAFE_COMPONENT.fullmatch(value) is None:
        raise ValueError(
            f"{label} must be a single 1-255 character identifier containing only "
            "letters, numbers, '.', '_' or '-'"
        )
    return value


def resolve_relative_path(root: str | Path, value: str, *, label: str) -> Path:
    """Resolve a non-empty relative path and keep it below ``root``.

    Existing symlinks are resolved as part of the containment check, so a
    seemingly local path cannot escape through a symlinked parent.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty relative path")

    candidate = Path(value)
    if candidate.is_absolute():
        raise ValueError(f"{label} must be relative to its workspace")

    resolved_root = Path(root).resolve()
    resolved = (resolved_root / candidate).resolve()
    try:
        relative = resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{label} must remain inside its workspace") from exc
    if not relative.parts:
        raise ValueError(f"{label} must identify a child of its workspace")
    return resolved
