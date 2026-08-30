from __future__ import annotations

import json

import pytest

from orchestrator.her_v2.backend_session import (
    HER_FIXED_ENVELOPE_PREFIX,
    HerBackendSessionCoordinator,
    HerFixedProtocolError,
)


def _sections(system_text: str = "Permanent policy"):
    return [
        {
            "key": "permanent_system",
            "title": "PERMANENT SYSTEM INSTRUCTIONS",
            "text": system_text,
            "authority": "permanent_system",
            "rank": 0,
            "protected": True,
            "metadata": {},
            "order": 0,
        },
        {
            "key": "current_user_request",
            "title": "CURRENT USER REQUEST",
            "text": "transport-owned current message",
            "authority": "current_user",
            "rank": 3,
            "protected": True,
            "metadata": {},
            "order": 1,
        },
        {
            "key": "persona",
            "title": "CURRENT PRESENTATION PERSONA",
            "text": "Friendly persona",
            "authority": "persona",
            "rank": 7,
            "protected": True,
            "metadata": {},
            "order": 2,
        },
    ]


def _prepare(
    coordinator: HerBackendSessionCoordinator,
    *,
    request_id: str,
    message: str,
    sections=None,
    resources=None,
    revoked_resource_ids=(),
    conversation_id: str = "hashi-conversation-1",
):
    return coordinator.prepare_transport(
        session_id="her-session-1",
        sections=sections or _sections(),
        resources=resources or [],
        user_message=message,
        request_id=request_id,
        message_id=f"message-{request_id}",
        instance_id="HASHI3",
        agent_id="agent1",
        owner_id="owner-1",
        hashi_conversation_id=conversation_id,
        context_generation=1,
        workzone_identity="workzone-a",
        revoked_resource_ids=revoked_resource_ids,
    )


