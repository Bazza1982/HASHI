from __future__ import annotations

import asyncio
import json
import logging

import pytest

from adapters.her_persona import HERPersonaPackagingSource
from adapters.her_v2 import _AdapterDelivery, _ConfiguredPersonaPackager
from orchestrator.her_v2.commentary import (
    CommentaryValidationError,
    DraftResponseCommentary,
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
async def test_draft_response_uses_typed_commentary_delivery_without_rewriting():
    timeline = []
    packager = _RecordingPackager(timeline)
    delivery = _RecordingDelivery(timeline)
    pipeline = PersonaCommentaryPipeline(packager=packager, delivery=delivery)
    draft = DraftResponseCommentary(
        event_id="turn:execution:draft",
        turn_id="turn",
        response="Exact Primary Execution response.",
    )

    accepted = await asyncio.gather(
        pipeline.publish_draft(draft),
        pipeline.publish_draft(draft),
    )

    assert accepted == [True, True]
    assert packager.calls == []
    assert len(delivery.calls) == 1
    packaged = delivery.calls[0]
    assert packaged.draft_response is True
    assert packaged.text == "DRAFT RESPONSE\n\nExact Primary Execution response."
    assert packaged.provenance == "primary_execution_draft"


@pytest.mark.asyncio
async def test_generic_delivery_boundary_rejects_raw_commentary_strings():
    delivery = _AdapterDelivery(
        lambda _event: None,
        allow_immediate_response=True,
    )

    for kind in ("commentary", "draft"):
        with pytest.raises(ValueError, match="raw commentary"):
            await delivery.deliver(
                kind=kind,
                text="unpackaged text",
                event_id=f"turn:raw:{kind}",
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

    async def package_persona_commentary(self, profile, **kwargs):
        self.calls.append((profile, kwargs))
        if self.error is not None:
            raise self.error
        return "Captain, three checks passed; final verification is running."

def _packaging_source(*, usable=True, reason=None, display_name="Navigator"):
    return HERPersonaPackagingSource(
        guidance="Address the user as Captain.",
        display_name=display_name,
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
async def test_declared_persona_commentary_agent_failure_logs_reason_and_falls_back(
    caplog,
):
    provider = _PackagingProvider()
    failure_reason = "Cannot preserve the original facts.\n" + ("detail " * 100)

    async def declared_failure(profile, **kwargs):
        provider.calls.append((profile, kwargs))
        return json.dumps(
            {
                "persona_commentary_agent_failed": True,
                "reason": failure_reason,
            }
        )

    provider.package_persona_commentary = declared_failure
    logger_name = "test.her-v2-declared-persona-commentary-failure"
    packager = _ConfiguredPersonaPackager(
        provider=provider,
        profile=ProviderProfile(
            "lightweight", "openrouter-api", "configured/model"
        ),
        source=_packaging_source(display_name="Public Navigator"),
        request_id="request",
        logger=logging.getLogger(logger_name),
    )

    with caplog.at_level(logging.WARNING, logger=logger_name):
        packaged = await packager.package(_neutral())

    assert packaged.fallback is True
    assert packaged.provenance == "minimal_persona_fallback"
    assert packaged.error_type == "persona_commentary_agent_failed"
    assert packaged.text.startswith("Public Navigator 向您汇报：")
    assert packaged.text.endswith(_neutral().text)
    assert failure_reason in caplog.text
    assert len(provider.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_output",
    [
        "Sorry, I cannot edit this commentary.",
        (
            '{"persona_commentary_agent_failed":"true",'
            '"reason":"String booleans are not the failure signal."}'
        ),
        (
            "```json\n"
            '{"persona_commentary_agent_failed":true,"reason":"fenced"}\n'
            "```"
        ),
        (
            "Failure details: "
            '{"persona_commentary_agent_failed":true,"reason":"prefixed"}'
        ),
    ],
)
async def test_other_outputs_are_not_inferred_to_be_persona_agent_failures(model_output):
    provider = _PackagingProvider()

    async def response(profile, **kwargs):
        provider.calls.append((profile, kwargs))
        return model_output

    provider.package_persona_commentary = response
    packager = _ConfiguredPersonaPackager(
        provider=provider,
        profile=ProviderProfile(
            "lightweight", "openrouter-api", "configured/model"
        ),
        source=_packaging_source(),
        request_id="request",
        logger=logging.getLogger("test.her-v2-strict-persona-failure-signal"),
    )

    packaged = await packager.package(_neutral())

    assert packaged.fallback is False
    assert packaged.provenance == "persona_packager"
    assert packaged.text == model_output
    assert len(provider.calls) == 1


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
async def test_non_sentinel_persona_response_is_not_classified_as_failure():
    provider = _PackagingProvider()

    async def refusal(profile, **kwargs):
        provider.calls.append((profile, kwargs))
        return "Sorry, I cannot rewrite that update."

    provider.package_persona_commentary = refusal
    packager = _ConfiguredPersonaPackager(
        provider=provider,
        profile=ProviderProfile(
            "lightweight", "openrouter-api", "configured/model"
        ),
        source=_packaging_source(),
        request_id="request",
        logger=logging.getLogger("test.her-v2-replan-persona-fact-fallback"),
    )
    commentary = NeutralCommentary(
        event_id="turn:execution-cycle:1:checkpoint:1:commentary",
        turn_id="turn",
        stage=Stage.REPLANNING,
        attempt=1,
        text=(
            "Progress is 60%. The plan is unchanged. "
            "Next: continue the current plan from verified evidence."
        ),
        required_facts=("60%", "PLAN IS UNCHANGED", "Next:"),
    )

    packaged = await packager.package(commentary)

    assert packaged.fallback is False
    assert packaged.provenance == "persona_packager"
    assert packaged.error_type == ""
    assert packaged.source_event_id == commentary.event_id
    assert packaged.text == "Sorry, I cannot rewrite that update."
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_model_commentary_fallback_uses_display_name_without_persona_call():
    provider = _PackagingProvider()
    resolved_display_name = "Public Navigator"
    packager = _ConfiguredPersonaPackager(
        provider=provider,
        profile=ProviderProfile(
            "lightweight", "openrouter-api", "configured/model"
        ),
        # Deliberately unlike the internal ``agent`` ID used by adapter tests.
        source=_packaging_source(display_name=resolved_display_name),
        request_id="request",
        logger=logging.getLogger("test.her-v2-replan-model-commentary-fallback"),
    )
    commentary = NeutralCommentary(
        event_id="turn:execution-cycle:1:checkpoint:1:commentary",
        turn_id="turn",
        stage=Stage.REPLANNING,
        attempt=1,
        text=(
            "Progress is 60%. The plan is unchanged. "
            "Next: continue from verified evidence."
        ),
        required_facts=("60%", "plan is unchanged", "Next:"),
        minimal_persona_fallback_reason="replan_model_commentary_fallback",
    )

    packaged = await packager.package(commentary)

    assert packaged.fallback is True
    assert packaged.provenance == "minimal_persona_fallback"
    assert packaged.error_type == "replan_model_commentary_fallback"
    assert packaged.text.startswith(f"{resolved_display_name} 向您汇报：")
    assert not packaged.text.startswith("agent 向您汇报：")
    assert all(fact in packaged.text for fact in commentary.required_facts)
    assert provider.calls == []


@pytest.mark.asyncio
async def test_required_clarification_reuses_commentary_persona_agent():
    provider = _PackagingProvider()

    async def render_clarification(profile, **kwargs):
        provider.calls.append((profile, kwargs))
        return f"Captain, {kwargs['neutral_commentary']}"

    provider.package_persona_commentary = render_clarification
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
        event_id="turn:clarification",
        turn_id="turn",
        kind="clarification",
        text="Which account should be changed?",
    )

    rendered = await packager.render(message)

    assert rendered.source_event_id == message.event_id
    assert rendered.kind == "clarification"
    assert rendered.provenance == "persona_packager"
    assert rendered.fallback is False
    assert rendered.text.endswith(message.text)
    assert len(provider.calls) == 1
    _profile, inputs = provider.calls[0]
    assert set(inputs) == {"persona_block", "neutral_commentary", "request_id"}
    assert inputs["persona_block"] == "Address the user as Captain."
    assert inputs["neutral_commentary"] == message.text


@pytest.mark.asyncio
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
        logger=logging.getLogger("test.her-v2-required-persona-fallback"),
    )
    message = RequiredUserMessage(
        event_id="turn:clarification",
        turn_id="turn",
        kind="clarification",
        text="Keep this exact validated content.",
    )

    rendered = await packager.render(message)

    assert rendered.fallback is True
    assert rendered.provenance == "minimal_persona_fallback"
    assert rendered.error_type == reason
    assert rendered.text == f"Navigator 想请您确认：{message.text}"
    assert len(provider.calls) == (0 if not source.usable else 1)


@pytest.mark.asyncio
async def test_required_clarification_declared_failure_logs_full_reason_and_falls_back(
    caplog,
):
    provider = _PackagingProvider()
    failure_reason = "Cannot preserve the clarification.\n" + ("detail " * 100)

    async def declared_failure(profile, **kwargs):
        provider.calls.append((profile, kwargs))
        return json.dumps(
            {
                "persona_commentary_agent_failed": True,
                "reason": failure_reason,
            }
        )

    provider.package_persona_commentary = declared_failure
    logger_name = "test.her-v2-required-clarification-declared-failure"
    packager = _ConfiguredPersonaPackager(
        provider=provider,
        profile=ProviderProfile(
            "lightweight", "openrouter-api", "configured/model"
        ),
        source=_packaging_source(display_name="Public Navigator"),
        request_id="request",
        logger=logging.getLogger(logger_name),
    )
    message = RequiredUserMessage(
        event_id="turn:clarification",
        turn_id="turn",
        kind="clarification",
        text="Which account should be changed?",
    )

    with caplog.at_level(logging.WARNING, logger=logger_name):
        rendered = await packager.render(message)

    assert rendered.fallback is True
    assert rendered.provenance == "minimal_persona_fallback"
    assert rendered.error_type == "persona_commentary_agent_failed"
    assert rendered.text == (
        "Public Navigator 想请您确认：Which account should be changed?"
    )
    assert failure_reason in caplog.text
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_required_fallback_never_drops_a_large_validated_clarification():
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
    clarification = "Q" * (MAX_RENDERED_REQUIRED_MESSAGE_CHARS + 1)
    message = RequiredUserMessage(
        event_id="turn:clarification",
        turn_id="turn",
        kind="clarification",
        text=clarification,
    )

    rendered = await packager.render(message)

    assert rendered.fallback is True
    assert rendered.text.endswith(clarification)
    assert provider.calls == []
