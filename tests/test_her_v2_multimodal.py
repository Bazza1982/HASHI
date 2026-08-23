from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from adapters.base import BackendResponse
from adapters.her_v2_provider import HashiStageProvider, _MediaRoutingToolRegistry
from adapters.stream_events import KIND_TEXT_DELTA, KIND_TOOL_START, StreamEvent
from orchestrator.her_v2.config import ProviderProfile
from orchestrator.her_v2.interfaces import ProviderFailureCode, StageInvocationError
from orchestrator.her_v2.models import (
    Effort,
    Stage,
    StageRequest,
    TriageClassification,
)
from orchestrator.multimodal_contract import (
    InputCapability,
    attachment_manifest,
    canonical_request_content,
)


_MISSING = object()


def _content(*, include_audio: bool = False):
    parts = [
        {"type": "text", "item_index": 1, "text": "Inspect the attachments."},
        {
            "type": "media",
            "item_index": 2,
            "attachment_id": "attachment-image",
            "modality": "image",
            "kind": "photo",
            "mime_type": "image/png",
            "filename": "image.png",
            "caption": "",
            "local_ref": "/authorized/image.png",
            "size_bytes": 8,
            "sha256": "1" * 64,
            "transport": {"message_id": 1},
        },
    ]
    if include_audio:
        parts.append(
            {
                "type": "media",
                "item_index": 3,
                "attachment_id": "attachment-audio",
                "modality": "audio",
                "kind": "audio",
                "mime_type": "audio/ogg",
                "filename": "audio.ogg",
                "caption": "",
                "local_ref": "/authorized/audio.ogg",
                "size_bytes": 12,
                "sha256": "2" * 64,
                "transport": {"message_id": 2},
            }
        )
    return canonical_request_content(parts)


def _request(
    stage: Stage,
    content,
    *,
    allow_tools: bool = False,
    allow_side_effects: bool = False,
    force_local_media_fallback: bool = False,
):
    return StageRequest(
        turn_id="turn-media",
        request_ref="request-media",
        stage=stage,
        role=stage.value,
        attempt=1,
        goal="Inspect the attachments.",
        classification=TriageClassification.SIMPLE_TASK,
        effort=Effort.LOW,
        context={},
        request_content=content,
        attachment_manifest=attachment_manifest(content),
        force_local_media_fallback=force_local_media_fallback,
        allow_tools=allow_tools,
        allow_side_effects=allow_side_effects,
    )


class _Backend:
    def __init__(
        self,
        responses,
        *,
        supports_tools: bool = False,
        emit_tool_activity: bool = False,
        emit_text_activity: bool = False,
    ):
        self.responses = list(responses)
        self.calls = []
        self.config = SimpleNamespace(extra={}, system_md=None, name="media-test")
        self.capabilities = SimpleNamespace(supports_tool_use=supports_tools)
        self.input_capability = InputCapability(
            provider="fake-api",
            model="native-image-model",
            input_modalities=frozenset({"text", "image"}),
            input_transports={"image": ("data_url",)},
            source="test",
        )
        self.tool_registry = None
        self.sys_prompt = ""
        self.shutdown_called = False
        self.emit_tool_activity = emit_tool_activity
        self.emit_text_activity = emit_text_activity

    def resolve_input_capability(self):
        return self.input_capability

    async def initialize(self):
        return True

    async def generate_response(
        self,
        prompt,
        request_id,
        *,
        is_retry=False,
        silent=False,
        on_stream_event=None,
        request_content=_MISSING,
    ):
        del silent
        self.calls.append(
            {
                "prompt": prompt,
                "request_id": request_id,
                "is_retry": is_retry,
                "request_content": request_content,
            }
        )
        if self.emit_tool_activity and on_stream_event is not None:
            await on_stream_event(
                StreamEvent(
                    kind=KIND_TOOL_START,
                    summary="unexpected tool activity",
                    tool_name="media_read",
                )
                )
        if self.emit_text_activity and on_stream_event is not None:
            await on_stream_event(
                StreamEvent(kind=KIND_TEXT_DELTA, summary="partial provider output")
            )
        return self.responses.pop(0)

    async def shutdown(self):
        self.shutdown_called = True


class _Manager:
    privacy_level = 1

    def __init__(self, backend):
        self.backend = backend

    def create_ephemeral_backend(self, engine, target_model=None):
        assert (engine, target_model) == ("fake-api", "native-image-model")
        return self.backend


