from __future__ import annotations

import asyncio
import logging

import pytest

from adapters.her_persona import HERPersonaPackagingSource
from adapters.her_v2 import _AdapterDelivery, _ConfiguredPersonaPackager
from orchestrator.her_v2.commentary import (
    CommentaryValidationError,
    NeutralCommentary,
    PackagedCommentary,
    PersonaCommentaryPipeline,
    commentary_from_stage_response,
)
from orchestrator.her_v2.config import ProviderProfile
from orchestrator.her_v2.models import Stage, StageResponse
from orchestrator.her_v2.presentation import (
    MAX_RENDERED_REQUIRED_MESSAGE_CHARS,
    RequiredUserMessage,
)


def _neutral(event_id: str = "turn:commentary:execution:1:1"):
    return NeutralCommentary(
        event_id=event_id,
        turn_id="turn",
        stage=Stage.EXECUTION,
        attempt=1,
        text="Three checks passed; the final verification is running.",
    )


class _RecordingPackager:
    def __init__(self, timeline):
        self.timeline = timeline
        self.calls = []

    async def package(self, commentary):
        self.calls.append(commentary)
        self.timeline.append(("package", commentary.event_id))
        await asyncio.sleep(0)
        return PackagedCommentary(
            source_event_id=commentary.event_id,
            stage=commentary.stage,
            text=f"Packaged: {commentary.text}",
            provenance="test_packager",
        )


class _RecordingDelivery:
    def __init__(self, timeline):
        self.timeline = timeline
        self.calls = []

    async def deliver_packaged_commentary(self, commentary):
        assert isinstance(commentary, PackagedCommentary)
        self.calls.append(commentary)
        self.timeline.append(("deliver", commentary.source_event_id))
        return True


@pytest.mark.asyncio
async def test_commentary_pipeline_packages_before_delivery_and_deduplicates_replay():
    timeline = []
    packager = _RecordingPackager(timeline)
    delivery = _RecordingDelivery(timeline)
    pipeline = PersonaCommentaryPipeline(packager=packager, delivery=delivery)
    commentary = _neutral()

    accepted = await asyncio.gather(
        pipeline.publish(commentary),
        pipeline.publish(commentary),
        pipeline.publish(commentary),
    )

    assert accepted == [True, True, True]
    assert packager.calls == [commentary]
    assert len(delivery.calls) == 1
    assert timeline == [
        ("package", commentary.event_id),
        ("deliver", commentary.event_id),
    ]


@pytest.mark.asyncio
async def test_generic_delivery_boundary_rejects_raw_commentary_strings():
    delivery = _AdapterDelivery(lambda _event: None, allow_early=True)

    with pytest.raises(ValueError, match="raw commentary"):
        await delivery.deliver(
            kind="commentary",
            text="unpackaged text",
            event_id="turn:raw",
        )


def test_optional_commentary_extraction_is_bounded_and_stage_scoped():
    response = StageResponse(
        text="",
        data={"summary": "done", "commentary": "  A neutral update.  "},
    )

    commentary = commentary_from_stage_response(
        response,
        turn_id="turn",
        stage=Stage.EXECUTION,
        invocation=2,
        attempt=1,
    )

    assert commentary is not None
    assert commentary.text == "A neutral update."
    assert commentary.event_id == "turn:commentary:execution:2:1"
    verification_commentary = commentary_from_stage_response(
        response,
        turn_id="turn",
        stage=Stage.VERIFICATION,
        invocation=3,
        attempt=1,
    )
    assert verification_commentary is not None
    assert verification_commentary.stage is Stage.VERIFICATION
    assert (
        commentary_from_stage_response(
            response,
            turn_id="turn",
            stage=Stage.CHECKPOINT,
            invocation=4,
            attempt=1,
        )
        is None
    )
    assert (
        commentary_from_stage_response(
            response,
            turn_id="turn",
            stage=Stage.FINALISATION,
            invocation=3,
            attempt=1,
        )
        is None
    )
    with pytest.raises(CommentaryValidationError, match="must be a string"):
        commentary_from_stage_response(
            StageResponse(text="", data={"commentary": ["invalid"]}),
            turn_id="turn",
            stage=Stage.PLANNING,
            invocation=1,
            attempt=1,
        )


class _PackagingProvider:
    def __init__(self, *, error=None):
        self.error = error
        self.calls = []
        self.required_calls = []

    async def package_persona_commentary(self, profile, **kwargs):
        self.calls.append((profile, kwargs))
        if self.error is not None:
            raise self.error
        return "Captain, three checks passed; final verification is running."

    async def package_persona_required_message(self, profile, **kwargs):
        self.required_calls.append((profile, kwargs))
        if self.error is not None:
            raise self.error
        return f"Captain, rendered {kwargs['message_kind']}: {kwargs['neutral_message']}"


def _packaging_source(*, usable=True, reason=None):
    return HERPersonaPackagingSource(
        guidance="Address the user as Captain.",
        display_name="Navigator",
        usable=usable,
        unavailable_reason=reason,
        content_sha256="digest" if usable else None,
    )


