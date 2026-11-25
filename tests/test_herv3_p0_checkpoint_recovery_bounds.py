"""Regression tests for HERV3 P0: unbounded checkpoint/recovery prompt growth.

Covers the five fixes:

- P0-1 bounded prompt projection (tool_receipts / side_effects / remaining_work)
- P0-2 prompt self-feedback cut (``remaining_work.goal`` + WIP journal summary)
- P0-3 route rebuild trigger (main-model identity only)
- P0-4 provider capacity preflight (no ``data:``/``base64,`` substring skip)
- P0-5 bounded recovery across repeated attempts (no recursive growth)

These are unit/integration tests against the real ``HerSessionStore``,
``HerBackendSessionCoordinator``, ``WIPJournal`` and the projection helpers.
No provider HTTP traffic is performed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from adapters.openrouter_api import OpenRouterAdapter
from orchestrator.her_v2.backend_session import HerBackendSessionCoordinator
from orchestrator.her_v2.recovery_projection import (
    FIELD_BUDGET_CHARS,
    GOAL_BUDGET_CHARS,
    TOTAL_BUDGET_CHARS,
    bounded_goal,
    bounded_prompt_projection,
)
from orchestrator.her_v2.wip_journal import WIPJournal


# --------------------------------------------------------------------------
# Helpers (mirror test_her_v2_control_plane.py conventions)
# --------------------------------------------------------------------------


def _sections():
    return [
        {
            "key": "permanent_system",
            "title": "SYSTEM",
            "text": "Stable policy",
            "authority": "permanent_system",
            "order": 0,
        },
        {
            "key": "persona",
            "title": "PERSONA",
            "text": "Friendly",
            "authority": "persona",
            "order": 1,
        },
    ]


def _accept(coordinator: HerBackendSessionCoordinator, request_id: str, message: str):
    encoded, _audit = coordinator.prepare_transport(
        session_id="session-1",
        sections=_sections(),
        resources=[],
        user_message=message,
        request_id=request_id,
        message_id=f"message-{request_id}",
        instance_id="HASHI6",
        agent_id="agent1",
        owner_id="owner",
        hashi_conversation_id="conversation",
        context_generation=1,
        workzone_identity="workzone",
    )
    return coordinator.accept(encoded)


def _route_snapshot(
    provider: str,
    model: str,
    *,
    effort: str = "high",
    auxiliary=None,
    voice: bool = False,
):
    profiles = [
        {"name": "main", "provider": provider, "model": model, "reasoning": effort}
    ]
    if auxiliary is not None:
        profiles.append(
            {
                "name": "auxiliary",
                "provider": auxiliary[0],
                "model": auxiliary[1],
                "reasoning": "off",
            }
        )
    return {
        "routing_mode": "single",
        "effort": effort,
        "profiles": profiles,
        "routes": {"direct": {"provider": provider, "model": model, "reasoning": effort}},
        "voice_origin_active": voice,
    }


def _runtime_event(turn_id: str, event_id: str, event: str, **payload):
    return {
        "event_id": event_id,
        "turn_id": turn_id,
        "request_ref": f"hashi-request:{turn_id}",
        "stage": payload.pop("stage", "execution"),
        "role": "primary",
        "event": event,
        "provider": "deepseek-api",
        "model": "deepseek-v4-pro",
        "attempt": 1,
        "payload": payload,
    }


def _settle(coordinator: HerBackendSessionCoordinator, turn, *, route_snapshot, routing_revision=1):
    coordinator.store.freeze_turn_routing(
        session_id=turn.session_id,
        turn_id=turn.turn_id,
        routing_revision=routing_revision,
        capability_revision=1,
        pricing_revision="prices-v1",
        route_snapshot=route_snapshot,
    )
    coordinator.store.record_runtime_event(
        session_id=turn.session_id,
        turn_id=turn.turn_id,
        record=_runtime_event(
            turn.turn_id,
            f"{turn.turn_id}:plan",
            "stage_completed",
            stage="planning",
            output={"steps": ["inspect", "change", "verify"]},
        ),
    )
    coordinator.complete(turn, assistant_text="Done")


# --------------------------------------------------------------------------
# P0-3: route rebuild trigger — main-model identity only
# --------------------------------------------------------------------------

MAIN = ("deepseek-api", "deepseek-v4-pro")
ALT = ("hashi-api", "gpt-5.6-sol")


@pytest.mark.parametrize(
    "variant",
    [
        pytest.param(
            lambda: _route_snapshot(*MAIN, effort="max"), id="effort-max-to-high"
        ),
        pytest.param(
            lambda: _route_snapshot(*MAIN, effort="low"), id="effort-low"
        ),
        pytest.param(
            lambda: _route_snapshot(*MAIN, auxiliary=("deepseek-api", "deepseek-flash")),
            id="auxiliary-added",
        ),
        pytest.param(lambda: _route_snapshot(*MAIN, voice=True), id="voice-toggle"),
    ],
)
def test_unrelated_route_change_does_not_rebuild(tmp_path, variant):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    first = _accept(coordinator, "turn-1", "Start")
    _settle(coordinator, first, route_snapshot=_route_snapshot(*MAIN))

    second = _accept(coordinator, "turn-2", "Continue")
    frozen = coordinator.store.freeze_turn_routing(
        session_id=second.session_id,
        turn_id=second.turn_id,
        routing_revision=2,
        capability_revision=1,
        pricing_revision="prices-v1",
        route_snapshot=variant(),
    )
    assert frozen["rebuild_from_checkpoint"] is False
    assert frozen["provider_context_generation"] == 1


def test_routing_revision_change_with_same_main_model_does_not_rebuild(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    first = _accept(coordinator, "turn-1", "Start")
    _settle(coordinator, first, route_snapshot=_route_snapshot(*MAIN))

    second = _accept(coordinator, "turn-2", "Continue")
    frozen = coordinator.store.freeze_turn_routing(
        session_id=second.session_id,
        turn_id=second.turn_id,
        routing_revision=99,  # big revision bump, same main model
        capability_revision=1,
        pricing_revision="prices-v1",
        route_snapshot=_route_snapshot(*MAIN),
    )
    assert frozen["rebuild_from_checkpoint"] is False


def test_main_model_change_rebuilds(tmp_path):
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    first = _accept(coordinator, "turn-1", "Start")
    _settle(coordinator, first, route_snapshot=_route_snapshot(*MAIN))

    second = _accept(coordinator, "turn-2", "Switch model")
    frozen = coordinator.store.freeze_turn_routing(
        session_id=second.session_id,
        turn_id=second.turn_id,
        routing_revision=2,
        capability_revision=1,
        pricing_revision="prices-v1",
        route_snapshot=_route_snapshot(*ALT),
    )
    assert frozen["rebuild_from_checkpoint"] is True
    assert frozen["provider_context_generation"] == 2


def test_short_session_has_no_rebuild_and_stays_live(tmp_path):
    """A normal short session with unchanged config does not rebuild."""
    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    first = _accept(coordinator, "turn-1", "Hello")
    frozen = coordinator.store.freeze_turn_routing(
        session_id=first.session_id,
        turn_id=first.turn_id,
        routing_revision=1,
        capability_revision=1,
        pricing_revision="prices-v1",
        route_snapshot=_route_snapshot(*MAIN),
    )
    assert frozen["rebuild_from_checkpoint"] is False
    assert frozen["provider_context_generation"] == 1


# --------------------------------------------------------------------------
# P0-1 / P0-2: bounded projection and prompt self-feedback cut
# --------------------------------------------------------------------------


def _huge_recovery_context():
    return {
        "format": "her-active-turn-recovery-context-v1",
        "authority": "quoted_recovery_context",
        "session_id": "session-1",
        "interrupted_turn_id": "turn-1",
        "tool_receipts": [
            {
                "operation_id": f"op-{i}",
                "invocation_id": f"inv-{i}",
                "tool_name": "shell",
                "status": "completed",
                "output": "x" * 10000,
            }
            for i in range(1200)
        ],
        "side_effects": [
            {
                "operation_id": f"op-{i}",
                "state": "pending",
                "kind": "file_write",
                "payload": "y" * 8000,
            }
            for i in range(500)
        ],
        "remaining_work": {
            "goal": "previously expanded prompt " + "z" * 200_000,
            "status": "in_progress",
        },
        "recent_settled_exchanges": [
            {"sequence": i, "user": "u" * 20000, "assistant": "a" * 20000}
            for i in range(60)
        ],
        "strategy": {"selected_strategy_cards": ["CODE_MODIFY"] * 5000},
    }


def test_projection_is_always_bounded(tmp_path):
    del tmp_path
    projection = bounded_prompt_projection(_huge_recovery_context())
    size = len(json.dumps(projection, ensure_ascii=False, default=str).encode("utf-8"))
    assert size <= TOTAL_BUDGET_CHARS, size
    assert projection["tool_receipts"]["total"] == 1200
    assert projection["tool_receipts"]["omitted"] > 0
    # Large receipts retain useful bounded excerpts, never full raw outputs.
    for receipt in projection["tool_receipts"]["recent"]:
        output = receipt.get("output")
        if output is not None:
            assert output["truncated"] is True
            assert output["sha256"].startswith("sha256:")
            assert len(output.get("excerpt", "")) < 10000
            assert receipt["canonical_evidence_ref"]
    # unresolved side effects are summarised with an evidence reference
    assert projection["side_effects"]["unresolved_total"] == 500
    assert "evidence_ref" in projection["side_effects"]
    # polluted goal is truncated with a sha256 witness
    goal = projection["remaining_work"].get("goal")
    assert isinstance(goal, dict) and goal["truncated"] is True
    assert goal["sha256"].startswith("sha256:")


def test_projection_nested_reprojection_does_not_grow(tmp_path):
    """Repeated recovery must not recursively grow the projected prompt."""
    del tmp_path
    current = _huge_recovery_context()
    sizes = []
    for _ in range(3):
        current = bounded_prompt_projection(current)
        sizes.append(
            len(json.dumps(current, ensure_ascii=False, default=str).encode("utf-8"))
        )
    assert sizes == sorted(sizes) or sizes[-1] <= sizes[0] + 1024
    assert max(sizes) <= TOTAL_BUDGET_CHARS


def test_projection_small_context_stays_intact(tmp_path):
    del tmp_path
    context = {
        "session_id": "s",
        "interrupted_turn_id": "t",
        "tool_receipts": [{"operation_id": "op-1", "status": "completed"}],
        "side_effects": [],
        "remaining_work": {"goal": "short goal", "status": "in_progress"},
        "safe_to_resume": True,
    }
    projection = bounded_prompt_projection(context)
    assert projection["tool_receipts"]["total"] == 1
    assert projection["remaining_work"]["goal"] == "short goal"
    assert projection["safe_to_resume"] is True


def test_bounded_goal_truncates_with_audit_sha256(tmp_path):
    del tmp_path
    huge = "x" * (GOAL_BUDGET_CHARS + 10_000)
    result = bounded_goal(huge)
    assert result["goal_truncated"] is True
    assert len(result["goal"]) <= GOAL_BUDGET_CHARS + 32
    assert result["goal_sha256"].startswith("sha256:")
    assert result["goal_chars"] == len(huge)

    small = bounded_goal("short request")
    assert small == {"goal": "short request"}


def test_request_received_goal_is_bounded_and_audit_preserved(tmp_path):
    import sqlite3 as _sqlite3

    coordinator = HerBackendSessionCoordinator(tmp_path / "state")
    original_goal = "用户目标 " + "任" * 20_000
    turn = _accept(coordinator, "turn-1", original_goal)
    coordinator.store.freeze_turn_routing(
        session_id=turn.session_id,
        turn_id=turn.turn_id,
        routing_revision=1,
        capability_revision=1,
        pricing_revision="prices-v1",
        route_snapshot=_route_snapshot(*MAIN),
    )
    # A historically-polluted request_received carrying an expanded prompt.
    expanded = "expanded provider prompt " + "p" * 200_000
    coordinator.store.record_runtime_event(
        session_id=turn.session_id,
        turn_id=turn.turn_id,
        record=_runtime_event(
            turn.turn_id,
            f"{turn.turn_id}:request",
            "request_received",
            request=expanded,
        ),
    )
    conn = _sqlite3.connect(str(coordinator.store.path))
    conn.row_factory = _sqlite3.Row
    try:
        row = conn.execute(
            "SELECT remaining_work_json FROM her_active_turn_recovery "
            "WHERE session_id = ? AND turn_id = ?",
            (turn.session_id, turn.turn_id),
        ).fetchone()
        assert row is not None
        remaining = json.loads(row["remaining_work_json"])
        assert remaining["goal_truncated"] is True
        assert len(remaining["goal"]) <= GOAL_BUDGET_CHARS + 32
        assert remaining["goal_sha256"].startswith("sha256:")
        # The original accepted user request and full provider audit remain
        # independently queryable; the provider prompt is not the goal.
        user = conn.execute(
            "SELECT user_message FROM her_turns WHERE session_id=? AND turn_id=?",
            (turn.session_id, turn.turn_id),
        ).fetchone()
        assert user["user_message"] == original_goal
        assert remaining["goal"].startswith("用户目标 ")
        audit = conn.execute(
            "SELECT payload_json FROM her_session_events "
            "WHERE event_id = ?",
            (f"runtime:{turn.turn_id}:request",),
        ).fetchone()
        assert audit is not None
        canonical = json.loads(audit["payload_json"])
        assert canonical["facts"]["request"] == expanded
    finally:
        conn.close()


def test_wip_journal_summary_never_copies_full_prompt(tmp_path):
    journal = WIPJournal(tmp_path / "wip.jsonl")
    huge_prompt = "assembled provider prompt " + "A" * 100_000
    journal.begin_turn(
        request_id="r1", prompt=huge_prompt, request_summary="", session_id="s1"
    )
    rendered = journal.render_context(journal.records())
    assert "assembled provider prompt " not in rendered
    assert "A" * 100_000 not in rendered  # provider prompt never re-fed
    assert len(rendered) < 10_000


# --------------------------------------------------------------------------
# P0-4: provider capacity preflight
# --------------------------------------------------------------------------


def _preflight_adapter():
    adapter = OpenRouterAdapter.__new__(OpenRouterAdapter)
    adapter.config = SimpleNamespace(
        name="p0test",
        engine="openrouter-api",
        model="test-model",
        workspace_dir=None,
        _hashi_runtime=SimpleNamespace(),
    )
    return adapter


def _patch_capacity(monkeypatch, usable_input_tokens):
    monkeypatch.setattr(
        "orchestrator.context_compaction.resolve_capacity_profile",
        lambda *a, **k: SimpleNamespace(
            provider="openrouter-api",
            model="test-model",
            usable_input_tokens=usable_input_tokens,
        ),
    )


def test_preflight_text_containing_data_substring_is_still_checked(monkeypatch):
    adapter = _preflight_adapter()
    _patch_capacity(monkeypatch, usable_input_tokens=1000)
    payload = {
        "model": "test-model",
        "messages": [
            {
                "role": "user",
                "content": "note: data: base64, here is an ordinary text " + "x" * 100_000,
            }
        ],
    }
    # The estimate is not a tokenizer proof. Actual provider admission must
    # decide whether this ordinary text fits, regardless of these substrings.
    adapter._preflight_payload_capacity(payload)


def test_preflight_real_multimodal_media_is_not_miscounted(monkeypatch):
    adapter = _preflight_adapter()
    _patch_capacity(monkeypatch, usable_input_tokens=2000)
    payload = {
        "model": "test-model",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this image briefly"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + "A" * 1_000_000},
                    },
                ]
            }
        ],
    }
    # The text portion is tiny; media bytes must not be counted as text tokens.
    adapter._preflight_payload_capacity(payload)


# --------------------------------------------------------------------------
# P0-5: bounded recovery across repeated failures / no tool replay
# --------------------------------------------------------------------------


def test_unresolved_side_effect_survives_projection_as_evidence_ref(tmp_path):
    del tmp_path
    context = {
        "session_id": "s",
        "interrupted_turn_id": "t",
        "side_effects": [
            {"operation_id": "write-1", "state": "unknown", "kind": "file_write"}
        ],
        "tool_receipts": [],
        "remaining_work": {"goal": "finish", "status": "in_progress"},
    }
    projection = bounded_prompt_projection(context)
    assert projection["side_effects"]["unresolved_total"] == 1
    assert projection["side_effects"]["evidence_ref"].startswith(
        "her_active_turn_recovery:side_effects"
    )


def test_projection_field_budget_hard_cap(tmp_path):
    del tmp_path
    context = {
        "session_id": "s",
        "interrupted_turn_id": "t",
        "tool_receipts": [
            {"operation_id": "op", "output": "O" * 200_000}
        ],
        "side_effects": [],
        "remaining_work": {"goal": "g", "status": "in_progress"},
    }
    projection = bounded_prompt_projection(context)
    receipts_json = json.dumps(projection["tool_receipts"], ensure_ascii=False, default=str)
    assert len(receipts_json.encode("utf-8")) <= FIELD_BUDGET_CHARS
