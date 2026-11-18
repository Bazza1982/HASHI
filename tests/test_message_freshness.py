"""Unit tests for shared message freshness classification (PAO Functions layer)."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from orchestrator.message_freshness import (
    classify_message_freshness,
    FRESHNESS_TELEGRAM_SECONDS,
    FRESHNESS_WORKBENCH_LOCAL_SECONDS,
    FRESHNESS_WORKBENCH_REMOTE_SECONDS,
    FRESHNESS_HCHAT_SECONDS,
    CONFIRMATION_EXPIRY_SECONDS,
    FUTURE_CLOCK_SKEW_TOLERANCE_SECONDS,
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def test_constants():
    assert FRESHNESS_TELEGRAM_SECONDS == 30
    assert FRESHNESS_WORKBENCH_LOCAL_SECONDS == 30
    assert FRESHNESS_WORKBENCH_REMOTE_SECONDS == 60
    assert FRESHNESS_HCHAT_SECONDS == 60
    assert CONFIRMATION_EXPIRY_SECONDS == 15 * 60
    assert FUTURE_CLOCK_SKEW_TOLERANCE_SECONDS == 5.0


def test_telegram_freshness_boundaries():
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)

    # 1. 29.9s ago (before threshold) -> fresh
    orig_fresh = _iso(now - timedelta(seconds=29.9))
    res = classify_message_freshness("telegram", originated_at=orig_fresh, received_at=now)
    assert res.is_fresh is True
    assert res.reason == "fresh"
    assert res.threshold_seconds == 30
    assert 29.8 < res.age_seconds < 30.0

    # 2. Exactly 30.0s ago (at threshold) -> fresh
    orig_exact = _iso(now - timedelta(seconds=30.0))
    res = classify_message_freshness("telegram", originated_at=orig_exact, received_at=now)
    assert res.is_fresh is True
    assert res.reason == "fresh"
    assert abs(res.age_seconds - 30.0) < 0.001

    # 3. 30.1s ago (past threshold) -> stale
    orig_stale = _iso(now - timedelta(seconds=30.1))
    res = classify_message_freshness("telegram", originated_at=orig_stale, received_at=now)
    assert res.is_fresh is False
    assert res.reason == "stale"
    assert res.age_seconds > 30.0


def test_workbench_local_and_remote_boundaries():
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)

    # Local Workbench -> 30s
    res_local_59s = classify_message_freshness(
        "workbench",
        is_remote=False,
        originated_at=_iso(now - timedelta(seconds=59)),
        received_at=now,
    )
    assert res_local_59s.is_fresh is False
    assert res_local_59s.reason == "stale"
    assert res_local_59s.threshold_seconds == 30

    # Remote Workbench -> 60s
    res_remote_59s = classify_message_freshness(
        "workbench",
        is_remote=True,
        originated_at=_iso(now - timedelta(seconds=59)),
        received_at=now,
    )
    assert res_remote_59s.is_fresh is True
    assert res_remote_59s.reason == "fresh"
    assert res_remote_59s.threshold_seconds == 60

    res_remote_60s = classify_message_freshness(
        "workbench",
        is_remote=True,
        originated_at=_iso(now - timedelta(seconds=60)),
        received_at=now,
    )
    assert res_remote_60s.is_fresh is True

    res_remote_61s = classify_message_freshness(
        "workbench",
        is_remote=True,
        originated_at=_iso(now - timedelta(seconds=60.1)),
        received_at=now,
    )
    assert res_remote_61s.is_fresh is False
    assert res_remote_61s.reason == "stale"


def test_hchat_boundaries():
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
    # hchat has 60s threshold
    res_59 = classify_message_freshness(
        "hchat",
        originated_at=_iso(now - timedelta(seconds=59)),
        received_at=now,
    )
    assert res_59.is_fresh is True
    assert res_59.threshold_seconds == 60

    res_61 = classify_message_freshness(
        "hchat",
        originated_at=_iso(now - timedelta(seconds=60.1)),
        received_at=now,
    )
    assert res_61.is_fresh is False
    assert res_61.reason == "stale"


def test_equivalent_offset_timestamps_have_identical_freshness():
    now = datetime(2026, 11, 16, 0, 0, 30, tzinfo=timezone.utc)
    equivalents = [
        "2026-11-16T00:00:00Z", "2026-11-16T00:00:00+00:00",
        "2026-11-16T11:00:00+11:00", "2026-11-15T19:00:00-05:00",
        datetime(2026, 11, 16, 11, 0, tzinfo=timezone(timedelta(hours=11))),
    ]
    for entrypoint in ("workbench", "telegram", "hchat"):
        values = [classify_message_freshness(entrypoint, originated_at=item, received_at=now)
                  for item in equivalents]
        assert {(item.reason, item.age_seconds, item.originated_at_iso) for item in values} == {
            ("fresh", 30.0, "2026-11-16T00:00:00Z")
        }
    for invalid in ("2026-11-16T00:00:00+24:00", "2026-11-16T00:00:00+05:60",
                    "2026-11-16T00:00:00", datetime(2026, 11, 16)):
        assert classify_message_freshness("workbench", originated_at=invalid, received_at=now).reason == "timestamp_invalid"


def test_abnormal_timestamps():
    now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Missing originated_at
    res_missing = classify_message_freshness("workbench", originated_at=None, received_at=now)
    assert res_missing.is_fresh is False
    assert res_missing.reason == "timestamp_missing"

    res_empty = classify_message_freshness("workbench", originated_at="", received_at=now)
    assert res_empty.is_fresh is False
    assert res_empty.reason == "timestamp_missing"

    # 2. Invalid format / not parseable
    res_inv = classify_message_freshness("workbench", originated_at="not-a-timestamp", received_at=now)
    assert res_inv.is_fresh is False
    assert res_inv.reason == "timestamp_invalid"

    # 3. Non-UTC naive timestamp
    res_naive = classify_message_freshness("workbench", originated_at="2026-10-16 12:00:00", received_at=now)
    assert res_naive.is_fresh is False
    assert res_naive.reason == "timestamp_invalid"

    # RFC 3339 requires the date/time separator, even when UTC is explicit.
    res_space = classify_message_freshness(
        "workbench", originated_at="2026-10-16 12:00:00Z", received_at=now
    )
    assert res_space.is_fresh is False
    assert res_space.reason == "timestamp_invalid"

    # 4. Clock skew in future
    # 4.9s in future -> allowed by 5s tolerance, treated as age 0.0 -> fresh
    res_fut_4s = classify_message_freshness(
        "workbench",
        originated_at=_iso(now + timedelta(seconds=4.9)),
        received_at=now,
    )
    assert res_fut_4s.is_fresh is True
    assert res_fut_4s.age_seconds == 0.0
    assert res_fut_4s.reason == "fresh"

    # 5.1s in future -> exceeds 5s tolerance -> timestamp_in_future
    res_fut_6s = classify_message_freshness(
        "workbench",
        originated_at=_iso(now + timedelta(seconds=5.1)),
        received_at=now,
    )
    assert res_fut_6s.is_fresh is False
    assert res_fut_6s.reason == "timestamp_in_future"


def test_clock_injection():
    # Verify clock can be injected without real wall-clock sleep
    current_time = 1000.0

    def fake_clock():
        return current_time

    res = classify_message_freshness(
        "telegram",
        originated_at="1970-01-01T00:16:15Z",  # 975s
        clock=fake_clock,
    )
    # age = 1000 - 975 = 25s <= 30s -> fresh
    assert res.is_fresh is True
    assert res.age_seconds == 25.0

    current_time = 1010.0
    res2 = classify_message_freshness(
        "telegram",
        originated_at="1970-01-01T00:16:15Z",  # 975s
        clock=fake_clock,
    )
    # age = 1010 - 975 = 35s > 30s -> stale
    assert res2.is_fresh is False
    assert res2.age_seconds == 35.0
