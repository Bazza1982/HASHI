"""Present completed Agent activity in its owner's current Conversation."""

from __future__ import annotations

from typing import Any, Mapping

from orchestrator.session_store import SESSION_KIND_AGENT_ACTIVITY, SessionStore


def project_completed_result(
    store: SessionStore,
    *,
    run: Mapping[str, Any],
    owner_id: str,
    agent_id: str,
) -> dict[str, Any] | None:
    """Mirror only the final answer for display, never its activity history.

    The canonical activity Message is already queued for an active phone call.
    This presentation-only copy is excluded from model history and the phone
    inbox, while remaining visible to Workbench's Conversation transcript.
    """

    final_message_id = str(run.get("final_message_id") or "").strip()
    if str(run.get("state") or "") != "completed" or not final_message_id:
        return None
    activity = store.get_session(str(run["session_id"]), owner_id=owner_id)
    if (
        activity.get("session_kind") != SESSION_KIND_AGENT_ACTIVITY
        or activity.get("agent_id") != agent_id.lower()
    ):
        return None
    final = store.get_message(
        final_message_id,
        session_id=activity["session_id"],
        owner_id=owner_id,
    )
    text = str(final.get("text") or "").strip()
    if final.get("role") != "assistant" or not text:
        return None
    primary = store.resolve_primary_session(
        owner_id=owner_id, agent_id=agent_id, establish=True,
    )
    return store.append_presentation_message(
        session_id=primary["session_id"],
        owner_id=owner_id,
        agent_id=agent_id,
        role="assistant",
        text=text,
        source="agent_activity.result",
        idempotency_key=f"activity-final:{final_message_id}",
        content_format="markdown",
        presentation_channel="final",
        history_eligible=False,
        message_context={
            "kind": "final",
            "activity_session_id": activity["session_id"],
            "activity_run_id": str(run["run_id"]),
            "activity_message_id": final_message_id,
        },
    )
