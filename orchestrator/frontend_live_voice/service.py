"""A bounded provider-event coordinator; process lifecycle is supplied by Functions."""
from __future__ import annotations
from collections.abc import Mapping
from typing import Any
from .ports import DurableVoicePort
from .protocol import CallBinding, LiveVoiceError, identifier, normalize_transcript

class LiveVoiceEventService:
    def __init__(self, durable: DurableVoicePort):
        self.durable = durable

    async def on_provider_event(self, binding: CallBinding, event: Mapping[str, Any]) -> None:
        # CallBinding is resolved from the owning sideband, never a browser's callback payload.
        if event.get("type") in {"session.input_audio.append", "session.output_audio.delta"}:
            return
        fragment = normalize_transcript(event)
        if fragment is not None:
            await self.durable.stage_fragment_once(binding, fragment)
            await self.durable.append_fragment_once(binding, fragment)
            observer = getattr(self.durable, "note_user_fragment", None)
            if fragment.speaker == "user" and callable(observer):
                await observer(binding, fragment)
            return
        if event.get("type") != "action.proposed":
            return
        delegation = event.get("delegation")
        if not isinstance(delegation, Mapping) or delegation.get("target") != "client":
            return
        delegation_id = identifier(delegation.get("id"))
        event_id = identifier(event.get("event_id"))
        offset = event.get("offset_ms")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise LiveVoiceError("live_delegation_invalid")
        await self.durable.stage_delegation_once(
            binding, event_id, delegation_id, offset
        )
        if await self.durable.register_delegation_once(binding, delegation_id, offset):
            await self.durable.schedule_proposal(binding, delegation_id, offset)
