from __future__ import annotations

import time

import pytest

from orchestrator.phone_context_handoff import _fragment_digest, _semantic_source_matches


@pytest.mark.real_wall_clock
@pytest.mark.parametrize("size", [400, 800, 1200])
@pytest.mark.parametrize("missing", [False, True], ids=["valid", "missing"])
def test_old_semantic_snapshot_matching_is_bounded(size, missing):
    # The last-arriving fragment belongs in the middle of transcript order.
    # The old snapshot predates it, so matching must reconstruct a high-water
    # snapshot rather than accepting a contiguous range or the entire history.
    rows = [
        {"call_id": "benchmark-call", "call_epoch": 1,
         "provider_event_id": f"event-{index}", "sequence": index + 1,
         "role": "user" if index % 2 == 0 else "assistant", "text": "repeated"}
        for index in range(size)
    ]
    rows.insert(size // 2, rows.pop())
    source = [row for row in rows if row["sequence"] < size]
    if missing:
        source[-1] = {**source[-1], "provider_event_id": "missing-source"}
    key = "recent_exchange:phone:" + _fragment_digest(source)

    started = time.perf_counter()
    matched = _semantic_source_matches(key, {"metadata": {"sequence": 1}}, rows)
    elapsed = time.perf_counter() - started

    print(f"fragments={size} case={'missing' if missing else 'valid'} seconds={elapsed:.4f}")
    assert matched is (not missing)
    assert elapsed < 8.0
