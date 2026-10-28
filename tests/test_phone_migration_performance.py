from __future__ import annotations

import time

import pytest

from orchestrator.phone_context_handoff import (
    _fragment_digest, _ordinary_history_section, _semantic_source_matches,
)


@pytest.mark.real_wall_clock
@pytest.mark.parametrize("size", [400, 800, 1200, 1600])
@pytest.mark.parametrize("missing", [False, True], ids=["valid", "missing"])
def test_new_phone_sections_verify_against_recorded_arrival_bounds(size, missing):
    rows = [
        {"call_id": "new-call", "call_epoch": 1,
         "provider_event_id": f"event-{index}", "sequence": (index + size // 2) % size + 1,
         "role": "user" if index % 2 == 0 else "assistant", "text": "repeated"}
        for index in range(size)
    ]
    source = [row for row in rows if row["sequence"] < size]
    _, _, metadata = _ordinary_history_section(source)
    assert metadata["phone_source_last_sequence"] == size - 1
    if missing:
        lost_id = source[-1]["provider_event_id"]
        rows = [{**row, "provider_event_id": "different"} if row["provider_event_id"] == lost_id else row
                for row in rows]
    started = time.perf_counter()
    matched = _semantic_source_matches(metadata["key"], {"metadata": metadata}, rows)
    elapsed = time.perf_counter() - started
    print(f"fragments={size} case=new-{'missing' if missing else 'valid'} seconds={elapsed:.4f}")
    assert matched is (not missing)
    assert elapsed < 5.0


@pytest.mark.real_wall_clock
@pytest.mark.parametrize("size", [400, 800, 1200, 1600])
def test_out_of_order_missing_legacy_source_is_bounded(size):
    rows = [
        {"call_id": "rotated", "call_epoch": 1,
         "provider_event_id": f"event-{index}", "sequence": (index + size // 2) % size + 1,
         "role": "user" if index % 2 == 0 else "assistant", "text": "repeated"}
        for index in range(size)
    ]
    source = [dict(row) for row in rows if row["sequence"] < size]
    source[-1]["provider_event_id"] = "missing-source"
    key = "recent_exchange:phone:" + _fragment_digest(source)
    started = time.perf_counter()
    assert not _semantic_source_matches(key, {"metadata": {"sequence": 1}}, rows)
    elapsed = time.perf_counter() - started
    print(f"fragments={size} case=reordered-missing seconds={elapsed:.4f}")
    assert elapsed < 5.0


@pytest.mark.real_wall_clock
def test_out_of_order_full_snapshot_uses_fast_identity_verification():
    size = 1200
    rows = [
        {"call_id": "rotated", "call_epoch": 1,
         "provider_event_id": f"event-{index}", "sequence": (index + size // 2) % size + 1,
         "role": "user" if index % 2 == 0 else "assistant", "text": "repeated"}
        for index in range(size)
    ]
    source = [row for row in rows if row["sequence"] < size]
    key = "recent_exchange:phone:" + _fragment_digest(source)
    started = time.perf_counter()
    assert _semantic_source_matches(key, {"metadata": {"sequence": 1}}, rows)
    assert time.perf_counter() - started < 8.0


@pytest.mark.real_wall_clock
@pytest.mark.parametrize("size", [400, 800, 1200, 1600])
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
