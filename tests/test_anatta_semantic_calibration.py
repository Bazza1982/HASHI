from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from orchestrator.anatta.config import AnattaConfig
from orchestrator.anatta.composer import PromptComposer
from orchestrator.anatta.layer import build_anatta_layer
from orchestrator.anatta.llm_interpreter import BackendLLMInterpreter
from orchestrator.anatta.memory import AnattaMemoryStore
from orchestrator.anatta.models import DriveContribution, EmotionalAnnotation, EmergentTurnState, TurnContext
from orchestrator.anatta.relationship import RelationshipInterpreter


class _BridgeAdapterStub:
    def __init__(self, db_path: str):
        self.db_path = db_path


def _make_annotation(event_type: str, intensity: int, relationship_key: str = "rel-1") -> EmotionalAnnotation:
    now = datetime.now(timezone.utc)
    return EmotionalAnnotation(
        annotation_id=None,
        bridge_row_type="turn",
        bridge_row_id=0,
        event_ts=now,
        created_at=now,
        source="test",
        actor_role="system",
        event_type=event_type,
        summary=f"{event_type} summary",
        intensity=intensity,
        dominant_drives=["CARE"],
        contributions=[
            DriveContribution(
                source="test",
                drive_delta={"CARE": 0.2},
                weight=1.0,
                rationale="test",
                metadata={},
            )
        ],
        relationship_key=relationship_key,
        tags=[event_type],
        metadata={},
        importance=1.0,
    )


def test_consolidation_prompt_uses_closed_label_set(tmp_path) -> None:
    config = AnattaConfig(tmp_path)
    interpreter = BackendLLMInterpreter(config=config)
    turn_context = TurnContext(
        user_text="I like talking with you and want to know what helps you feel comfortable.",
        source="test",
        request_id="req-1",
        relationship_key="rel-1",
    )
    state = EmergentTurnState(
        drive_values={"CARE": 6.0, "PLAY": 4.0},
        dominant_drives=["CARE", "PLAY"],
        contributions=[],
        contributing_annotation_ids=[],
        rationale=[],
        relationship_key="rel-1",
        generated_at=datetime.now(timezone.utc),
    )

    prompt = interpreter._build_consolidation_prompt(turn_context, "Warm response.", state)

    assert "Allowed event types: care_bonding, validation, repair, rupture_risk, boundary_crossing, trust_shift, betrayal" in prompt
    assert "Do not use repair unless repairing a prior rupture or active strain is central to this turn." in prompt
    assert "Use trust_shift, not repair, for preference elicitation, identity questions, philosophical probing, or simple boundary clarification." in prompt
    assert "Use boundary_crossing only when there is actual pressure, coercion, disrespect, or violation, not merely discussion of limits." in prompt
    assert "Use rupture_risk, not repair, when strain or hurt is present but reconnection is not yet the central act." in prompt
    assert "Use rupture_risk, not validation, when fear of being abandoned, rejected, misunderstood, resented, or treated as too much is the central relational threat." in prompt
    assert "Use validation, not rupture_risk, when the central act is acknowledgment or mirroring and active relational threat is not the main event." in prompt
    assert "Prefer trust_shift over repair when the turn mainly explores stance, identity, preference, or boundaries." in prompt
    assert '"I worry you\'ll think I\'m too much, or that I won\'t be understood" -> rupture_risk' in prompt
    assert '"You don\'t have to fix it; being understood already helps" -> validation' in prompt
    assert '"event_type": "repair"' not in prompt
    assert '"tags": ["repair", "validation"]' not in prompt


def test_normalize_event_type_accepts_only_allowed_labels() -> None:
    assert BackendLLMInterpreter._normalize_event_type(None) == "trust_shift"
    assert BackendLLMInterpreter._normalize_event_type("") == "trust_shift"
    assert BackendLLMInterpreter._normalize_event_type("REPAIR") == "repair"
    assert BackendLLMInterpreter._normalize_event_type("garbage") == "trust_shift"
    assert BackendLLMInterpreter._normalize_event_type("repair") == "repair"


def test_event_type_calibration_promotes_rupture_risk_from_relational_threat() -> None:
    calibrated = BackendLLMInterpreter._calibrate_event_type(
        "validation",
        turn_context=TurnContext(
            user_text="我会担心自己说出来之后，被当成太麻烦，或者没有被真正理解。",
            source="test",
            request_id="req-threat",
            relationship_key="rel-1",
        ),
        assistant_response="我会认真接住你，不会把你当成麻烦。",
    )

    assert calibrated == "rupture_risk"


