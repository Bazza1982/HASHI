"""Typed Workzone validation facts; preserve existing ValueError compatibility."""

from __future__ import annotations

_REASONS = frozenset(
    {
        "workzone_path_missing",
        "workzone_windows_path_required",
        "workzone_path_is_file",
        "workzone_path_not_directory",
        "workzone_path_is_agent_home",
        "workzone_path_duplicate",
    }
)


class WorkzonePathError(ValueError):
    def __init__(self, reason: str, message: str):
        if reason not in _REASONS:
            raise ValueError("invalid Workzone path failure reason")
        self.reason = reason
        super().__init__(message)
