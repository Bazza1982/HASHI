"""Storage tuning for the explicitly selected Portable removable-drive profile."""

from __future__ import annotations

import os
from typing import IO, Any


def removable_storage_profile() -> bool:
    """Return whether HASHI was launched with its removable-drive profile."""

    return (
        str(os.environ.get("HASHI_PORTABLE_STORAGE_PROFILE") or "")
        .strip()
        .casefold()
        == "removable"
    )


def flush_projection(handle: IO[Any]) -> None:
    """Flush a recoverable projection without redundant Portable barriers."""

    handle.flush()
    if not removable_storage_profile():
        os.fsync(handle.fileno())


__all__ = ["flush_projection", "removable_storage_profile"]
