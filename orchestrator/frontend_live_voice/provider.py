"""Small explicit Phone provider boundary owned by Frontend Functions.

Adapters translate media, context and events; PAO owns the logical call. Only
adapters with separately qualified real transports enter the default registry.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .protocol import LiveVoiceError


@dataclass(frozen=True)
class ProviderCapabilities:
    version: str
    max_input_messages: int
    max_instruction_tokens: int
    max_input_tokens: int
    max_session_seconds: int
    duplex_audio: bool = True
    proactive_opening: bool = True
    transcript: bool = True
    context_updates: bool = True
    task_results: bool = True
    recovery: bool = True
    playback_control: str = "client"
    output_completion: str = "unavailable"


class VoiceProvider(Protocol):
    provider_id: str
    capabilities: ProviderCapabilities

    def credential(self, secrets: Mapping[str, Any]) -> str: ...
    def validate_selection(self, model: str, voice: str) -> None: ...
    def media_descriptor(self) -> dict[str, Any]: ...
    def encode_history(
        self, messages: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]: ...
    def validate_session(
        self,
        instructions: str,
        model: str,
        voice: str,
        messages: Sequence[Mapping[str, Any]],
    ) -> None: ...
    def http_session(self) -> Any: ...
    async def fit_input(
        self, http: Any, **kwargs: Any
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]: ...
    async def create(
        self,
        http: Any,
        *,
        key: str,
        sdp: str,
        instructions: str,
        model: str,
        voice: str,
        input_messages: Sequence[Mapping[str, Any]],
        input_token_count_exact: int,
    ) -> Mapping[str, Any]: ...
    async def attach(self, http: Any, *, key: str, provider_session_id: str) -> Any: ...
    def normalize_event(self, raw: str) -> dict[str, Any] | None: ...
    def update(
        self, kind: str, content: str, delegation_id: str | None, event_id: str
    ) -> dict[str, Any]: ...
    def control(self, action: str, event_id: str) -> dict[str, Any]: ...
    def opening(self, goal: str, event_id: str) -> dict[str, Any]: ...


def default_registry() -> dict[str, VoiceProvider]:
    from .openai_live import OpenAILiveProvider

    adapter = OpenAILiveProvider()
    return {adapter.provider_id: adapter}


def select_provider(
    registry: Mapping[str, VoiceProvider], provider_id: str
) -> VoiceProvider:
    adapter = registry.get(provider_id)
    if adapter is None or adapter.provider_id != provider_id:
        raise LiveVoiceError("live_provider_unqualified", 503)
    capabilities = adapter.capabilities
    if not all(
        (
            capabilities.duplex_audio,
            capabilities.proactive_opening,
            capabilities.transcript,
            capabilities.context_updates,
            capabilities.task_results,
            capabilities.recovery,
        )
    ):
        raise LiveVoiceError("live_provider_capability_unqualified", 503)
    return adapter
