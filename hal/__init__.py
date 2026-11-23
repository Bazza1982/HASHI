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

__all__ = [
    "HALAction", "HALEvent", "HALProgram", "HALRule", "HALSyntaxError",
    "compile_hal", "entry_matches", "resolve_prompt",
]