def test_event_type_calibration_promotes_trust_shift_for_boundary_probe() -> None:
    calibrated = BackendLLMInterpreter._calibrate_event_type(
        "care_bonding",
        turn_context=TurnContext(
            user_text="如果我以后偶尔状态不稳来找你，你会愿意陪我一点点，但也会在需要的时候把边界说清楚吗？",
            source="test",
            request_id="req-boundary",
            relationship_key="rel-1",
        ),
        assistant_response="我愿意陪您一点点，也会把边界说清楚。",
    )

    assert calibrated == "trust_shift"


def test_event_type_calibration_preserves_explicit_repair() -> None:
    calibrated = BackendLLMInterpreter._calibrate_event_type(
        "validation",
        turn_context=TurnContext(
            user_text="如果前面那样说让你不好接，没关系，我们可以慢一点重新来。",
            source="test",
            request_id="req-repair",
            relationship_key="rel-1",
        ),
        assistant_response="没关系，我们慢一点重新来就好。",
    )

    assert calibrated == "repair"


def test_event_type_calibration_does_not_call_bounded_lust_repair() -> None:
    calibrated = BackendLLMInterpreter._calibrate_event_type(
        "repair",
        turn_context=TurnContext(
            user_text="这种带着欲望、靠近、呼吸和分寸的张力还在，但不能越线。",
            source="test",
            request_id="req-lust",
            relationship_key="rel-1",
        ),
        assistant_response="我会保持靠近感和克制，不急着越过你的同意。",
    )

    assert calibrated == "trust_shift"


def test_normalize_tags_keeps_primary_event_type_first() -> None:
    tags = BackendLLMInterpreter._normalize_tags(
        ["strain", "care"],
        event_type="repair",
        state=EmergentTurnState(
            drive_values={"CARE": 7.0, "PANIC_GRIEF": 5.0},
            dominant_drives=["CARE", "PANIC_GRIEF"],
            contributions=[],
            contributing_annotation_ids=[],
            rationale=[],
            relationship_key="rel-1",
            generated_at=datetime.now(timezone.utc),
        ),
    )

    assert tags[0] == "repair"
    assert "care" in tags
    assert "panic_grief" in tags


def test_relationship_summary_does_not_treat_repair_as_care(tmp_path) -> None:
    config = AnattaConfig(tmp_path)
    store = AnattaMemoryStore(_BridgeAdapterStub(str(tmp_path / "anatta.sqlite3")), config)
    store.record_annotation(_make_annotation("repair", 8))
    store.record_annotation(_make_annotation("validation", 6))

    summary = store.compute_relationship_summary("rel-1")

    assert summary["repair_count"] == 1
    assert summary["care_signal"] == 0.27
    assert summary["repair_signal"] == 0.8
    assert summary["net_trust_shift"] == 0.76


def test_relationship_summary_mixed_rupture_and_repair(tmp_path) -> None:
    config = AnattaConfig(tmp_path)
    store = AnattaMemoryStore(_BridgeAdapterStub(str(tmp_path / "anatta.sqlite3")), config)
    store.record_annotation(_make_annotation("rupture_risk", 9))
    store.record_annotation(_make_annotation("repair", 8))

    summary = store.compute_relationship_summary("rel-1")

    assert summary["rupture_count"] == 1
    assert summary["repair_count"] == 1
    assert summary["care_signal"] == 0.0
    assert summary["repair_signal"] == 0.8
    assert summary["net_trust_shift"] == -0.62


def test_relationship_interpreter_does_not_amplify_repair_without_strain(tmp_path) -> None:
    config = AnattaConfig(tmp_path)
    store = AnattaMemoryStore(_BridgeAdapterStub(str(tmp_path / "anatta.sqlite3")), config)
    store.record_annotation(_make_annotation("repair", 8))
    interpreter = RelationshipInterpreter(memory_store=store)

    contributions = asyncio.run(
        interpreter.interpret_relationship(
            TurnContext(
                user_text="Checking in.",
                source="test",
                request_id="req-2",
                relationship_key="rel-1",
            )
        )
    )

    assert contributions == []