class _MediaRegistry:
    max_loops = None
    audit_context = {}

    def is_allowed(self, name):
        return name == "media_read"

    def allowed_tool_names(self):
        return ("media_read",)

    def is_read_only(self, name):
        return name == "media_read"

    def get_tool_definitions(self, tiers=None):
        del tiers
        return [{"type": "function", "function": {"name": "media_read"}}]

    async def execute(self, tool_name, arguments, tool_call_id=""):
        return SimpleNamespace(
            tool_call_id=tool_call_id,
            output=f"read:{tool_name}:{arguments['path']}",
            is_error=False,
        )


def _profile():
    return ProviderProfile("test", "fake-api", "native-image-model")


@pytest.mark.asyncio
async def test_local_fallback_rechecks_integrity_before_stage_routing(tmp_path):
    image = tmp_path / "image.png"
    original = b"\x89PNG\r\n\x1a\noriginal"
    image.write_bytes(original)
    content = canonical_request_content(
        [
            {"type": "text", "item_index": 1, "text": "Inspect it."},
            {
                "type": "media",
                "item_index": 2,
                "attachment_id": "attachment-image",
                "modality": "image",
                "kind": "photo",
                "mime_type": "image/png",
                "filename": image.name,
                "caption": "",
                "local_ref": str(image),
                "size_bytes": len(original),
                "sha256": hashlib.sha256(original).hexdigest(),
                "transport": {"message_id": 1},
            },
        ]
    )

    class _ValidatedBackend(_Backend):
        def authorized_media_roots(self):
            return (tmp_path,)

    backend = _ValidatedBackend([], supports_tools=True)
    backend.input_capability = InputCapability(
        provider="fake-api",
        model="text-only-model",
        input_modalities=frozenset({"text"}),
        input_transports={},
        source="test",
    )
    provider = HashiStageProvider(
        backend_manager=_Manager(backend),
        tool_registry=_MediaRegistry(),
    )
    image.write_bytes(b"\x89PNG\r\n\x1a\nchanged")

    with pytest.raises(StageInvocationError) as captured:
        await provider.invoke(
            _profile(),
            _request(Stage.EXECUTION, content, allow_tools=True),
        )

    assert captured.value.code is ProviderFailureCode.MEDIA_INTEGRITY_CHANGED
    assert captured.value.details["attachment_id"] == "attachment-image"
    assert backend.calls == []


@pytest.mark.asyncio
async def test_typed_fallback_releases_native_attachment_duplicate_guard():
    registry = _MediaRoutingToolRegistry(
        _MediaRegistry(),
        native_attachment_ids={"attachment-image"},
        native_local_refs={"/authorized/image.png", "image.png"},
        all_media_native=False,
    )

    blocked = await registry.execute(
        "media_read",
        {"path": "/authorized/image.png"},
        "call-before-fallback",
    )
    registry.enable_local_media_fallback({"attachment-image"})
    allowed = await registry.execute(
        "media_read",
        {"path": "/authorized/image.png"},
        "call-after-fallback",
    )

    assert blocked.is_error is True
    assert allowed.is_error is False
    assert allowed.output == "read:media_read:/authorized/image.png"


@pytest.mark.asyncio
async def test_allow_tools_false_still_passes_native_media_to_stage_backend():
    content = _content()
    backend = _Backend(
        [
            BackendResponse(
                text="",
                duration_ms=1,
                structured_data={"classification": "DIRECT_RESPONSE"},
            )
        ]
    )
    provider = HashiStageProvider(backend_manager=_Manager(backend))

    result = await provider.invoke(
        _profile(),
        _request(Stage.TRIAGE, content, allow_tools=False),
    )

    assert backend.calls[0]["request_content"] == content
    assert backend.tool_registry is None
    assert result.media_routing[0]["route"] == "native"
    assert backend.shutdown_called is True


@pytest.mark.asyncio
async def test_mixed_stage_request_sends_only_native_subset_to_provider():
    content = _content(include_audio=True)
    backend = _Backend(
        [
            BackendResponse(
                text="",
                duration_ms=1,
                structured_data={
                    "disposition": "COMPLETED",
                    "summary": "Inspected both inputs.",
                },
            )
        ],
        supports_tools=True,
    )
    provider = HashiStageProvider(
        backend_manager=_Manager(backend),
        tool_registry=_MediaRegistry(),
    )

    result = await provider.invoke(
        _profile(),
        _request(
            Stage.EXECUTION,
            content,
            allow_tools=True,
            allow_side_effects=True,
        ),
    )

    sent_ids = [
        part["attachment_id"]
        for part in backend.calls[0]["request_content"]["parts"]
        if part["type"] == "media"
    ]
    assert sent_ids == ["attachment-image"]
    assert [item["route"] for item in result.media_routing] == [
        "native",
        "local_fallback",
    ]


