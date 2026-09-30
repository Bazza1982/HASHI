"""PAO's once-per-logical-call opening, persisted beside the frozen call choice.

Provider acceptance, output observation and local player progress are separate
facts. A lost receipt never authorizes another opening request.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
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
    }


def opening_goal(phone: Mapping[str, Any]) -> str:
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
        "The call is ready for two-way conversation. Open naturally now using the effective "
        "Persona, address preferences and instruction authority already supplied. "
        + chosen
        + "Briefly continue a clear recent topic, or begin answering a pending user question from "
        "the supplied facts. Otherwise offer a short natural greeting without inventing an address. "
        "Speak one or two substantive sentences, then pause and listen. Use only existing context "
        "and verified results; this opening requests no lookup or action. Present the user's subject, "
        "not internal execution. If the caller speaks first or interrupts, yield and respond to their words."
    )


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
            elif signal == "accepted" and state.get("request_sent"):
                state["request_accepted"] = True
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
        )
        return state
