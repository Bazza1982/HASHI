"""A bounded event/admission coordinator; process lifecycle is supplied by Functions."""
from __future__ import annotations
import hmac
from collections.abc import Mapping
from typing import Any
from .ports import AdmissionPort, DurableVoicePort
from .protocol import CallBinding, LiveVoiceError, identifier, normalize_transcript, positive_int, stable_digest

class LiveVoiceEventService:
    def __init__(self, durable: DurableVoicePort, admission: AdmissionPort):
        self.durable, self.admission = durable, admission

    async def on_provider_event(self, binding: CallBinding, event: Mapping[str, Any]) -> None:
        # CallBinding is resolved from the owning sideband, never a browser's callback payload.
        if event.get("type") in {"session.input_audio.append", "session.output_audio.delta"}:
            return
        fragment = normalize_transcript(event)
        if fragment is not None:
            await self.durable.append_fragment_once(binding, fragment)
            return
        if event.get("type") != "session.delegation.created":
            return
        delegation = event.get("delegation")
        if not isinstance(delegation, Mapping) or delegation.get("target") != "client":
            return
        delegation_id = identifier(delegation.get("id"))
        offset = event.get("offset_ms")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise LiveVoiceError("live_delegation_invalid")
        if await self.durable.register_delegation_once(binding, delegation_id, offset):
            await self.durable.schedule_proposal(binding, delegation_id, offset)

    async def decide(self, binding: CallBinding, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if set(payload) != {"delegation_id", "proposal_version", "proposal_digest", "decision", "idempotency_key"}:
            raise LiveVoiceError("live_decision_invalid")
        if payload["decision"] not in {"confirm", "discard"}:
            raise LiveVoiceError("live_decision_invalid")
        key = identifier(payload["idempotency_key"])
        positive_int(payload["proposal_version"])
        request_digest = stable_digest(dict(payload))
        prior = await self.admission.find_decision(binding, idempotency_key=key, request_digest=request_digest)
        if prior is not None:
            return prior
        proposal = await self.durable.read_proposal(binding, identifier(payload["delegation_id"]))
        digest = payload["proposal_digest"]
        if not isinstance(digest, str) or not hmac.compare_digest(digest, proposal.digest) or payload["proposal_version"] != proposal.version:
            raise LiveVoiceError("live_proposal_changed", 409)
        if payload["decision"] == "confirm" and (proposal.ambiguous or not proposal.text.strip()):
            raise LiveVoiceError("live_clarification_required", 409)
        return await self.admission.decide_and_admit(binding, proposal, decision=payload["decision"],
                                                    idempotency_key=key, request_digest=request_digest)
