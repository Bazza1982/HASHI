"""FC-owned, bounded Telegram catch-up; retries never renew an Event's age."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

RESULT_TTL_SECONDS = 2 * 60 * 60
NOTICE_TTL_SECONDS = 10 * 60


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class ReplayDecision:
    state: str
    reason: str
    original_at: datetime | None

    def label(self, *, locale: str) -> str:
        from orchestrator.ui_language import tr

        assert self.state == "send" and self.original_at is not None
        return tr(
            "delivery.replay_label",
            locale=locale,
            timestamp=self.original_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
        )


def telegram_replay_decision(
    store: Any,
    claim: Mapping[str, Any],
    event: Mapping[str, Any],
    *,
    owner_id: str,
    agent_id: str,
    chat_id: int,
    now: datetime | None = None,
) -> ReplayDecision:
    """A conservative whitelist over canonical FC facts, not rendered prose."""
    detail = claim.get("detail") or {}
    original = _timestamp(event.get("created_at"))
    context: Mapping[str, Any] = {}
    if event.get("message_id"):
        message = store.get_message(
            str(event["message_id"]),
            session_id=str(claim["session_id"]),
            owner_id=owner_id,
        )
        context = message.get("message_context") or {}
    policy = context.get("delivery_recovery") or {}
    if policy.get("original_at") is not None:
        original_notice = _timestamp(policy["original_at"])
        original = (
            min(original, original_notice) if original and original_notice else None
        )
    kind = str(claim.get("kind") or "")
    category = str(policy.get("category") or "")

    if (
        detail.get("superseded_by")
        or event.get("superseded_by")
        or policy.get("superseded_by")
    ):
        return ReplayDecision("suppressed", "superseded", original)
    error = detail.get("error_context") or {}
    normal_reasons = {
        "user_steer",
        "user_stop",
        "user_cancel",
        "cancelled_by_user",
        "safe_voice_discarded",
    }
    reason = str(detail.get("reason") or error.get("error_code") or "").casefold()
    # Older failed Events predate typed termination reasons. Recognize only
    # their exact known legacy interruption summary, never arbitrary text.
    summary = str(detail.get("error") or claim.get("summary") or "").strip().casefold()
    if (
        kind in {"run.cancelled", "run.stopped", "run.superseded"}
        or reason in normal_reasons
        or summary in {"interrupted by " + item for item in normal_reasons}
    ):
        return ReplayDecision("suppressed", "normal_interruption", original)

    deadline = None
    if kind == "run.question.created":
        from orchestrator.run_questions import QuestionError, RunQuestions

        try:
            question = RunQuestions(store).telegram_question(
                question_id=str(detail.get("question_id") or ""),
                owner_id=owner_id,
                agent_id=agent_id,
                chat_id=str(chat_id),
            )
        except QuestionError:
            return ReplayDecision("suppressed", "question_closed", original)
        if question["state"] == "expired":
            return ReplayDecision(
                "expired", "expired", _timestamp(question["created_at"])
            )
        if question["state"] != "pending":
            return ReplayDecision("suppressed", "question_closed", original)
        original = _timestamp(question["created_at"])
        deadline = _timestamp(question["expires_at"])
        category = "question"
    elif kind in {"run.completed", "assistant.output.available"}:
        category = "result" if event.get("message_id") else ""
    elif (
        kind in {"run.failed", "run.interrupted"}
        or event.get("semantic_kind") == "error"
    ):
        if (
            claim.get("run_id")
            and store.get_run(str(claim["run_id"]))["state"] == "completed"
        ):
            return ReplayDecision("suppressed", "superseded", original)
        category = "failure"
    elif kind in {"frontend.command_result", "command.completed"}:
        category = "command_result"
    elif not category:
        source = str(detail.get("source") or "")
        if (
            str(event.get("semantic_kind")) == "final"
            or source.endswith((".explicit-text", ".explicit-media"))
            or any(
                b.get("type") == "media_ref" for b in event.get("content_blocks") or ()
            )
        ):
            category = "result"

    ttl = {
        "result": RESULT_TTL_SECONDS,
        "failure": NOTICE_TTL_SECONDS,
        "command_result": NOTICE_TTL_SECONDS,
        "question": NOTICE_TTL_SECONDS,
    }.get(category)
    if ttl is None:
        return ReplayDecision("suppressed", "not_replayable", original)
    if original is None:
        return ReplayDecision("suppressed", "invalid_timestamp", None)
    deadline = (
        min(original + timedelta(seconds=ttl), deadline)
        if deadline
        else original + timedelta(seconds=ttl)
    )
    observed = now or utc_now()
    if observed < original or observed >= deadline:
        return ReplayDecision("expired", "expired", original)
    return ReplayDecision("send", "eligible", original)


def finish_without_telegram(
    store: Any,
    claim: Mapping[str, Any],
    *,
    owner_id: str,
    decision: ReplayDecision,
) -> None:
    """Terminalize only this endpoint; never delete history or invent delivery."""
    assert decision.state in {"expired", "suppressed"}
    store.complete_delivery_outbox(
        outbox_id=str(claim["outbox_id"]),
        lease_token=str(claim["lease_token"]),
        status=decision.state,
        error_code="telegram_replay_" + decision.reason,
    )
    store.record_frontend_delivery_receipt(
        session_id=str(claim["session_id"]),
        owner_id=owner_id,
        receipt={
            "type": "hashi.delivery-receipt",
            "version": 1,
            "event_id": str(claim["event_id"]),
            "endpoint_id": str(claim["endpoint_id"]),
            "status": "skipped",
            "proof": None,
        },
    )


# HASHI-Fingerprint: hfp_04f23bcec4e247978a2b3d8ef80688c6
