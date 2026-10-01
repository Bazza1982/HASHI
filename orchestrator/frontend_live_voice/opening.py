"""PAO's once-per-logical-call opening, persisted beside the frozen call choice.

Provider acceptance, output observation and local player progress are separate
facts. A lost receipt never authorizes another opening request.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import time
from typing import Any
from uuid import uuid4

from .protocol import CallBinding


def new_opening() -> dict[str, Any]:
    return {
        "opening_id": f"opening-{uuid4().hex}",
        "state": "pending",
        "request_sent": False,
        "request_accepted": False,
        "output_observed": False,
        "playback_observed": False,
        "user_started": False,
        "provider_ready_epoch": 0,
        "media_ready_epoch": 0,
        "continuation_reserved": False,
        "continuation_sent": False,
        "continuation_accepted": False,
    }


def opening_goal(phone: Mapping[str, Any], *, continuation: bool = False) -> str:
    # PCM has already supplied the effective authority layers, persona and history.
    # No address, Agent identity, user name or invented task fact is baked in here.
    language = str(phone.get("language") or "auto")
    interface_language = str(
        phone.get("interface_language") or "the interface language supplied in context"
    )
    chosen = (
        f"Use the selected Phone language ({language}). "
        if language != "auto"
        else "Use the most recent relevant user's language, otherwise the explicit Persona default, "
        f"otherwise {interface_language}. "
    )
    return (
        ("No opening speech was observed during a continuous silent output window after the accepted "
         "opening instruction. Continue that opening now; if already beginning it, complete it without repeating. "
         if continuation else "This phone call has just connected. Begin speaking aloud now, without waiting for a new user message. ")
        + "Earlier conversation is context, not an opening already delivered in this call. Use the effective "
        "Persona, address preferences and instruction authority already supplied. "
        + chosen
        + "Begin with one short natural greeting. You may name a clear recent topic. "
        "Continue a pending user question only when its complete answer is already in startup context. "
        "A saved-result index is an address book, even when it includes source-derived structure; "
        "its excerpt is not the report to summarize aloud. If the full answer is absent, invite the "
        "caller to continue that topic, then listen. Use only verified context; this opening "
        "requests no lookup or action. Present the user's subject, "
        "not internal execution. If the caller speaks first or interrupts, yield and respond to their words."
    )


@dataclass
class OpeningAudioObservation:
    """Bounded volatile evidence only; any ambiguity prevents a continuation.

    The adapter checks the samples. Here both the provider timeline and paced
    local observation must cover the silence window; a burst of old frames does
    not substitute for observing a live silent stream after acknowledgment.
    """

    last_end_ms: float | None = None
    last_received: float = 0.0
    acknowledged_at: float | None = None
    acknowledged_end_ms: float | None = None
    first_after_ack: float | None = None
    uncertain: bool = False
    non_silent: bool = False
    maximum_arrival_gap: float = 1.0

    def observe(self, event: Mapping[str, Any], *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        activity = event.get("activity")
        if activity == "non_silent":
            self.non_silent = True
        elif activity != "silent":
            self.uncertain = True
        start, end = event.get("start_ms"), event.get("end_ms")
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or value < 0 or value > 2**53 - 1 or not math.isfinite(value) for value in (start, end)) or end <= start:
            self.uncertain = True
            return
        if self.last_end_ms is not None and start != self.last_end_ms:
            self.uncertain = True
        if self.last_end_ms is not None and not 0 <= now - self.last_received <= self.maximum_arrival_gap:
            self.uncertain = True
        if self.acknowledged_at is not None and self.first_after_ack is None:
            if not 0 <= now - self.acknowledged_at <= self.maximum_arrival_gap:
                self.uncertain = True
            self.first_after_ack = now
        self.last_end_ms, self.last_received = float(end), now

    def acknowledge(self, *, now: float | None = None) -> None:
        self.acknowledged_at = time.monotonic() if now is None else now
        self.acknowledged_end_ms = self.last_end_ms
        if self.last_end_ms is None or not 0 <= self.acknowledged_at - self.last_received <= self.maximum_arrival_gap:
            self.uncertain = True

    def silence_proof(self, *, elapsed_seconds: float, now: float | None = None) -> dict[str, Any] | None:
        now = time.monotonic() if now is None else now
        minimum = elapsed_seconds * 0.75
        if (self.uncertain or self.non_silent or self.acknowledged_at is None
                or self.acknowledged_end_ms is None or self.last_end_ms is None
                or self.first_after_ack is None or now - self.acknowledged_at < elapsed_seconds
                or self.last_received - self.first_after_ack < minimum
                or now - self.last_received > min(self.maximum_arrival_gap, elapsed_seconds / 2)
                or self.last_end_ms - self.acknowledged_end_ms < minimum * 1000):
            return None
        return {"kind": "provider_pcm_silence", "duration_ms": round(self.last_end_ms - self.acknowledged_end_ms),
                "observation_ms": round((now - self.acknowledged_at) * 1000)}


class CallOpening:
    def __init__(self, store: Any, audit: Any):
        self.store = store
        self.audit = audit

    def read(self, binding: CallBinding) -> dict[str, Any]:
        with self.store._lock, self.store._connection() as connection:
            row = connection.execute(
                "SELECT phone_config_json FROM live_calls WHERE call_id = ?",
                (binding.call_id,),
            ).fetchone()
        record = json.loads(row["phone_config_json"] or "{}") if row else {}
        return dict(record.get("opening") or {})

    def change(
        self, binding: CallBinding, signal: str, **detail: Any
    ) -> dict[str, Any]:
        with self.store._lock, self.store._connection() as connection:
            row = connection.execute(
                "SELECT * FROM live_calls WHERE call_id = ?", (binding.call_id,)
            ).fetchone()
            if (
                row is None
                or int(row["call_epoch"]) != binding.call_epoch
                or row["provider_session_id"] != binding.provider_session_id
            ):
                return {}
            record = json.loads(row["phone_config_json"] or "{}")
            state = dict(record.get("opening") or {})
            if not state:  # Pre-upgrade calls are never retroactively greeted.
                return {}
            before = dict(state)
            phase = row["phase"]
            terminal = phase in {"ending", "ended", "failed", "interrupted"}
            if signal == "claim":
                if (
                    terminal
                    or phase != "active"
                    or state.get("state") != "pending"
                    or state.get("user_started")
                    or state.get("provider_ready_epoch") != binding.call_epoch
                    or state.get("media_ready_epoch") != binding.call_epoch
                ):
                    return {}
                state.update(state="requested", request_epoch=binding.call_epoch)
            elif signal == "provider.ready" and not terminal:
                state["provider_ready_epoch"] = binding.call_epoch
            elif signal == "client.media_ready" and not terminal:
                if (
                    detail.get("input_active") is True
                    and detail.get("playback_unlocked") is True
                ):
                    state["media_ready_epoch"] = binding.call_epoch
            elif signal in {"user.started", "ending"}:
                if signal == "user.started":
                    state["user_started"] = True
                if state["state"] == "pending" or (
                    state["state"] == "requested" and not state.get("request_sent")
                ):
                    state["state"] = "skipped"
                elif state["state"] in {
                    "requested",
                    "accepted",
                    "generated",
                    "uncertain",
                }:
                    state["state"] = "interrupted"
            elif (
                signal == "sent"
                and not terminal
                and state["state"] == "requested"
                and not state.get("user_started")
            ):
                state["request_sent"] = True
                state["request_event_id"] = str(detail.get("event_id") or state["opening_id"])
                if state.get("continuation_reserved"):
                    state["continuation_sent"] = True
            elif signal == "continuation.claim":
                if (terminal or phase != "active" or state["state"] != "accepted"
                        or not state.get("request_accepted") or state.get("request_epoch") != binding.call_epoch
                        or state.get("continuation_reserved") or state.get("user_started")
                        or state.get("output_observed") or not detail.get("silence_proof")):
                    return {}
                state.update(state="requested", continuation_reserved=True,
                             continuation_evidence=dict(detail["silence_proof"]))
            elif signal == "accepted" and state.get("request_sent"):
                state["request_accepted"] = True
                if state.get("continuation_sent"):
                    state["continuation_accepted"] = True
                if state["state"] == "requested":
                    state["state"] = "accepted"
            elif signal == "speech.generated":
                if state.get("request_sent") and state["state"] in {
                    "requested",
                    "accepted",
                    "generated",
                    "uncertain",
                }:
                    state.update(
                        output_observed=True,
                        output_evidence="assistant_transcript",
                        output_attribution="estimated",
                    )
                    if state["state"] != "uncertain":
                        state["state"] = "generated"
            elif signal == "playback":
                if (
                    detail.get("opening_id") == state["opening_id"]
                    and detail.get("current_time_ms", 0) > 0
                    and state.get("output_observed")
                    and not terminal
                    and state["state"] not in {"skipped", "interrupted", "rejected"}
                ):
                    state.update(playback_observed=True, state="playback_observed")
            elif signal in {"uncertain", "rejected"}:
                if state["state"] in {"requested", "accepted", "generated"}:
                    state["state"] = signal
                    state["reason"] = str(detail.get("reason") or signal)
            if state == before:
                return state if signal != "claim" else {}
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            record["opening"] = state
            connection.execute(
                "UPDATE live_calls SET phone_config_json = ? WHERE call_id = ?",
                (
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                    binding.call_id,
                ),
            )
            self.store._append_event(
                connection,
                session_id=binding.session_id,
                run_id=None,
                kind="voice.live.opening.state",
                summary="Phone opening observation",
                detail={
                    "schema": "hashi.live_voice.event.v1",
                    "scope": binding.public_scope(),
                    **state,
                },
            )
        self.audit.record(
            binding,
            "opening.state",
            opening_id=state["opening_id"],
            state=state["state"],
            reason=state.get("reason"),
            request_sent=state.get("request_sent"),
            request_accepted=state.get("request_accepted"),
            output_observed=state.get("output_observed"),
            output_evidence=state.get("output_evidence"),
            playback_observed=state.get("playback_observed"),
            continuation_reserved=state.get("continuation_reserved"),
            continuation_sent=state.get("continuation_sent"),
            continuation_accepted=state.get("continuation_accepted"),
            continuation_evidence=state.get("continuation_evidence"),
        )
        return state
