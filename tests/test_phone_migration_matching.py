from __future__ import annotations

import random

import pytest

from tests.test_phone_context_handoff import phone as phone

from orchestrator.phone_context_handoff import (
    _fragment_digest, _history_units, _semantic_source_matches,
)


def _all_reference_candidates(rows, first):
    def intervals(units):
        for start in range(len(units)):
            for end in range(start + 1, len(units) + 1):
                yield [row for unit in units[start:end] for row in unit]

    yield rows
    yield from intervals(_history_units(rows))
    for high in sorted({row["sequence"] for row in rows if row["sequence"] >= first}):
        eligible = [row for row in rows if first <= row["sequence"] <= high]
        for candidate in intervals(_history_units(eligible)):
            sequences = {row["sequence"] for row in candidate}
            if first in sequences and high in sequences:
                yield candidate


def test_legacy_semantic_matching_keeps_all_verified_snapshot_candidates():
    for seed in range(12):
        rng = random.Random(seed)
        sequences = list(range(1, 13))
        rng.shuffle(sequences)
        rows = [
            {"call_id": f"call-{i // 4}", "call_epoch": 1,
             "provider_event_id": f"event-{i}", "sequence": sequences[i],
             "role": rng.choice(["user", "assistant"]), "text": "same fragment"}
            for i in range(12)
        ]
        candidates = list(_all_reference_candidates(rows, 1))
        for source in rng.sample(candidates, min(12, len(candidates))):
            key = "recent_exchange:phone:" + _fragment_digest(source)
            assert _semantic_source_matches(key, {"metadata": {"sequence": 1},
                                                  "text": "different old rendering"}, rows)

        # A digest with one identity absent must never authorize deleting history.
        missing = [dict(row) for row in rows]
        missing[rng.randrange(len(missing))]["provider_event_id"] = "missing-source"
        key = "recent_exchange:phone:" + _fragment_digest(missing)
        assert not _semantic_source_matches(key, {"metadata": {"sequence": 1}}, rows)

def test_exhausted_legacy_search_is_not_missing_source():
    from orchestrator.phone_context_handoff import _validate_semantic_sources

    size = 400
    rows = [
        {"call_id": "rotated-valid", "call_epoch": 1,
         "provider_event_id": f"event-{index}",
         "sequence": (index + size // 2) % size + 1,
         "role": "user" if index % 2 == 0 else "assistant", "text": "repeated"}
        for index in range(size)
    ]
    source = [row for row in rows[2:-2] if row["sequence"] <= 350]
    key = "recent_exchange:phone:" + _fragment_digest(source)
    section = {"metadata": {"sequence": 1}, "text": "older display"}
    assert _semantic_source_matches(key, section, rows) is None
    assert _validate_semantic_sources({key: section}, [key], rows) == [key]


@pytest.mark.asyncio
async def test_deferred_legacy_context_keeps_history_and_does_not_repeat_search(phone, monkeypatch):
    from types import SimpleNamespace

    from orchestrator.frontend_live_voice.protocol import Fragment
    from orchestrator.phone_context_handoff import (
        _fragments, _semantic_transcript, commit_success, prepare_turn,
    )
    from tests.test_phone_context_handoff import turn_item, turn_runtime

    monkeypatch.setattr("orchestrator.phone_context_handoff.LEGACY_SNAPSHOT_CANDIDATE_LIMIT", 0)
    for index in [*range(6, 12), *range(6)]:
        await phone.manager.append_fragment_once(
            phone.binding,
            Fragment(f"budget-{index}", "user" if index % 2 == 0 else "assistant",
                     f"word-{index}", index * 20, index * 20 + 10),
        )
    with phone.store._lock, phone.store._connection() as connection:
        connection.execute(
            "UPDATE live_calls SET phase='ended',ended_at='2026-10-28T05:00:00Z' WHERE call_id=?",
            (phone.call_id,),
        )
        fragments = _fragments(
            connection, owner_id=phone.owner_id, agent_id=phone.agent_id,
            session_id=phone.session_id, context_generation=1,
            call_id=phone.call_id, call_epoch=1, ended_only=True,
        )
    high_water = sorted(row["sequence"] for row in fragments)[8]
    source = [row for row in fragments[2:-2] if row["sequence"] <= high_water]
    key = "recent_exchange:phone:" + _fragment_digest(source)
    old = {"key": key, "title": "RECENT COMPLETED EXCHANGE",
           "text": _semantic_transcript(source), "authority": "history", "protected": False,
           "metadata": {"sequence": min(row["sequence"] for row in source)}}
    runtime = turn_runtime(phone)
    backend = runtime.backend_manager.current_backend
    backend.current_pcm_section_keys = lambda: (key,)
    backend.current_pcm_sections = lambda _keys: {key: old}
    item = turn_item(phone, "budget-deferred")
    sections, audit = prepare_turn(runtime, item, incremental=True)
    assert audit["retained_section_keys"] == [key]
    assert key not in audit["removed_section_keys"]
    assert "word-3" in "\n".join(section[1] for section in sections)
    assert backend.current_pcm_sections((key,))[key] == old

    commit_success(item, SimpleNamespace(is_success=True))
    with phone.store._lock, phone.store._connection() as connection:
        consumed = connection.execute(
            "SELECT provider_event_id FROM phone_context_consumption WHERE call_id=?",
            (phone.call_id,),
        ).fetchall()
    assert {row[0] for row in consumed} == {row["provider_event_id"] for row in fragments}

    def unexpected_search(*_args, **_kwargs):
        pytest.fail("Deferred legacy history must not be searched after a successful checkpoint")

    monkeypatch.setattr("orchestrator.phone_context_handoff._semantic_source_matches", unexpected_search)
    later, _ = prepare_turn(runtime, turn_item(phone, "budget-after-success"), incremental=True)
    assert later == []