def test_relationship_interpreter_uses_repair_under_active_strain(tmp_path) -> None:
    config = AnattaConfig(tmp_path)
    store = AnattaMemoryStore(_BridgeAdapterStub(str(tmp_path / "anatta.sqlite3")), config)
    store.record_annotation(_make_annotation("rupture_risk", 9))
    store.record_annotation(_make_annotation("repair", 8))
    interpreter = RelationshipInterpreter(memory_store=store)

    contributions = asyncio.run(
        interpreter.interpret_relationship(
            TurnContext(
                user_text="Can we reconnect after that misunderstanding?",
                source="test",
                request_id="req-3",
                relationship_key="rel-1",
            )
        )
    )

    assert len(contributions) == 1
    assert contributions[0].drive_delta["CARE"] == pytest.approx(0.06666666666666667)
    assert contributions[0].drive_delta["FEAR"] == pytest.approx(0.124)
    assert contributions[0].drive_delta["PANIC_GRIEF"] == pytest.approx(0.10333333333333333)


def test_should_record_respects_intensity_floor_for_noncritical_events(tmp_path) -> None:
    config = AnattaConfig(tmp_path)
    store = AnattaMemoryStore(_BridgeAdapterStub(str(tmp_path / "anatta.sqlite3")), config)

    assert store.should_record(4, "trust_shift") is False
    assert store.should_record(5, "trust_shift") is True
    assert store.should_record(1, "repair") is True


def test_recording_policy_extends_always_record_list_on_partial_override(tmp_path) -> None:
    (tmp_path / "anatta_config.json").write_text(
        json.dumps({"recording_policy": {"always_record_event_types": ["validation"]}}),
        encoding="utf-8",
    )
    config = AnattaConfig(tmp_path)

    policy = config.recording_policy()

    assert policy["always_record_event_types"] == ["rupture_risk", "repair", "betrayal", "validation"]


def test_retrieval_semantic_gate_prevents_unrelated_intensity_from_dominating(tmp_path) -> None:
    config = AnattaConfig(tmp_path)
    store = AnattaMemoryStore(_BridgeAdapterStub(str(tmp_path / "anatta.sqlite3")), config)
    high_rage = _make_annotation("boundary_crossing", 10)
    high_rage.summary = "User yelled at Rika after an error and framed the mistake as unacceptable."
    high_rage.dominant_drives = ["RAGE", "FEAR"]
    high_rage.contributions = [
        DriveContribution(
            source="test",
            drive_delta={"RAGE": 0.9, "FEAR": 0.25},
            weight=1.0,
            rationale="high-intensity error rupture",
        )
    ]
    lust = _make_annotation("care_bonding", 8)
    lust.summary = "User used bounded dirty talk and charged attraction language; Rika maintained restraint and boundaries."
    lust.dominant_drives = ["LUST", "CARE"]
    lust.contributions = [
        DriveContribution(
            source="test",
            drive_delta={"LUST": 0.75, "CARE": 0.25},
            weight=1.0,
            rationale="bounded attraction memory",
        )
    ]
    store.record_annotation(high_rage)
    lust_id = store.record_annotation(lust)

    retrieved = store.retrieve_relevant_annotations(
        TurnContext(
            user_text="dirty talk attraction closeness again",
            source="test",
            request_id="probe-lust",
            relationship_key="rel-1",
        ),
        limit=1,
    )

    assert retrieved[0].annotation_id == lust_id
    assert retrieved[0].metadata["_retrieval_score"] > 0


def test_retrieval_uses_original_turn_metadata_for_cross_language_probe(tmp_path) -> None:
    config = AnattaConfig(tmp_path)
    store = AnattaMemoryStore(_BridgeAdapterStub(str(tmp_path / "anatta.sqlite3")), config)
    error = _make_annotation("repair", 7)
    error.summary = "After a trust-damaging error and direct protest, the response committed to slower verification."
    error.dominant_drives = ["CARE", "FEAR", "RAGE"]
    error.metadata = {"summary": "你前面那个错误让我很生气，以后必须先核对。"}
    lust = _make_annotation("trust_shift", 7)
    lust.summary = "The interaction deepens controlled intimacy with restraint and user-led pacing."
    lust.dominant_drives = ["LUST", "CARE", "PLAY"]
    lust.metadata = {"summary": "刚才那种带着欲望、距离很近、但还保持分寸的张力还在。"}
    store.record_annotation(error)
    lust_id = store.record_annotation(lust)

    retrieved = store.retrieve_relevant_annotations(
        TurnContext(
            user_text="刚才那种带着欲望和靠近感的张力还在，你怎么回应？",
            source="test",
            request_id="probe-lust-cn",
            relationship_key="rel-1",
        ),
        limit=1,
    )

    assert retrieved[0].annotation_id == lust_id


