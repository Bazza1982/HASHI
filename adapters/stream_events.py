"""
Stream event types for real-time verbose display.

When verbose mode is ON, backends emit StreamEvent objects via an
on_stream_event callback.  The runtime's streaming display loop
consumes them and edits the Telegram placeholder message in real time.
"""

import time
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Optional

# Canonical event kinds.  Backends should use these constants.
KIND_THINKING = "thinking"
KIND_TOOL_START = "tool_start"
KIND_TOOL_END = "tool_end"
KIND_FILE_READ = "file_read"
KIND_FILE_EDIT = "file_edit"
KIND_SHELL_EXEC = "shell_exec"
KIND_TEXT_DELTA = "text_delta"
KIND_PROGRESS = "progress"
KIND_ERROR = "error"


@dataclass
class StreamEvent:
    """A single streaming activity event emitted by a backend adapter."""

    kind: str                       # one of the KIND_* constants above
    summary: str                    # human-readable one-liner, e.g. "Reading config.py"
    timestamp: float = field(default_factory=time.time)
    detail: str = ""                # optional longer content (truncated before display)
    tool_name: str = ""             # e.g. "Read", "Grep", "Bash"
    file_path: str = ""             # relevant file path, if any


# Callback signature accepted by generate_response().
# None means "no streaming" (default / verbose-off path).
StreamCallback = Optional[Callable[[StreamEvent], Awaitable[None]]]
