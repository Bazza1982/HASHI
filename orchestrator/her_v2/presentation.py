"""Persona presentation boundary for required HER v2 user messages.

Final reports and clarification questions remain workflow-owned required
messages.  This module changes presentation only: it cannot change delivery
kind, source identity, workflow authority, or the validated message meaning.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


REQUIRED_MESSAGE_KINDS = frozenset({"final", "clarification"})
MAX_RENDERED_REQUIRED_MESSAGE_CHARS = 128_000


class RequiredMessageValidationError(ValueError):
    """A required user message is not safe to render or deliver."""


@dataclass(frozen=True)
class RequiredUserMessage:
    """One validated, Persona-free message that must reach the user."""

    event_id: str
    turn_id: str
    kind: str
    text: str

    def __post_init__(self) -> None:
        event_id = str(self.event_id or "").strip()
        turn_id = str(self.turn_id or "").strip()
        kind = str(self.kind or "").strip()
        text = str(self.text or "").strip()
        if not event_id or not turn_id:
            raise RequiredMessageValidationError(
                "required message needs event and turn identifiers"
            )
        if kind not in REQUIRED_MESSAGE_KINDS:
            raise RequiredMessageValidationError(
                f"{kind!r} is not a required Persona message kind"
            )
        if not text:
            raise RequiredMessageValidationError("required message is empty")
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "text", text)


@dataclass(frozen=True)
class RenderedRequiredMessage:
    """Persona-rendered output that retains required-message identity."""

    source_event_id: str
    kind: str
    text: str
    provenance: str
    fallback: bool = False
    error_type: str = ""

    def __post_init__(self) -> None:
        source_event_id = str(self.source_event_id or "").strip()
        kind = str(self.kind or "").strip()
        text = str(self.text or "").strip()
        provenance = str(self.provenance or "").strip()
        if not source_event_id or not provenance:
            raise RequiredMessageValidationError(
                "rendered required message needs source identity and provenance"
            )
        if kind not in REQUIRED_MESSAGE_KINDS:
            raise RequiredMessageValidationError(
                f"{kind!r} is not a rendered required-message kind"
            )
        if not text:
            raise RequiredMessageValidationError(
                "rendered required message is empty"
            )
        if (
            len(text) > MAX_RENDERED_REQUIRED_MESSAGE_CHARS
            and not bool(self.fallback)
        ):
            raise RequiredMessageValidationError(
                "rendered required message exceeds the bounded size"
            )
        object.__setattr__(self, "source_event_id", source_event_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "error_type", str(self.error_type or "")[:120])


class RequiredPersonaRenderer(Protocol):
    """Presentation-only boundary for required user messages."""

    async def render(
        self, message: RequiredUserMessage
    ) -> RenderedRequiredMessage: ...
