from __future__ import annotations

import random

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
