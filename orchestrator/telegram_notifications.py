from __future__ import annotations

from pathlib import Path
from typing import Any

NOTIFY_ON_MARKER = ".notify_on"
NOTIFY_QUIET_MARKER = ".notify_quiet"
NOTIFY_MODES = frozenset({"on", "quiet", "off"})
QUIET_INTERIM_PURPOSES = frozenset(
    {
        "answer_preview",
        "initial_resolution",
        "left-brain-visible",
        "meditation-cost",
        "meter-cost",
        "placeholder",
        "placeholder_status",
        "reasoning",
        "streaming_display",
        "streaming_display_rollover",
        "task_acknowledgement",
        "task_commentary",
        "technical",
        "think",
        "wrapper-verbose",
    }
)


def notification_mode(runtime: Any) -> str:
    value = str(getattr(runtime, "_notify_mode", "") or "").strip().lower()
    if value in NOTIFY_MODES:
        return value
    legacy = getattr(runtime, "_notify_enabled", None)
    if legacy is True:
        return "on"
    workspace_dir = getattr(runtime, "workspace_dir", None)
    if workspace_dir is None:
        return "off"
    root = Path(workspace_dir)
    if (root / NOTIFY_QUIET_MARKER).exists():
        return "quiet"
    return "on" if (root / NOTIFY_ON_MARKER).exists() else "off"


def notify_enabled(runtime: Any) -> bool:
    return notification_mode(runtime) == "on"


def set_notification_mode(runtime: Any, mode: str) -> None:
    resolved = str(mode or "").strip().lower()
    if resolved not in NOTIFY_MODES:
        raise ValueError(f"Unsupported notification mode: {mode!r}")
    runtime._notify_mode = resolved
    runtime._notify_enabled = resolved == "on"
    workspace_dir = getattr(runtime, "workspace_dir", None)
    if workspace_dir is None:
        return
    root = Path(workspace_dir)
    on_path = root / NOTIFY_ON_MARKER
    quiet_path = root / NOTIFY_QUIET_MARKER
    on_path.unlink(missing_ok=True)
    quiet_path.unlink(missing_ok=True)
    if resolved == "on":
        on_path.touch()
    elif resolved == "quiet":
        quiet_path.touch()


def set_notify_enabled(runtime: Any, enabled: bool) -> None:
    set_notification_mode(runtime, "on" if enabled else "off")


def disable_notification(
    runtime: Any,
    *,
    purpose: str = "",
    delivery_mode: str = "",
    final_chunk: bool = True,
) -> bool:
    mode = notification_mode(runtime)
    if mode == "on":
        return False
    if mode == "off":
        return True
    message_purpose = str(purpose or "").strip().lower()
    important_tokens = ("error", "important", "alert", "warning", "timeout", "recovery")
    if message_purpose in {"control", "her_control", "park-reminder"} or any(
        token in message_purpose for token in important_tokens
    ):
        return False
    if message_purpose in QUIET_INTERIM_PURPOSES:
        return True
    # Quiet treats every non-interim delivery as a completed result. This
    # covers foreground answers, background completions, command results, and
    # future terminal message kinds without maintaining a fragile allowlist.
    return not final_chunk


def apply_disable_notification_default(runtime: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    kwargs.setdefault(
        "disable_notification",
        disable_notification(
            runtime,
            purpose=str(kwargs.get("_purpose") or ""),
            delivery_mode=str(kwargs.get("_delivery_mode") or ""),
        ),
    )
    return kwargs
