"""Cross-platform privacy-mode helpers owned by the HASHI Core runtime."""

from __future__ import annotations

import os


def tighten_fd_permissions(file_descriptor: int, mode: int = 0o600) -> None:
    """Apply a POSIX descriptor mode when the platform provides that API.

    Windows does not expose ``os.fchmod`` and its ACL model cannot be
    represented by POSIX mode bits.  Windows deployment permissions are owned
    by the installer; runtime writes must therefore avoid pretending that an
    unavailable descriptor operation succeeded.
    """

    operation = getattr(os, "fchmod", None)
    if os.name != "nt" and callable(operation):
        operation(file_descriptor, mode)
