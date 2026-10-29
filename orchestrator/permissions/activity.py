"""Private tool-call activity identity; it never carries permission authority."""
from contextvars import ContextVar

TOOL_CALL = ContextVar("permission_current_tool_call", default="")
