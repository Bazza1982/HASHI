"""Route-aware Safe Voice gating for native audio Turns.

Native Audio Direct/Immediate may answer from the original audio without ever
consuming local STT.  Safe Voice is therefore deferred until a Triage,
fallback, or tool-capable path actually asks to use/authorize that transcript.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from typing import Any


def _lock(state: dict[str, Any]) -> asyncio.Lock:
    candidate = state.get("gate_lock")
    if isinstance(candidate, asyncio.Lock):
        return candidate
    candidate = asyncio.Lock()
    state["gate_lock"] = candidate
    return candidate


async def _call(state: dict[str, Any], key: str) -> None:
    callback = state.get(key)
    if not callable(callback):
        return
    result = callback()
    if inspect.isawaitable(result):
        await result


def state_after_transcription(state: Mapping[str, Any]) -> str:
    """Resolve STT's initial state without prematurely invoking Safe Voice."""

    if not bool(state.get("safe_voice")):
        return "released"
    if bool(state.get("confirmation_requested")):
        return "pending_confirmation"
    if bool(state.get("native_audio_completed")):
        return "released"
    return "ready"


async def await_authorized_transcript(
    state: dict[str, Any] | None,
    *,
    require_confirmation: bool = True,
) -> tuple[str, str]:
    """Wait for STT and request Safe Voice only for an actual consumer."""

    if not isinstance(state, dict):
        return "", "unavailable"
    state_lock = _lock(state)
    async with state_lock:
        if require_confirmation and bool(state.get("safe_voice")):
            state["confirmation_requested"] = True

    task = state.get("task")
    if isinstance(task, asyncio.Task) and not task.done():
        await asyncio.shield(task)
    ready_event = state.get("ready_event")
    if isinstance(ready_event, asyncio.Event) and not ready_event.is_set():
        await ready_event.wait()

    present_confirmation = False
    async with state_lock:
        status = str(state.get("status") or "unavailable")
        if (
            require_confirmation
            and bool(state.get("safe_voice"))
            and status in {"ready", "pending_confirmation"}
            and not bool(state.get("confirmation_presented"))
        ):
            state["confirmation_presented"] = True
            present_confirmation = True
    if present_confirmation:
        try:
            await _call(state, "request_confirmation")
        except Exception as exc:
            async with state_lock:
                state["confirmation_error"] = type(exc).__name__
                state["status"] = "unavailable"
                release_event = state.get("release_event")
                if isinstance(release_event, asyncio.Event):
                    release_event.set()

    release_event = state.get("release_event")
    if isinstance(release_event, asyncio.Event) and not release_event.is_set():
        await release_event.wait()
    return str(state.get("text") or "").strip(), str(
        state.get("status") or "unavailable"
    )


def response_has_native_audio(payload: Mapping[str, Any]) -> bool:
    if not bool(payload.get("success")):
        return False
    content = payload.get("content")
    return isinstance(content, (list, tuple)) and any(
        isinstance(part, Mapping)
        and str(part.get("type") or "").strip().casefold() == "audio"
        and bool(str(part.get("asset_id") or "").strip())
        for part in content
    )


async def complete_native_audio_response(
    state: dict[str, Any] | None,
    payload: Mapping[str, Any],
) -> bool:
    """Auto-release unconsumed STT after a native audio answer completes."""

    if not isinstance(state, dict) or not response_has_native_audio(payload):
        return False
    state_lock = _lock(state)
    should_release = False
    async with state_lock:
        state["native_audio_completed"] = True
        ready_event = state.get("ready_event")
        should_release = bool(
            str(state.get("status") or "") == "ready"
            and not state.get("confirmation_requested")
            and isinstance(ready_event, asyncio.Event)
            and ready_event.is_set()
        )
    if not should_release:
        return False
    return await release_deferred_transcript(state)


async def release_deferred_transcript(state: dict[str, Any] | None) -> bool:
    """Release a ready transcript without presenting a Safe Voice challenge."""

    if not isinstance(state, dict):
        return False
    state_lock = _lock(state)
    async with state_lock:
        if str(state.get("status") or "") != "ready":
            return str(state.get("status") or "") == "released"
        state["status"] = "releasing"
    try:
        await _call(state, "auto_release")
    except Exception as exc:
        async with state_lock:
            state["auto_release_error"] = type(exc).__name__
            state["status"] = "ready"
        return False
    async with state_lock:
        state["status"] = "released"
        release_event = state.get("release_event")
        if isinstance(release_event, asyncio.Event):
            release_event.set()
    return True


__all__ = [
    "await_authorized_transcript",
    "complete_native_audio_response",
    "release_deferred_transcript",
    "response_has_native_audio",
    "state_after_transcription",
]
