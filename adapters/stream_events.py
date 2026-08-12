"""Canonical backend activity events.

Presentation switches consume disjoint subsets of this stream:

* ``/think`` receives ``KIND_THINKING`` events containing genuine
  provider-returned reasoning (or an explicit provider-redacted reasoning
  notice), plus ``KIND_COMMENTARY`` events containing model-authored interim
  commentary when a backend exposes it.
* ``/verbose`` receives progress, tool/file/shell, result, and error events.
* ``KIND_TEXT_DELTA`` is reserved for local observers and final-answer
  assembly; Telegram does not present live answer drafts.

Adapters must not label generic start/busy messages as thinking.
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
KIND_REVIEW = "review"
KIND_VALIDATION = "validation"
KIND_TESTING = "testing"
KIND_ERROR = "error"


@dataclass
class StreamEvent:
    """A single streaming activity event emitted by a backend adapter."""

    kind: str                       # one of the KIND_* constants above
    summary: str                    # human-readable content; commentary may be multiline
    timestamp: float = field(default_factory=time.time)
    detail: str = ""                # optional longer diagnostic content
    tool_name: str = ""             # e.g. "Read", "Grep", "Bash"
    file_path: str = ""             # relevant file path, if any
    current: float | None = None     # optional real progress numerator
    total: float | None = None       # optional real progress denominator
    unit: str = ""                   # e.g. pages, files, images
    raw_delta: str = ""             # exact provider delta; concatenate verbatim when present


# Callback signature accepted by generate_response().
# None means "no streaming" (default / verbose-off path).
StreamCallback = Optional[Callable[[StreamEvent], Awaitable[None]]]
