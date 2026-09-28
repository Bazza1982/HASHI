"""Persona presentation boundary for required HER v2 clarifications.

Clarification questions remain workflow-owned required messages. This module
changes presentation only: it cannot change delivery kind, source identity,
workflow authority, or the validated message meaning.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


REQUIRED_MESSAGE_KINDS = frozenset({"clarification"})
MAX_RENDERED_REQUIRED_MESSAGE_CHARS = 128_000
MAX_FINAL_STYLE_TEXT_CHARS = 128_000
PRESENTATION_AUTHORITIES = frozenset(
    {"permanent_system", "global_system", "local_system", "persona"}
)


class RequiredMessageValidationError(ValueError):
    """A required user message is not safe to render or deliver."""


@dataclass(frozen=True)
class RequiredUserMessage:
    """One validated, Persona-free clarification that must reach the user."""

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
    """Presentation-only boundary for required clarification messages."""

    async def render(
        self, message: RequiredUserMessage
    ) -> RenderedRequiredMessage: ...


@dataclass(frozen=True)
class PresentationRequirement:
    """One typed, authoritative source of final-answer presentation rules."""

    key: str
    title: str
    authority: str
    text: str

    def __post_init__(self) -> None:
        key = str(self.key or "").strip()
        title = str(self.title or "").strip()
        authority = str(self.authority or "").strip()
        text = str(self.text or "").strip()
        if not key or not text:
            raise RequiredMessageValidationError(
                "presentation requirement needs source identity and text"
            )
        if authority not in PRESENTATION_AUTHORITIES:
            raise RequiredMessageValidationError(
                f"{authority!r} is not presentation authority"
            )
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "title", title or key)
        object.__setattr__(self, "authority", authority)
        object.__setattr__(self, "text", text)


@dataclass(frozen=True)
class FinalStyleRequest:
    """A main-model answer plus the rules allowed to change its expression."""

    event_id: str
    turn_id: str
    draft_text: str
    current_request: str
    requirements: tuple[PresentationRequirement, ...] = ()

    def __post_init__(self) -> None:
        event_id = str(self.event_id or "").strip()
        turn_id = str(self.turn_id or "").strip()
        draft_text = str(self.draft_text or "").strip()
        current_request = str(self.current_request or "").strip()
        if not event_id or not turn_id:
            raise RequiredMessageValidationError(
                "final style request needs event and turn identifiers"
            )
        if not draft_text:
            raise RequiredMessageValidationError("final style draft is empty")
        if len(draft_text) > MAX_FINAL_STYLE_TEXT_CHARS:
            raise RequiredMessageValidationError(
                "final style draft exceeds the bounded size"
            )
        requirements = tuple(self.requirements or ())
        if any(not isinstance(item, PresentationRequirement) for item in requirements):
            raise RequiredMessageValidationError(
                "final style requirements must be typed presentation requirements"
            )
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "draft_text", draft_text)
        object.__setattr__(self, "current_request", current_request)
        object.__setattr__(self, "requirements", requirements)


@dataclass(frozen=True)
class FinalStyleResult:
    """Presentation-only decision for one already-complete main answer."""

    source_event_id: str
    decision: str
    text: str
    provenance: str
    fallback: bool = False
    error_type: str = ""

    def __post_init__(self) -> None:
        source_event_id = str(self.source_event_id or "").strip()
        decision = str(self.decision or "").strip().casefold()
        text = str(self.text or "").strip()
        provenance = str(self.provenance or "").strip()
        if not source_event_id or not provenance:
            raise RequiredMessageValidationError(
                "final style result needs source identity and provenance"
            )
        if decision not in {"keep", "rewrite"}:
            raise RequiredMessageValidationError(
                f"{decision!r} is not a final style decision"
            )
        if not text:
            raise RequiredMessageValidationError("final style result is empty")
        if len(text) > MAX_FINAL_STYLE_TEXT_CHARS:
            raise RequiredMessageValidationError(
                "final style result exceeds the bounded size"
            )
        object.__setattr__(self, "source_event_id", source_event_id)
        object.__setattr__(self, "decision", decision)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "error_type", str(self.error_type or "")[:120])


class FinalStyleRenderer(Protocol):
    """Optional presentation-only boundary before final FC delivery."""

    async def render(self, request: FinalStyleRequest) -> FinalStyleResult: ...