@pytest.mark.asyncio
async def test_configured_packager_passes_only_neutral_text_and_persona_block():
    provider = _PackagingProvider()
    packager = _ConfiguredPersonaPackager(
        provider=provider,
        profile=ProviderProfile(
            "lightweight", "openrouter-api", "configured/model"
        ),
        source=_packaging_source(),
        request_id="request",
        logger=logging.getLogger("test.her-v2-persona-packager"),
    )

    packaged = await packager.package(_neutral())

    assert packaged.provenance == "persona_packager"
    assert packaged.fallback is False
    assert len(provider.calls) == 1
    _profile, inputs = provider.calls[0]
    assert set(inputs) == {"persona_block", "neutral_commentary", "request_id"}
    assert inputs["persona_block"] == "Address the user as Captain."
    assert inputs["neutral_commentary"] == _neutral().text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "error", "reason"),
    [
        (_packaging_source(usable=False, reason="persona_block_missing"), None, "persona_block_missing"),
        (_packaging_source(), RuntimeError("provider offline"), "RuntimeError"),
    ],
)
async def test_missing_block_or_packaging_failure_uses_deterministic_minimal_fallback(
    source, error, reason
):
    provider = _PackagingProvider(error=error)
    packager = _ConfiguredPersonaPackager(
        provider=provider,
        profile=ProviderProfile(
            "lightweight", "openrouter-api", "configured/model"
        ),
        source=source,
        request_id="request",
        logger=logging.getLogger("test.her-v2-persona-fallback"),
    )

    packaged = await packager.package(_neutral())

    assert packaged.fallback is True
    assert packaged.provenance == "minimal_persona_fallback"
    assert packaged.error_type == reason
    assert packaged.text.startswith("Navigator 向您汇报：")
    assert packaged.text.endswith(_neutral().text)
    assert len(provider.calls) == (0 if not source.usable else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["final", "clarification"])
async def test_configured_packager_renders_typed_required_messages(kind):
    provider = _PackagingProvider()
    packager = _ConfiguredPersonaPackager(
        provider=provider,
        profile=ProviderProfile(
            "lightweight", "openrouter-api", "configured/model"
        ),
        source=_packaging_source(),
        request_id="request",
        logger=logging.getLogger("test.her-v2-required-persona"),
    )
    message = RequiredUserMessage(
        event_id=f"turn:{kind}",
        turn_id="turn",
        kind=kind,
        text="## Result\n\n- Receipt: `job-42`",
    )

    rendered = await packager.render(message)

    assert rendered.source_event_id == message.event_id
    assert rendered.kind == kind
    assert rendered.provenance == "persona_packager"
    assert rendered.fallback is False
    assert rendered.text.endswith(message.text)
    assert len(provider.required_calls) == 1
    _profile, inputs = provider.required_calls[0]
    assert set(inputs) == {
        "persona_block",
        "neutral_message",
        "message_kind",
        "request_id",
    }
    assert inputs["persona_block"] == "Address the user as Captain."
    assert inputs["neutral_message"] == message.text
    assert inputs["message_kind"] == kind


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["final", "clarification"])
@pytest.mark.parametrize(
    ("source", "error", "reason"),
    [
        (
            _packaging_source(usable=False, reason="persona_block_missing"),
            None,
            "persona_block_missing",
        ),
        (_packaging_source(), RuntimeError("provider offline"), "RuntimeError"),
    ],
)
async def test_required_message_rendering_has_deterministic_persona_fallback(
    kind, source, error, reason
):
    provider = _PackagingProvider(error=error)
    packager = _ConfiguredPersonaPackager(
        provider=provider,
        profile=ProviderProfile(
            "lightweight", "openrouter-api", "configured/model"
        ),
        source=source,
        request_id="request",
        logger=logging.getLogger("test.her-v2-required-persona-fallback"),
    )
    message = RequiredUserMessage(
        event_id=f"turn:{kind}",
        turn_id="turn",
        kind=kind,
        text="Keep this exact validated content.",
    )

    rendered = await packager.render(message)

    expected_prefix = (
        "Navigator 向您汇报：\n\n"
        if kind == "final"
        else "Navigator 想请您确认："
    )
    assert rendered.fallback is True
    assert rendered.provenance == "minimal_persona_fallback"
    assert rendered.error_type == reason
    assert rendered.text == f"{expected_prefix}{message.text}"
    assert len(provider.required_calls) == (0 if not source.usable else 1)


@pytest.mark.asyncio
async def test_required_fallback_never_drops_a_large_validated_report():
    provider = _PackagingProvider()
    packager = _ConfiguredPersonaPackager(
        provider=provider,
        profile=ProviderProfile(
            "lightweight", "openrouter-api", "configured/model"
        ),
        source=_packaging_source(usable=False, reason="persona_block_missing"),
        request_id="request",
        logger=logging.getLogger("test.her-v2-required-persona-large-fallback"),
    )
    report = "R" * (MAX_RENDERED_REQUIRED_MESSAGE_CHARS + 1)
    message = RequiredUserMessage(
        event_id="turn:final",
        turn_id="turn",
        kind="final",
        text=report,
    )

    rendered = await packager.render(message)

    assert rendered.fallback is True
    assert rendered.text.endswith(report)
    assert provider.required_calls == []