@pytest.mark.asyncio
async def test_mixed_stage_merges_adapter_fallback_for_native_subset():
    content = _content(include_audio=True)
    backend = _Backend(
        [
            BackendResponse(
                text="",
                duration_ms=1,
                structured_data={
                    "disposition": "COMPLETED",
                    "summary": "Inspected both inputs.",
                },
                stream_metadata={
                    "multimodal_fallback_attempted": True,
                    "multimodal_routing": [
                        {
                            "attachment_id": "attachment-image",
                            "item_index": 2,
                            "modality": "image",
                            "route": "local_fallback",
                            "reason": "provider_typed_modality_unsupported",
                            "transport": None,
                        }
                    ],
                },
            )
        ],
        supports_tools=True,
    )
    provider = HashiStageProvider(
        backend_manager=_Manager(backend),
        tool_registry=_MediaRegistry(),
    )

    result = await provider.invoke(
        _profile(),
        _request(
            Stage.EXECUTION,
            content,
            allow_tools=True,
            allow_side_effects=True,
        ),
    )

    assert [item["attachment_id"] for item in result.media_routing] == [
        "attachment-image",
        "attachment-audio",
    ]
    assert [item["route"] for item in result.media_routing] == [
        "local_fallback",
        "local_fallback",
    ]
    assert result.media_routing[0]["reason"] == (
        "provider_typed_modality_unsupported"
    )


@pytest.mark.asyncio
async def test_typed_modality_failure_gets_one_safe_text_only_replay():
    content = _content()
    backend = _Backend(
        [
            BackendResponse(
                text="",
                duration_ms=1,
                error="provider rejected image input",
                is_success=False,
                error_code=ProviderFailureCode.PROVIDER_MODALITY_UNSUPPORTED.value,
                error_retryable=False,
            ),
            BackendResponse(
                text="",
                duration_ms=1,
                structured_data={"classification": "SIMPLE_TASK"},
            ),
        ],
        supports_tools=True,
    )
    provider = HashiStageProvider(
        backend_manager=_Manager(backend),
        tool_registry=_MediaRegistry(),
    )

    result = await provider.invoke(
        _profile(),
        _request(Stage.TRIAGE, content, allow_tools=True),
    )

    assert len(backend.calls) == 2
    assert backend.calls[0]["request_content"] == content
    assert backend.calls[1]["request_content"] is _MISSING
    assert backend.calls[1]["is_retry"] is True
    assert result.media_routing[0]["reason"] == (
        "provider_typed_modality_unsupported"
    )


@pytest.mark.asyncio
async def test_typed_modality_failure_after_tool_activity_is_not_replayed():
    content = _content()
    backend = _Backend(
        [
            BackendResponse(
                text="",
                duration_ms=1,
                error="provider rejected image input after activity",
                is_success=False,
                error_code=ProviderFailureCode.PROVIDER_MODALITY_UNSUPPORTED.value,
                error_retryable=False,
            )
        ],
        supports_tools=True,
        emit_tool_activity=True,
    )
    provider = HashiStageProvider(
        backend_manager=_Manager(backend),
        tool_registry=_MediaRegistry(),
    )

    with pytest.raises(StageInvocationError) as captured:
        await provider.invoke(
            _profile(),
            _request(Stage.TRIAGE, content, allow_tools=True),
        )

    assert captured.value.code is ProviderFailureCode.PROVIDER_MODALITY_UNSUPPORTED
    assert len(backend.calls) == 1


