"""Canonical backend activity events and presentation ownership.

``kind`` records what happened.  ``delivery_class`` records the sole live
presentation owner.  HER emits an explicit class at the source so presentation
code never has to infer ownership from prose.  Legacy adapters may temporarily
leave the class empty and use :func:`legacy_delivery_class` at their existing
presentation boundary.

Adapters must not label generic start/busy messages as thinking and must never
reconstruct provider reasoning that was not returned for display.
"""

import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Optional

# Canonical event kinds.  Backends should use these constants.
KIND_THINKING = "thinking"
KIND_COMMENTARY = "commentary"
KIND_TOOL_START = "tool_start"
KIND_TOOL_END = "tool_end"
KIND_FILE_READ = "file_read"
KIND_FILE_EDIT = "file_edit"
KIND_SHELL_EXEC = "shell_exec"
KIND_TEXT_DELTA = "text_delta"
KIND_PROGRESS = "progress"
KIND_ACKNOWLEDGEMENT = "acknowledgement"
KIND_INITIAL_RESOLUTION = "initial_resolution"
KIND_REVIEW = "review"
KIND_VALIDATION = "validation"
KIND_TESTING = "testing"
KIND_ERROR = "error"

# Canonical presentation owners.  These are deliberately independent of event
# kinds: a direct-response acknowledgement, for example, has acknowledgement
# semantics but belongs solely to the mandatory final lane.
DELIVERY_TECHNICAL = "technical"
DELIVERY_USER_COMMENTARY = "user_commentary"
DELIVERY_REASONING = "reasoning"
DELIVERY_FINAL = "final"
DELIVERY_CONTROL = "control"
DELIVERY_INTERNAL = "internal"
DELIVERY_CLASSES = frozenset(
    {
        DELIVERY_TECHNICAL,
        DELIVERY_USER_COMMENTARY,
        DELIVERY_REASONING,
        DELIVERY_FINAL,
        DELIVERY_CONTROL,
        DELIVERY_INTERNAL,
    }
)


def legacy_delivery_class(kind: str) -> str:
    """Return the compatibility owner for adapters not yet emitting metadata."""

    if kind == KIND_THINKING:
        return DELIVERY_REASONING
    if kind in {KIND_ACKNOWLEDGEMENT, KIND_COMMENTARY}:
        return DELIVERY_USER_COMMENTARY
    if kind == KIND_TEXT_DELTA:
        return DELIVERY_INTERNAL
    return DELIVERY_TECHNICAL


@dataclass
class StreamEvent:
    """A single streaming activity event emitted by a backend adapter."""

    kind: str  # one of the KIND_* constants above
    summary: str  # human-readable content; commentary may be multiline
    timestamp: float = field(default_factory=time.time)
    detail: str = ""  # optional longer diagnostic content
    tool_name: str = ""  # e.g. "Read", "Grep", "Bash"
    file_path: str = ""  # relevant file path, if any
    current: float | None = None  # optional real progress numerator
    total: float | None = None  # optional real progress denominator
    unit: str = ""  # e.g. pages, files, images
    raw_delta: str = ""  # exact provider delta; concatenate verbatim when present
    event_id: str = ""  # stable logical identity for idempotent delivery
    delivery_class: str = ""  # one DELIVERY_* owner; empty only for legacy adapters
    origin: str = ""  # planner, provider, tool gateway, runtime, etc.
    phase: str = ""  # initial, execution, replan, verification, etc.
    revision: int | None = None  # planning/replan revision when applicable
    required: bool = False  # bypass optional presentation toggles
    provenance: str = ""  # provider_returned, provider_summary, etc.
    resolution: str = ""  # final, commentary, clarification, or discard
    target_event_id: str = ""  # provisional event changed by a control event


# Callback signature accepted by generate_response().
# None means "no streaming" (default / verbose-off path).
StreamCallback = Optional[Callable[[StreamEvent], Awaitable[bool | None]]]
