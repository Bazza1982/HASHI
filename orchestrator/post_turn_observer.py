from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class TurnObservationRequest:
    request_id: str
    source: str
    user_text: str
    assistant_text: str
    model_name: str
    chat_id: int | None = None
    summary: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TurnContextRequest:
    request_id: str
    source: str
    user_text: str
    model_name: str
    chat_id: int | None = None
    summary: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class PreTurnContextProvider(Protocol):
    def should_provide(self, source: str, *, is_bridge_request: bool) -> bool:
        ...

    async def build_context_sections(
        self,
        request: TurnContextRequest,
    ) -> list[tuple[str, str]]:
        ...

    def workspace_files_to_preserve(self) -> frozenset[str]:
        ...


@runtime_checkable
class PostTurnObserver(Protocol):
    def should_observe(self, source: str, *, is_bridge_request: bool) -> bool:
        ...

    def schedule_observation(
        self,
        request: TurnObservationRequest,
        background_tasks: set[asyncio.Task[Any]],
    ) -> None:
        ...

    def workspace_files_to_preserve(self) -> frozenset[str]:
        ...