def test_fixed_session_opens_once_then_sends_only_message_and_pcm_delta(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")

    first_transport, first_audit = _prepare(
        coordinator,
        request_id="turn-1",
        message="First question",
    )
    assert first_transport.startswith(HER_FIXED_ENVELOPE_PREFIX)
    first_payload = json.loads(first_transport.split("\n", 1)[1])
    assert first_payload["operation"] == "open_session"
    assert set(first_payload["pcm_snapshot"]["sections"]) == {
        "permanent_system",
        "persona",
    }
    assert first_audit["incremental"] is False

    first = coordinator.accept(first_transport)
    assert "Permanent policy" in first.materialized_prompt
    assert "First question" in first.materialized_prompt
    coordinator.complete(first, assistant_text="First answer")

    second_transport, second_audit = _prepare(
        coordinator,
        request_id="turn-2",
        message="Second question",
    )
    second_payload = json.loads(second_transport.split("\n", 1)[1])
    assert second_payload["operation"] == "append_turn"
    assert second_payload["pcm_delta"]["operations"] == []
    assert "Permanent policy" not in second_transport
    assert "Friendly persona" not in second_transport
    assert "First question" not in second_transport
    assert "First answer" not in second_transport
    assert "Second question" in second_transport
    assert second_audit["incremental"] is True
    assert second_audit["pcm_sections_unchanged"] == 2

    second = coordinator.accept(second_transport)
    assert "Permanent policy" in second.materialized_prompt
    assert "First question" in second.materialized_prompt
    assert "First answer" in second.materialized_prompt
    assert "Second question" in second.materialized_prompt
    coordinator.complete(second, assistant_text="Second answer")


def test_fixed_session_persists_across_coordinator_replacement(tmp_path):
    root = tmp_path / "state"
    first_coordinator = HerBackendSessionCoordinator(root)
    transport, _audit = _prepare(
        first_coordinator,
        request_id="turn-1",
        message="Remember blue",
    )
    turn = first_coordinator.accept(transport)
    first_coordinator.complete(turn, assistant_text="I will remember blue")

    recovered = HerBackendSessionCoordinator(root)
    next_transport, audit = _prepare(
        recovered,
        request_id="turn-2",
        message="What colour?",
    )

    assert audit["operation"] == "append_turn"
    next_turn = recovered.accept(next_transport)
    assert "Remember blue" in next_turn.materialized_prompt
    assert "I will remember blue" in next_turn.materialized_prompt


def test_fixed_session_rejects_cross_conversation_binding(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    transport, _audit = _prepare(
        coordinator,
        request_id="turn-1",
        message="First",
    )
    turn = coordinator.accept(transport)
    coordinator.complete(turn, assistant_text="Done")

    hostile_transport, _audit = _prepare(
        coordinator,
        request_id="turn-2",
        message="Leak history",
        conversation_id="different-conversation",
    )

    with pytest.raises(HerFixedProtocolError) as caught:
        coordinator.accept(hostile_transport)
    assert caught.value.code == "session_binding_conflict"


def test_fixed_session_idempotent_retry_never_reexecutes_completed_turn(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    transport, _audit = _prepare(
        coordinator,
        request_id="turn-1",
        message="Do it once",
    )
    accepted = coordinator.accept(transport)
    coordinator.complete(accepted, assistant_text="Done once")

    replay = coordinator.accept(transport)

    assert replay.duplicate is True
    assert replay.duplicate_text == "Done once"
    assert replay.materialized_prompt == ""


def test_incremental_pcm_omission_retains_initial_only_sections(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    initial_sections = _sections() + [
        {
            "key": "recent_exchange:7",
            "title": "RECENT COMPLETED EXCHANGE",
            "text": "A pre-session exchange that HASHI sends only at open.",
            "authority": "history",
            "rank": 4,
            "protected": False,
            "metadata": {"sequence": 7},
            "order": 3,
        }
    ]
    first_transport, _audit = _prepare(
        coordinator,
        request_id="turn-1",
        message="First",
        sections=initial_sections,
    )
    first = coordinator.accept(first_transport)
    coordinator.complete(first, assistant_text="Done")

    second_transport, _audit = _prepare(
        coordinator,
        request_id="turn-2",
        message="Second",
        sections=_sections(),
    )
    assert "pre-session exchange" not in second_transport

    second = coordinator.accept(second_transport)
    assert "pre-session exchange" in second.materialized_prompt


def test_incremental_pcm_sends_non_text_typed_field_changes(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    first_transport, _audit = _prepare(
        coordinator,
        request_id="turn-1",
        message="First",
    )
    first = coordinator.accept(first_transport)
    coordinator.complete(first, assistant_text="Done")

    changed_sections = _sections()
    changed_sections[0] = {
        **changed_sections[0],
        "metadata": {"policy_revision": 2},
    }
    second_transport, second_audit = _prepare(
        coordinator,
        request_id="turn-2",
        message="Second",
        sections=changed_sections,
    )
    payload = json.loads(second_transport.split("\n", 1)[1])

    assert second_audit["pcm_sections_sent"] == 1
    assert payload["pcm_delta"]["operations"] == [
        {
            "op": "upsert",
            "key": "permanent_system",
            "section": {
                **changed_sections[0],
                "content_sha256": payload["pcm_delta"]["operations"][0]["section"][
                    "content_sha256"
                ],
            },
        }
    ]


def test_resource_delta_sends_only_additions_and_explicit_revocations(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    first_resource = {
        "attachment_id": "attachment-a",
        "filename": "alpha.png",
        "sha256": "a" * 64,
    }
    second_resource = {
        "attachment_id": "attachment-b",
        "filename": "beta.png",
        "sha256": "b" * 64,
    }
    first_transport, _audit = _prepare(
        coordinator,
        request_id="turn-1",
        message="First",
        resources=[first_resource],
    )
    first = coordinator.accept(first_transport)
    coordinator.complete(first, assistant_text="Done")

    unchanged_transport, unchanged_audit = _prepare(
        coordinator,
        request_id="turn-2",
        message="Second",
        resources=[first_resource],
    )
    unchanged_payload = json.loads(unchanged_transport.split("\n", 1)[1])
    assert unchanged_payload["resource_delta"]["attachments_added"] == []
    assert unchanged_payload["resource_delta"]["attachments_revoked"] == []
    assert "alpha.png" not in unchanged_transport
    assert unchanged_audit["resource_attachments_unchanged"] == 1
    unchanged = coordinator.accept(unchanged_transport)
    assert "alpha.png" in unchanged.materialized_prompt
    coordinator.complete(unchanged, assistant_text="Done again")

    added_transport, _audit = _prepare(
        coordinator,
        request_id="turn-3",
        message="Third",
        resources=[second_resource],
    )
    added_payload = json.loads(added_transport.split("\n", 1)[1])
    assert added_payload["resource_delta"]["attachments_added"] == [second_resource]
    assert "alpha.png" not in added_transport
    added = coordinator.accept(added_transport)
    coordinator.complete(added, assistant_text="Third done")

    revoked_transport, _audit = _prepare(
        coordinator,
        request_id="turn-4",
        message="Fourth",
        revoked_resource_ids=["attachment-a"],
    )
    revoked_payload = json.loads(revoked_transport.split("\n", 1)[1])
    assert revoked_payload["resource_delta"]["attachments_added"] == []
    assert revoked_payload["resource_delta"]["attachments_revoked"] == [
        "attachment_id:attachment-a"
    ]
    revoked = coordinator.accept(revoked_transport)
    resources = coordinator.store.session(revoked.session_id)["resources"][
        "attachments"
    ]
    assert [item["attachment_id"] for item in resources] == ["attachment-b"]


def test_append_rejects_canonical_sequence_conflict_before_model_work(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    first_transport, _audit = _prepare(
        coordinator,
        request_id="turn-1",
        message="First",
    )
    first = coordinator.accept(first_transport)
    coordinator.complete(first, assistant_text="Done")

    second_transport, _audit = _prepare(
        coordinator,
        request_id="turn-2",
        message="Second",
    )
    payload = json.loads(second_transport.split("\n", 1)[1])
    payload["expected_canonical_sequence"] -= 1

    with pytest.raises(HerFixedProtocolError) as caught:
        coordinator.accept(coordinator.encode(payload))
    assert caught.value.code == "sequence_gap"


def test_cancelled_turn_retains_session_for_next_incremental_turn(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    first_transport, _audit = _prepare(
        coordinator,
        request_id="turn-1",
        message="Start work",
    )
    first = coordinator.accept(first_transport)
    coordinator.cancel(first, reason="User stopped this turn")

    cancelled = coordinator.store.turn_by_idempotency(
        first.session_id, "her-session-1:message-turn-1"
    )
    assert cancelled["status"] == "cancelled"
    assert coordinator.store.session(first.session_id)["status"] == "open"

    second_transport, audit = _prepare(
        coordinator,
        request_id="turn-2",
        message="Start a different turn",
    )
    assert audit["operation"] == "append_turn"
    second = coordinator.accept(second_transport)
    assert second.turn_id == "turn-2"
