"""Freeze a bounded transcript slice for client delegation. This builder never admits a Run."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from collections.abc import Iterable
from .protocol import CallBinding, Fragment, LiveVoiceError, identifier, stable_digest

@dataclass(frozen=True)
class Proposal:
    delegation_id: str
    version: int
    text: str
    source_event_ids: tuple[str, ...]
    digest: str
    ambiguous: bool
    cutoff_ms: int
    expires_at: str
    execution_text: str = ""


def build_proposal(binding: CallBinding, delegation_id: str, fragments: Iterable[Fragment], *,
                   after_ms: int, cutoff_ms: int, expires_at: str, version: int = 1) -> Proposal:
    identifier(delegation_id)
    if any(isinstance(x, bool) or not isinstance(x, int) for x in (after_ms, cutoff_ms, version)) or not 0 <= after_ms <= cutoff_ms or version < 1:
        raise LiveVoiceError("live_proposal_window_invalid")
    # after_ms is a committed application watermark, not an inferred turn boundary.
    source = sorted((f for f in fragments if f.speaker == "user" and f.end_ms > after_ms and f.start_ms < cutoff_ms),
                    key=lambda f: (f.start_ms, f.end_ms, f.provider_event_id))
    seen: set[str] = set()
    unique: list[Fragment] = []
    for f in source:
        if f.provider_event_id not in seen:
            unique.append(f)
            seen.add(f.provider_event_id)
    text = "".join(f.text for f in unique)  # Original words, spaces, repetitions.
    if len(text.encode("utf-8")) > 32768:
        raise LiveVoiceError("live_proposal_too_large", 413)
    ambiguous = not text.strip() or any(f.start_ms < after_ms or f.end_ms > cutoff_ms for f in unique)
    material = {"scope": binding.public_scope(), "delegation_id": delegation_id, "version": version,
                "source": [asdict(f) for f in unique], "after_ms": after_ms, "cutoff_ms": cutoff_ms,
                "expires_at": expires_at}
    return Proposal(delegation_id, version, text, tuple(f.provider_event_id for f in unique), stable_digest(material), ambiguous, cutoff_ms, expires_at)
