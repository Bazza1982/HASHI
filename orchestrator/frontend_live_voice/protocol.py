"""Transport-neutral live-voice types; no authority is derived from client fields."""
from __future__ import annotations
from dataclasses import dataclass, asdict
import hashlib
import json
import re
from typing import Any, Mapping

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")

class LiveVoiceError(ValueError):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code, self.status = code, status


def identifier(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise LiveVoiceError("live_id_invalid")
    return value


def positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise LiveVoiceError("live_generation_invalid")
    return value


@dataclass(frozen=True)
class CallBinding:
    owner_id: str  # Obtained from existing authenticated ingress, never request JSON.
    instance_id: str
    instance_generation: str
    agent_id: str
    session_id: str
    context_generation: int
    call_id: str
    call_epoch: int
    provider_session_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, str) or not self.owner_id or len(self.owner_id) > 512:
            raise LiveVoiceError("live_owner_invalid")
        for name in ("instance_id", "instance_generation", "agent_id", "session_id", "call_id", "provider_session_id"):
            identifier(getattr(self, name))
        positive_int(self.context_generation)
        positive_int(self.call_epoch)

    def public_scope(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if k not in {"owner_id", "provider_session_id"}}

    def require_scope(self, expected: Mapping[str, Any], *, authenticated_owner: str) -> None:
        if authenticated_owner != self.owner_id:
            raise LiveVoiceError("live_not_found", 404)
        for key in ("instance_id", "instance_generation", "agent_id", "session_id", "context_generation"):
            if expected.get(key) != getattr(self, key):
                raise LiveVoiceError("live_scope_changed", 409)


@dataclass(frozen=True)
class Fragment:
    provider_event_id: str
    speaker: str
    text: str
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        identifier(self.provider_event_id)
        if self.speaker not in {"user", "assistant"} or not isinstance(self.text, str) or len(self.text.encode("utf-8")) > 65536:
            raise LiveVoiceError("live_fragment_invalid")
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (self.start_ms, self.end_ms)) or self.start_ms < 0 or self.end_ms < self.start_ms:
            raise LiveVoiceError("live_fragment_invalid")


def normalize_transcript(event: Mapping[str, Any]) -> Fragment | None:
    # Audio reflection is intentionally ignored, with no decoding or logging.
    types = {"session.input_transcript.delta": "user", "session.output_transcript.delta": "assistant"}
    speaker = types.get(event.get("type"))
    if speaker is None:
        return None
    return Fragment(event.get("event_id"), speaker, event.get("delta"), event.get("start_ms"), event.get("end_ms"))


def stable_digest(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
