from __future__ import annotations

from pathlib import Path
from typing import Any


NOTIFY_ON_MARKER = ".notify_on"


def notify_enabled(runtime: Any) -> bool:
    value = getattr(runtime, "_notify_enabled", None)
    if value is not None:
        return bool(value)
    workspace_dir = getattr(runtime, "workspace_dir", None)
    if workspace_dir is None:
        return False
    return (Path(workspace_dir) / NOTIFY_ON_MARKER).exists()


def set_notify_enabled(runtime: Any, enabled: bool) -> None:
    setattr(runtime, "_notify_enabled", bool(enabled))
    workspace_dir = getattr(runtime, "workspace_dir", None)
    if workspace_dir is None:
        return
    path = Path(workspace_dir) / NOTIFY_ON_MARKER
    if enabled:
        path.touch()
    else:
        path.unlink(missing_ok=True)


def disable_notification(runtime: Any) -> bool:
    return not notify_enabled(runtime)


def apply_disable_notification_default(runtime: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    kwargs.setdefault("disable_notification", disable_notification(runtime))
    return kwargs
