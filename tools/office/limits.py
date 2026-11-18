"""Operator-configured resource budgets for the isolated Office worker."""
DEFAULT_BUDGETS = {
    "file_bytes": 32 * 1024 * 1024,
    "expanded_bytes": 128 * 1024 * 1024,
    "archive_entries": 2048,
    "materialized_cells": 200_000,
}


def _available_memory():
    """Best-effort available physical memory, without optional dependencies."""
    import os
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD),
                            ("total_phys", ctypes.c_ulonglong), ("avail_phys", ctypes.c_ulonglong),
                            ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                            ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                            ("avail_extended", ctypes.c_ulonglong)]
            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.avail_phys)
        elif hasattr(os, "sysconf"):
            available = int(os.sysconf("SC_AVPHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
            # Linux containers may have a lower effective limit than host RAM.
            from pathlib import Path
            for limit_file, used_file in (
                ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current"),
                ("/sys/fs/cgroup/memory/memory.limit_in_bytes", "/sys/fs/cgroup/memory/memory.usage_in_bytes"),
            ):
                try:
                    raw = Path(limit_file).read_text().strip()
                    if raw != "max":
                        remaining = int(raw) - int(Path(used_file).read_text().strip())
                        available = min(available, max(1, remaining))
                except (OSError, ValueError):
                    continue
            return available
    except (AttributeError, OSError, ValueError, TypeError):
        pass
    return None


def resolve_budgets(values=None):
    if values is None:
        values = {}
    if not isinstance(values, dict) or set(values) - set(DEFAULT_BUDGETS):
        raise ValueError("Office resource_budgets must contain only supported budget names.")
    result = dict(DEFAULT_BUDGETS)
    available = _available_memory()
    if available and available > 0:
        result.update(file_bytes=max(1, available // 32),
                      expanded_bytes=max(1, available // 16),
                      archive_entries=max(1, available // (128 * 1024)),
                      materialized_cells=max(1, available // 8192))
    for key, value in values.items():
        if type(value) is not int or value <= 0:
            raise ValueError("Office resource budget " + key + " must be a positive integer.")
        result[key] = value
    return result
