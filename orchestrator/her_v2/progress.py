"""Meaningful-progress tracking for HER v2 idle timeout enforcement."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


@dataclass
class ProgressTracker:
    clock: Callable[[], float] = time.monotonic
    last_progress_at: float = field(init=False)
    _fingerprints: set[str] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        self.last_progress_at = float(self.clock())

    def record(self, kind: str, content: str, *, meaningful: bool = True) -> bool:
        if not meaningful:
            return False
        normalized = " ".join(str(content or "").split())
        if not normalized:
            return False
        digest = hashlib.sha256(f"{kind}|{normalized}".encode("utf-8")).hexdigest()
        if digest in self._fingerprints:
            return False
        self._fingerprints.add(digest)
        self.last_progress_at = float(self.clock())
        return True

    def idle_for(self) -> float:
        return max(0.0, float(self.clock()) - self.last_progress_at)

    def expired(self, timeout_s: float) -> bool:
        return self.idle_for() >= float(timeout_s)


@dataclass
class ProviderActivityTracker:
    """Attempt-local provider activity, independent of user-visible progress."""

    clock: Callable[[], float] = time.monotonic
    started_at: float = field(init=False)
    first_activity_at: float | None = field(default=None, init=False)
    last_activity_at: float | None = field(default=None, init=False)
    event_count: int = field(default=0, init=False)
    text_event_count: int = field(default=0, init=False)
    reasoning_event_count: int = field(default=0, init=False)
    tool_started: set[str] = field(default_factory=set, init=False)
    tool_completed: set[str] = field(default_factory=set, init=False)
    tool_start_counts: dict[str, int] = field(default_factory=dict, init=False)
    tool_completion_counts: dict[str, int] = field(default_factory=dict, init=False)
    non_read_only_tools: set[str] = field(default_factory=set, init=False)
    unknown_tools: set[str] = field(default_factory=set, init=False)
    last_event_kind: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self.started_at = float(self.clock())

    def record(self, event: Mapping[str, Any]) -> bool:
        kind = str(event.get("kind") or "").strip()
        content = str(event.get("content") or "").strip()
        tool_name = str(event.get("tool_name") or "").strip()
        # Empty transport heartbeats are deliberately not activity.
        if not kind or not (content or tool_name):
            return False
        now = float(self.clock())
        if self.first_activity_at is None:
            self.first_activity_at = now
        self.last_activity_at = now
        previous_kind = self.last_event_kind
        self.last_event_kind = kind
        self.event_count += 1
        if kind == "text_delta":
            self.text_event_count += 1
        if kind == "thinking":
            self.reasoning_event_count += 1
        if kind in {"tool_start", "file_read", "file_edit", "shell_exec"}:
            effective_tool = tool_name or kind
            self.tool_started.add(effective_tool)
            # Some adapters emit ``tool_start`` followed immediately by a
            # specialised file/shell event for the same call.  Count that pair
            # once, while still counting a later invocation of the same tool.
            active = self.tool_start_counts.get(
                effective_tool, 0
            ) > self.tool_completion_counts.get(effective_tool, 0)
            duplicate_specialised_event = bool(
                kind in {"file_read", "file_edit", "shell_exec"}
                and previous_kind == "tool_start"
                and active
            )
            if not duplicate_specialised_event:
                self.tool_start_counts[effective_tool] = (
                    self.tool_start_counts.get(effective_tool, 0) + 1
                )
            read_only = event.get("tool_read_only")
            if kind == "file_read":
                read_only = True
            elif kind in {"file_edit", "shell_exec"}:
                read_only = False
            if read_only is True:
                self.unknown_tools.discard(effective_tool)
            elif read_only is False:
                self.unknown_tools.discard(effective_tool)
                self.non_read_only_tools.add(effective_tool)
            else:
                if effective_tool not in self.non_read_only_tools:
                    self.unknown_tools.add(effective_tool)
        if kind == "tool_end" and tool_name:
            self.tool_completed.add(tool_name)
            self.tool_completion_counts[tool_name] = (
                self.tool_completion_counts.get(tool_name, 0) + 1
            )
        return True

    @property
    def response_started(self) -> bool:
        return self.first_activity_at is not None

    @property
    def side_effects_possible(self) -> bool:
        return bool(self.non_read_only_tools or self.unknown_tools)

    def replay_safe(self, *, allow_side_effects: bool) -> bool:
        if not allow_side_effects or not self.tool_started:
            return True
        if self.side_effects_possible:
            return False
        return all(
            self.tool_completion_counts.get(tool_name, 0) >= started
            for tool_name, started in self.tool_start_counts.items()
        )

    def snapshot(self) -> dict[str, Any]:
        now = float(self.clock())
        return {
            "started_at_monotonic": self.started_at,
            "first_activity_after_s": (
                round(self.first_activity_at - self.started_at, 6)
                if self.first_activity_at is not None
                else None
            ),
            "last_activity_after_s": (
                round(self.last_activity_at - self.started_at, 6)
                if self.last_activity_at is not None
                else None
            ),
            "last_activity_age_s": (
                round(max(0.0, now - self.last_activity_at), 6)
                if self.last_activity_at is not None
                else None
            ),
            "event_count": self.event_count,
            "text_event_count": self.text_event_count,
            "reasoning_event_count": self.reasoning_event_count,
            "tool_started": sorted(self.tool_started),
            "tool_completed": sorted(self.tool_completed),
            "tool_start_counts": dict(sorted(self.tool_start_counts.items())),
            "tool_completion_counts": dict(
                sorted(self.tool_completion_counts.items())
            ),
            "non_read_only_tools": sorted(self.non_read_only_tools),
            "unknown_tools": sorted(self.unknown_tools),
            "side_effects_possible": self.side_effects_possible,
            "last_event_kind": self.last_event_kind or None,
        }
