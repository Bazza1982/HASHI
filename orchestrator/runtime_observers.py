from __future__ import annotations

from typing import Any

from orchestrator.post_turn_observer import (
    PreTurnContextProvider,
    TurnContextRequest,
    TurnObservationRequest,
)
from orchestrator.post_turn_registry import build_post_turn_observers


def reload_post_turn_observers(runtime: Any) -> None:
    try:
        runtime._post_turn_observers = build_post_turn_observers(
            workspace_dir=runtime.workspace_dir,
            bridge_memory_store=runtime.memory_store,
            backend_invoker=runtime._sidecar_invoker,
            backend_context_getter=runtime._sidecar_context_getter,
        )
        runtime._pre_turn_context_providers = [
            observer for observer in runtime._post_turn_observers
            if isinstance(observer, PreTurnContextProvider)
        ]
        runtime.logger.info(
            "Turn observers initialized: post_count=%s pre_count=%s",
            len(runtime._post_turn_observers),
            len(runtime._pre_turn_context_providers),
        )
    except Exception as exc:
        runtime._post_turn_observers = []
        runtime._pre_turn_context_providers = []
        runtime.logger.warning("Failed to initialize post-turn observers: %s", exc)


async def build_pre_turn_context_sections(
    runtime: Any,
    item: Any,
    user_text: str,
    *,
    is_bridge_request: bool,
) -> list[tuple[str, str]]:
    if not runtime._pre_turn_context_providers:
        return []
    request = TurnContextRequest(
        request_id=item.request_id,
        source=item.source,
        user_text=user_text,
        model_name=runtime.get_current_model(),
        chat_id=item.chat_id,
        summary=item.summary,
        metadata={},
    )
    sections: list[tuple[str, str]] = []
    for provider in runtime._pre_turn_context_providers:
        try:
            if not provider.should_provide(item.source, is_bridge_request=is_bridge_request):
                continue
            sections.extend(await provider.build_context_sections(request))
        except Exception as exc:
            runtime.logger.warning(
                "Pre-turn context provider failed for %s via %s: %s",
                item.request_id,
                type(provider).__name__,
                exc,
            )
    return sections


def schedule_post_turn_observers(
    runtime: Any,
    item: Any,
    user_text: str,
    assistant_text: str,
    *,
    is_bridge_request: bool,
) -> None:
    if not runtime._post_turn_observers:
        return
    request = TurnObservationRequest(
        request_id=item.request_id,
        source=item.source,
        user_text=user_text,
        assistant_text=assistant_text,
        model_name=runtime.get_current_model(),
        chat_id=item.chat_id,
        summary=item.summary,
        metadata={},
    )
    for observer in runtime._post_turn_observers:
        try:
            if observer.should_observe(item.source, is_bridge_request=is_bridge_request):
                observer.schedule_observation(request, runtime._background_tasks)
        except Exception as exc:
            runtime.logger.warning(
                "Post-turn observer failed to schedule for %s via %s: %s",
                item.request_id,
                type(observer).__name__,
                exc,
            )


def observer_workspace_keep_names(runtime: Any) -> set[str]:
    keep_names: set[str] = set()
    for observer in runtime._post_turn_observers:
        try:
            keep_names.update(str(name) for name in observer.workspace_files_to_preserve())
        except Exception as exc:
            runtime.logger.warning(
                "Post-turn observer failed to report preserved files via %s: %s",
                type(observer).__name__,
                exc,
            )
    return keep_names
