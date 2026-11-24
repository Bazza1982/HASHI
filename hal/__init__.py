"""HAL v0.1 pure language front end.

Importing this package cannot start an event loop, modify HASHI Core or send commands.
It is intentionally usable when HASHI/Workbench runtimes are offline.
"""

from .language import (
    HALAction,
    HALEvent,
    HALProgram,
    HALRule,
    HALSyntaxError,
    compile_hal,
    entry_matches,
    resolve_prompt,
)
from .event_source import (
    EVENT_SYSTEM_IDLE,
    HalTrigger,
    event_availability,
    feed_event_hal_event,
    is_automation_origin,
)
from .runtime import (
    EXEC_ACTIVE,
    EXEC_COMPLETED,
    EXEC_DISABLED,
    EXEC_FAILED,
    plan_for_trigger,
    validate_script,
)

__all__ = [
    "HALAction", "HALEvent", "HALProgram", "HALRule", "HALSyntaxError",
    "compile_hal", "entry_matches", "resolve_prompt",
    "EVENT_SYSTEM_IDLE", "HalTrigger", "event_availability",
    "feed_event_hal_event", "is_automation_origin",
    "EXEC_ACTIVE", "EXEC_COMPLETED", "EXEC_DISABLED", "EXEC_FAILED",
    "plan_for_trigger", "validate_script",
]
# HASHI-Fingerprint: hfp_664c2f23f1094078a3ee5b395a04743f