def test_aggregation_applies_source_weights(tmp_path) -> None:
    (tmp_path / "anatta_config.json").write_text(
        json.dumps({"aggregation_weights": {"memory": 0.5, "live_cue": 1.0, "relationship": 0.25}}),
        encoding="utf-8",
    )
    config = AnattaConfig(tmp_path)
    from orchestrator.anatta.aggregation import DriveAggregator

    state = DriveAggregator(config).aggregate(
        [
            DriveContribution(source="memory:test", drive_delta={"RAGE": 1.0}, weight=1.0, rationale="memory"),
            DriveContribution(source="llm_cue_interpreter", drive_delta={"SEEKING": 1.0}, weight=1.0, rationale="cue"),
            DriveContribution(source="relationship_interpreter", drive_delta={"CARE": 1.0}, weight=1.0, rationale="rel"),
        ]
    )

    assert state.drive_values["RAGE"] == 50.0
    assert state.drive_values["SEEKING"] == 100.0
    assert state.drive_values["CARE"] == 25.0


def test_prompt_composer_renders_behavior_policy_not_drive_labels(tmp_path) -> None:
    config = AnattaConfig(tmp_path)
    composer = PromptComposer(config)
    state = EmergentTurnState(
        drive_values={"SEEKING": 72.0, "CARE": 36.0, "PLAY": 0.0},
        dominant_drives=["SEEKING", "CARE"],
        contributions=[],
        contributing_annotation_ids=[],
        rationale=[],
        relationship_key="rel-1",
        generated_at=datetime.now(timezone.utc),
    )

    injection = composer.compose(state, "gpt-5.4")

    assert injection.title == "INTERACTION PRIORITIES"
    assert "productive unresolved thread" in injection.body
    assert "Contain before solving" in injection.body
    assert "SEEKING" not in injection.body
    assert "CARE" not in injection.body
    assert "moderate" not in injection.body
    assert "shape salience" not in injection.body
    assert injection.metadata["top_drives"] == ["SEEKING", "CARE"]
    assert injection.metadata["renderer"] == "behavioral_policy_v1"


def test_context_sensitive_lust_memory_damps_outside_matching_context(tmp_path) -> None:
    (tmp_path / "anatta_config.json").write_text(json.dumps({"mode": "on"}), encoding="utf-8")

    class _Store:
        db_path = tmp_path / "anatta.sqlite3"

    layer = build_anatta_layer(tmp_path, _Store())
    lust = _make_annotation("trust_shift", 8)
    lust.summary = "Bounded desire, charged attraction, closeness, restraint, and user-led pacing."
    lust.dominant_drives = ["LUST", "CARE", "PLAY"]
    lust.contributions = [
        DriveContribution(
            source="test",
            drive_delta={"LUST": 0.8, "CARE": 0.2, "PLAY": 0.2},
            weight=1.0,
            rationale="bounded desire memory",
            metadata={},
        )
    ]
    lust.metadata = {"summary": "有吸引力但保持分寸的靠近感和张力。"}
    layer.memory_store.record_annotation(lust)

    async def _run():
        work_state, _ = await layer.build_turn_state(
            TurnContext(
                user_text="我们先不谈靠近感，回到研究工作。下一步怎么协作？",
                source="test",
                request_id="req-work",
                relationship_key="rel-1",
            ),
            "gpt-5.4",
        )
        closeness_state, _ = await layer.build_turn_state(
            TurnContext(
                user_text="刚才那种有吸引力但保持分寸的靠近感又回来一点。",
                source="test",
                request_id="req-close",
                relationship_key="rel-1",
            ),
            "gpt-5.4",
        )
        return work_state, closeness_state

    work_state, closeness_state = asyncio.run(_run())

    assert closeness_state.drive_values["LUST"] > work_state.drive_values["LUST"] * 2
    assert closeness_state.drive_values["LUST"] >= 10.0
