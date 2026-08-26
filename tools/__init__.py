from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tools.registry import ToolRegistry, ToolResult

__all__ = ["ToolRegistry", "ToolResult"]


def __getattr__(name: str):
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from tools.registry import ToolRegistry, ToolResult

    exports = {"ToolRegistry": ToolRegistry, "ToolResult": ToolResult}
    globals().update(exports)
    return exports[name]
