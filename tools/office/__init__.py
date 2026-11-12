"""Office Toolkit contracts and discovery; Office libraries stay in the sidecar."""
from .contract import (
    OFFICE_READ_ONLY_TOOLS,
    OFFICE_TOOL_NAMES,
    OFFICE_TOOL_SCHEMAS,
    OfficeInputError,
    office_tool_definitions,
    validate_office_arguments,
)
from .task_signal import detect_office_format, office_task_hint

__all__ = [
    "OFFICE_READ_ONLY_TOOLS",
    "OFFICE_TOOL_NAMES",
    "OFFICE_TOOL_SCHEMAS",
    "OfficeInputError",
    "office_tool_definitions",
    "validate_office_arguments",
    "detect_office_format",
    "office_task_hint",
]
