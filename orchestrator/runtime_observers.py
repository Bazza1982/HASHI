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
        for observer in runtime._post_turn_observers:
            attach_runtime = getattr(observer, "attach_runtime", None)
            if callable(attach_runtime):
                attach_runtime(runtime)
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


def notify_right_brain_started(
    runtime: Any,
    item: Any,
    user_text: str,
    *,
    final_prompt: str,
    is_bridge_request: bool,
) -> None:
    _notify_observer_turn_event(
        runtime,
        item,
        user_text,
        is_bridge_request=is_bridge_request,
        method_name="on_right_brain_started",
        event_name="right-brain start",
        final_prompt=final_prompt,
    )


def notify_right_brain_completed(
    runtime: Any,
    item: Any,
    user_text: str,
    assistant_text: str,
    *,
    is_bridge_request: bool,
    completion_path: str,
) -> None:
    _notify_observer_turn_event(
        runtime,
        item,
        user_text,
        is_bridge_request=is_bridge_request,
        method_name="on_right_brain_completed",
        event_name="right-brain completion",
        assistant_text=assistant_text,
        completion_path=completion_path,
    )


def notify_right_brain_interrupted(
    runtime: Any,
    item: Any,
    user_text: str,
    *,
    is_bridge_request: bool,
    reason: str,
    error: str | None = None,
) -> None:
    _notify_observer_turn_event(
        runtime,
        item,
        user_text,
        is_bridge_request=is_bridge_request,
        method_name="on_right_brain_interrupted",
        event_name="right-brain interruption",
        reason=reason,
        error=error or "",
    )


def _notify_observer_turn_event(
    runtime: Any,
    item: Any,
    user_text: str,
    *,
    is_bridge_request: bool,
    method_name: str,
    event_name: str,
    **metadata: Any,
) -> None:
    if not runtime._post_turn_observers:
        return
    request = TurnObservationRequest(
        request_id=item.request_id,
        source=item.source,
        user_text=user_text,
        assistant_text=str(metadata.pop("assistant_text", "") or ""),
        model_name=runtime.get_current_model(),
        chat_id=item.chat_id,
        summary=item.summary,
        metadata=metadata,
    )
    for observer in runtime._post_turn_observers:
        try:
            if not observer.should_observe(item.source, is_bridge_request=is_bridge_request):
                continue
            handler = getattr(observer, method_name, None)
            if callable(handler):
                handler(request)
        except Exception as exc:
            runtime.logger.warning(
                "Post-turn observer failed during %s for %s via %s: %s",
                event_name,
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
