from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any

from orchestrator.post_turn_observer import PostTurnObserver, PreTurnContextProvider, TurnContextRequest, TurnObservationRequest

from .layer import EmotionalSelfLayer
from .models import EmergentTurnState, TurnContext


class AnattaPostTurnObserver(PostTurnObserver, PreTurnContextProvider):
    STATE_CACHE_TTL = timedelta(minutes=5)
    STATE_CACHE_MAX = 100

    def __init__(self, layer: EmotionalSelfLayer):
        self.layer = layer
        self.logger = logging.getLogger("Anatta.PostTurnObserver")
        self._state_cache: dict[str, tuple[datetime, EmergentTurnState]] = {}

    def should_observe(self, source: str, *, is_bridge_request: bool) -> bool:
        if is_bridge_request:
            return False
        if source in {"startup", "system"}:
            return False
        mode = self._mode()
        return mode in {"shadow", "on"}

    def should_provide(self, source: str, *, is_bridge_request: bool) -> bool:
        if is_bridge_request:
            return False
        if source in {"startup", "system"}:
            return False
        return self._mode() == "on"

    async def build_context_sections(
        self,
        request: TurnContextRequest,
    ) -> list[tuple[str, str]]:
        if self._mode() != "on":
            return []
        turn_context = self._build_turn_context(request)
        state, injection = await self.layer.build_turn_state(turn_context, request.model_name)
        self._cache_state(request.request_id, state)
        if not self.layer.should_inject_prompt():
            return []
        if not injection.metadata.get("inject"):
            return []
        if not injection.body.strip():
            return []
        self.logger.info(
            "Anatta pre-turn context provided for %s (dominant_drives=%s)",
            request.request_id,
            ",".join(state.dominant_drives),
        )
        return [(injection.title, injection.body)]

    def schedule_observation(
        self,
        request: TurnObservationRequest,
        background_tasks: set[asyncio.Task[Any]],
    ) -> None:
        mode = self._mode()
        if mode not in {"shadow", "on"}:
            return
        self.logger.info("Scheduling Anatta %s task for %s", mode, request.request_id)
        task = asyncio.create_task(self._run_observation(request, mode))
        background_tasks.add(task)

        def _done_callback(completed: asyncio.Task[Any]) -> None:
            background_tasks.discard(completed)
            with suppress(asyncio.CancelledError):
                exc = completed.exception()
                if exc:
                    self.logger.warning("Anatta %s task failed for %s: %s", mode, request.request_id, exc)

        task.add_done_callback(_done_callback)

    def workspace_files_to_preserve(self) -> frozenset[str]:
        return frozenset({"anatta_config.json"})

    async def _run_observation(self, request: TurnObservationRequest, mode: str) -> None:
        self.logger.info("Anatta %s task starting for %s", mode, request.request_id)
        turn_context = self._build_turn_context(request)
        state = self._pop_cached_state(request.request_id)
        if state is None:
            state, _ = await self.layer.build_turn_state(turn_context, request.model_name)
        await self.layer.record_async(turn_context, request.assistant_text, state)
        self.logger.info(
            "Anatta %s recorded for %s (dominant_drives=%s)",
            mode,
            request.request_id,
            ",".join(state.dominant_drives),
        )

    def _build_turn_context(self, request: TurnContextRequest | TurnObservationRequest) -> TurnContext:
        metadata = dict(request.metadata)
        metadata.setdefault("request_id", request.request_id)
        metadata.setdefault("summary", request.summary)
        metadata.setdefault("source", request.source)
        relationship_key = self.layer.resolve_relationship_key(
            source=request.source,
            chat_id=request.chat_id,
            metadata=metadata,
        )
        return TurnContext(
            user_text=request.user_text,
            source=request.source,
            request_id=request.request_id,
            relationship_key=relationship_key,
            bridge_recent_turns=self.layer.bridge_adapter.get_recent_turns(limit=8),
            bridge_memories=self.layer.bridge_adapter.retrieve_memories(request.user_text, limit=6),
            metadata=metadata,
        )

    def _cache_state(self, request_id: str | None, state: EmergentTurnState) -> None:
        if not request_id:
            return
        self._prune_state_cache()
        self._state_cache[str(request_id)] = (datetime.now(timezone.utc), state)
        while len(self._state_cache) > self.STATE_CACHE_MAX:
            oldest_key = min(self._state_cache, key=lambda key: self._state_cache[key][0])
            self._state_cache.pop(oldest_key, None)

    def _pop_cached_state(self, request_id: str | None) -> EmergentTurnState | None:
        if not request_id:
            return None
        self._prune_state_cache()
        payload = self._state_cache.pop(str(request_id), None)
        if payload is None:
            return None
        return payload[1]

    def _prune_state_cache(self) -> None:
        cutoff = datetime.now(timezone.utc) - self.STATE_CACHE_TTL
        expired = [key for key, (created_at, _state) in self._state_cache.items() if created_at < cutoff]
        for key in expired:
            self._state_cache.pop(key, None)

    def _mode(self) -> str:
        try:
            return str(self.layer.mode())
        except Exception:
            return "off"


def build_post_turn_observer(
    *,
    workspace_dir: Any,
    bridge_memory_store: Any,
    backend_invoker: Any | None = None,
    backend_context_getter: Any | None = None,
    options: dict[str, Any] | None = None,
) -> AnattaPostTurnObserver:
    from .layer import build_anatta_layer

    _ = options
    layer = build_anatta_layer(
        workspace_dir=workspace_dir,
        bridge_memory_store=bridge_memory_store,
        backend_invoker=backend_invoker,
        backend_context_getter=backend_context_getter,
    )
    return AnattaPostTurnObserver(layer)