@pytest.mark.asyncio
async def test_typed_modality_failure_after_text_activity_is_not_replayed():
    content = _content()
    backend = _Backend(
        [
            BackendResponse(
                text="",
                duration_ms=1,
                error="provider rejected image input after partial output",
                is_success=False,
                error_code=ProviderFailureCode.PROVIDER_MODALITY_UNSUPPORTED.value,
                error_retryable=False,
            )
        ],
        supports_tools=True,
        emit_text_activity=True,
    )
    provider = HashiStageProvider(
        backend_manager=_Manager(backend),
        tool_registry=_MediaRegistry(),
    )

    with pytest.raises(StageInvocationError) as captured:
        await provider.invoke(
            _profile(),
            _request(Stage.TRIAGE, content, allow_tools=True),
        )

    assert captured.value.code is ProviderFailureCode.PROVIDER_MODALITY_UNSUPPORTED
    assert len(backend.calls) == 1


@pytest.mark.asyncio
async def test_tool_free_stage_fails_instead_of_silently_dropping_unsupported_media():
    content = _content()
    backend = _Backend([])
    backend.input_capability = InputCapability(
        provider="fake-api",
        model="text-only-model",
        input_modalities=frozenset({"text"}),
        input_transports={},
        source="test",
    )
    provider = HashiStageProvider(backend_manager=_Manager(backend))

    with pytest.raises(StageInvocationError) as captured:
        await provider.invoke(
            _profile(),
            _request(Stage.TRIAGE, content, allow_tools=False),
        )

    assert captured.value.code is ProviderFailureCode.PROVIDER_MODALITY_UNSUPPORTED
    assert backend.calls == []


@pytest.mark.asyncio
async def test_adapter_media_fallback_is_not_replayed_again_by_her():
    content = _content()
    backend = _Backend(
        [
            BackendResponse(
                text="",
                duration_ms=1,
                error="fallback replay also failed",
                is_success=False,
                error_code=ProviderFailureCode.PROVIDER_MODALITY_UNSUPPORTED.value,
                error_retryable=False,
                stream_metadata={
                    "multimodal_fallback_attempted": True,
                    "multimodal_routing": [
                        {
                            "attachment_id": "attachment-image",
                            "item_index": 2,
                            "modality": "image",
                            "route": "local_fallback",
                            "reason": "provider_typed_modality_unsupported",
                            "transport": None,
                        }
                    ],
                },
            )
        ],
        supports_tools=True,
    )
    provider = HashiStageProvider(
        backend_manager=_Manager(backend),
        tool_registry=_MediaRegistry(),
    )

    with pytest.raises(StageInvocationError) as captured:
        await provider.invoke(
            _profile(),
            _request(Stage.TRIAGE, content, allow_tools=True),
        )

    assert captured.value.code is ProviderFailureCode.PROVIDER_MODALITY_UNSUPPORTED
    assert len(backend.calls) == 1


@pytest.mark.asyncio
async def test_retry_after_typed_drift_stays_on_local_route_without_resending_media():
    content = _content()
    backend = _Backend(
        [
            BackendResponse(
                text="",
                duration_ms=1,
                structured_data={"classification": "SIMPLE_TASK"},
            )
        ],
        supports_tools=True,
    )
    provider = HashiStageProvider(
        backend_manager=_Manager(backend),
        tool_registry=_MediaRegistry(),
    )

    result = await provider.invoke(
        _profile(),
        _request(
            Stage.TRIAGE,
            content,
            allow_tools=True,
            force_local_media_fallback=True,
        ),
    )

    assert backend.calls[0]["request_content"] is _MISSING
    assert result.media_routing[0]["route"] == "local_fallback"
    assert result.media_routing[0]["reason"] == (
        "provider_typed_modality_unsupported"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "error_code"),
    [
        (Stage.IMMEDIATE_RESPONSE, ProviderFailureCode.PROVIDER_MODALITY_UNSUPPORTED),
        (Stage.TRIAGE, ProviderFailureCode.PROVIDER_AUTHENTICATION_FAILED),
        (Stage.TRIAGE, ProviderFailureCode.PROVIDER_RATE_LIMITED),
    ],
)
async def test_unsafe_or_unrelated_provider_failures_do_not_trigger_media_replay(
    stage,
    error_code,
):
    content = _content()
    backend = _Backend(
        [
            BackendResponse(
                text="",
                duration_ms=1,
                error="provider request failed",
                is_success=False,
                error_code=error_code.value,
                error_retryable=False,
            )
        ]
    )
    provider = HashiStageProvider(backend_manager=_Manager(backend))

    with pytest.raises(StageInvocationError) as captured:
        await provider.invoke(
            _profile(),
            _request(stage, content, allow_tools=False),
        )

    assert captured.value.code is error_code
    assert len(backend.calls) == 1
