"""HER v2 commentary lane.

Ordinary stage commentary is optional; compulsory Replanning builds its
required neutral event from a successful validated stage result. This module
deliberately knows nothing about lifecycle events, plans, execution state,
provider prompts, Persona files, or Telegram. Persona packaging and transport
are composed outside :class:`HERv2Runtime`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol

from .models import Stage, StageResponse
from .structured import response_data


MAX_NEUTRAL_COMMENTARY_CHARS = 4_000
MAX_PACKAGED_COMMENTARY_CHARS = 8_000
MAX_DRAFT_RESPONSE_COMMENTARY_CHARS = 128_000

# Immediate Response and Finalisation already own dedicated user-facing lanes.
# Triage is classification authority, while sub-agents are not user-facing.
COMMENTARY_STAGES = frozenset(
    {
        Stage.PLANNING,
        Stage.EXECUTION,
        Stage.REPLANNING,
        Stage.REVIEW,
        Stage.VERIFICATION,
    }
)


class CommentaryValidationError(ValueError):
    """A commentary field or event is not safe to publish."""


@dataclass(frozen=True)
class NeutralCommentary:
    """A bounded, Persona-free message supplied by one successful stage."""

    event_id: str
    turn_id: str
    stage: Stage
    attempt: int
    text: str
    required_facts: tuple[str, ...] = ()
    minimal_persona_fallback_reason: str = ""

    def __post_init__(self) -> None:
        event_id = str(self.event_id or "").strip()
        turn_id = str(self.turn_id or "").strip()
        text = str(self.text or "").strip()
        if not event_id or not turn_id:
            raise CommentaryValidationError(
                "neutral commentary requires event and turn identifiers"
            )
        if self.stage not in COMMENTARY_STAGES:
            raise CommentaryValidationError(
                f"{self.stage.value} is not a user-commentary stage"
            )
        if int(self.attempt) < 1:
            raise CommentaryValidationError(
                "neutral commentary attempt must be positive"
            )
        if not text:
            raise CommentaryValidationError("neutral commentary is empty")
        if len(text) > MAX_NEUTRAL_COMMENTARY_CHARS:
            raise CommentaryValidationError(
                "neutral commentary exceeds the bounded size"
            )
        required_facts = tuple(
            str(item or "").strip() for item in self.required_facts
        )
        if any(not item for item in required_facts):
            raise CommentaryValidationError(
                "neutral commentary required facts must be non-empty"
            )
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "required_facts", required_facts)
        object.__setattr__(
            self,
            "minimal_persona_fallback_reason",
            str(self.minimal_persona_fallback_reason or "").strip()[:120],
        )


@dataclass(frozen=True)
class DraftResponseCommentary:
    """Exact provisional Primary Execution text for the commentary lane."""

    event_id: str
    turn_id: str
    response: str

    def __post_init__(self) -> None:
        event_id = str(self.event_id or "").strip()
        turn_id = str(self.turn_id or "").strip()
        response = str(self.response or "").strip()
        if not event_id or not turn_id:
            raise CommentaryValidationError(
                "draft response commentary requires event and turn identifiers"
            )
        if not response:
            raise CommentaryValidationError("draft response commentary is empty")
        if len(self.text) > MAX_DRAFT_RESPONSE_COMMENTARY_CHARS:
            raise CommentaryValidationError(
                "draft response commentary exceeds the bounded size"
            )
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "response", response)

    @property
    def text(self) -> str:
        return f"DRAFT RESPONSE\n\n{self.response}"


@dataclass(frozen=True)
class PackagedCommentary:
    """Persona-packaged output accepted by the commentary delivery boundary."""

    source_event_id: str
    stage: Stage
    text: str
    provenance: str
    fallback: bool = False
    error_type: str = ""
    draft_response: bool = False

    def __post_init__(self) -> None:
        source_event_id = str(self.source_event_id or "").strip()
        text = str(self.text or "").strip()
        provenance = str(self.provenance or "").strip()
        if not source_event_id or not provenance:
            raise CommentaryValidationError(
                "packaged commentary requires source identity and provenance"
            )
        if self.stage not in COMMENTARY_STAGES:
            raise CommentaryValidationError(
                f"{self.stage.value} is not a packaged-commentary stage"
            )
        if not text:
            raise CommentaryValidationError("packaged commentary is empty")
        max_chars = (
            MAX_DRAFT_RESPONSE_COMMENTARY_CHARS
            if self.draft_response
            else MAX_PACKAGED_COMMENTARY_CHARS
        )
        if len(text) > max_chars:
            raise CommentaryValidationError(
                "packaged commentary exceeds the bounded size"
            )
        if self.draft_response and (
            self.stage is not Stage.EXECUTION
            or not text.startswith("DRAFT RESPONSE\n\n")
        ):
            raise CommentaryValidationError(
                "draft response commentary must preserve the labelled Execution draft"
            )
        object.__setattr__(self, "source_event_id", source_event_id)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "error_type", str(self.error_type or "")[:120])


class CommentaryPort(Protocol):
    """Runtime-facing neutral commentary boundary."""

    async def publish(self, commentary: NeutralCommentary) -> bool: ...

    async def publish_draft(self, commentary: DraftResponseCommentary) -> bool: ...


class PersonaPackager(Protocol):
    """Presentation-only boundary: neutral commentary in, packaged prose out."""

    async def package(self, commentary: NeutralCommentary) -> PackagedCommentary: ...


class PackagedCommentaryDelivery(Protocol):
    """Transport boundary that cannot accept an untyped raw string."""

    async def deliver_packaged_commentary(
        self, commentary: PackagedCommentary
    ) -> bool: ...


@dataclass
class NullCommentaryPort:
    async def publish(self, commentary: NeutralCommentary) -> bool:
        del commentary
        return False

    async def publish_draft(self, commentary: DraftResponseCommentary) -> bool:
        del commentary
        return False


@dataclass
class RecordingCommentaryPort:
    records: list[NeutralCommentary] = field(default_factory=list)
    drafts: list[DraftResponseCommentary] = field(default_factory=list)
    fail: bool = False
    accept_drafts: bool = False

    async def publish(self, commentary: NeutralCommentary) -> bool:
        if self.fail:
            raise RuntimeError("recording commentary port failure")
        if all(item.event_id != commentary.event_id for item in self.records):
            self.records.append(commentary)
        return True

    async def publish_draft(self, commentary: DraftResponseCommentary) -> bool:
        if self.fail:
            raise RuntimeError("recording commentary port failure")
        if all(item.event_id != commentary.event_id for item in self.drafts):
            self.drafts.append(commentary)
        return self.accept_drafts


class PersonaCommentaryPipeline:
    """Package, then deliver, each logical commentary event at most once.

    Event IDs are reserved before packaging.  A transport ambiguity therefore
    cannot cause replay to duplicate a user-facing message.  Pipeline failure
    remains an optional presentation failure and is returned as ``False``.
    """

    def __init__(
        self,
        *,
        packager: PersonaPackager,
        delivery: PackagedCommentaryDelivery,
    ) -> None:
        self.packager = packager
        self.delivery = delivery
        self._lock = asyncio.Lock()
        self._attempts: dict[str, asyncio.Task[bool]] = {}

    async def publish(self, commentary: NeutralCommentary) -> bool:
        async with self._lock:
            task = self._attempts.get(commentary.event_id)
            if task is None:
                task = asyncio.create_task(self._package_then_deliver(commentary))
                self._attempts[commentary.event_id] = task
        return await asyncio.shield(task)

    async def publish_draft(self, commentary: DraftResponseCommentary) -> bool:
        async with self._lock:
            task = self._attempts.get(commentary.event_id)
            if task is None:
                task = asyncio.create_task(self._deliver_draft(commentary))
                self._attempts[commentary.event_id] = task
        return await asyncio.shield(task)

    async def _package_then_deliver(self, commentary: NeutralCommentary) -> bool:
        try:
            packaged = await self.packager.package(commentary)
            if packaged.source_event_id != commentary.event_id:
                raise CommentaryValidationError(
                    "packager changed the source commentary identity"
                )
            return bool(await self.delivery.deliver_packaged_commentary(packaged))
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def _deliver_draft(self, commentary: DraftResponseCommentary) -> bool:
        try:
            packaged = PackagedCommentary(
                source_event_id=commentary.event_id,
                stage=Stage.EXECUTION,
                text=commentary.text,
                provenance="primary_execution_draft",
                draft_response=True,
            )
            return bool(await self.delivery.deliver_packaged_commentary(packaged))
        except asyncio.CancelledError:
            raise
        except Exception:
            return False


def commentary_from_stage_response(
    response: StageResponse,
    *,
    turn_id: str,
    stage: Stage,
    invocation: int,
    attempt: int,
) -> NeutralCommentary | None:
    """Extract one optional neutral field without affecting stage validity."""

    if stage not in COMMENTARY_STAGES:
        return None
    raw = response_data(response).get("commentary")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise CommentaryValidationError("commentary must be a string when present")
    text = raw.strip()
    if not text:
        raise CommentaryValidationError("commentary must be non-empty when present")
    return NeutralCommentary(
        event_id=f"{turn_id}:commentary:{stage.value}:{invocation}:{attempt}",
        turn_id=turn_id,
        stage=stage,
        attempt=attempt,
        text=text,
    )
