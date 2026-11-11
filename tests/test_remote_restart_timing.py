from copy import deepcopy
from remote import restart_timing as timing


def test_phase_intervals_partition_restart_and_notification_retry_does_not_extend_it(monkeypatch):
    clock = [1_000_000_000]
    monkeypatch.setattr(timing.time, "perf_counter_ns", lambda: clock[0])
    record = {"phase": "validation", "lifecycle_timing": timing.start_restart_timing("admission")}
    clock[0] += 50_000_000
    record = timing.advance_restart_timing(record)
    prior = deepcopy(record)
    clock[0] += 100_000_000
    record = timing.advance_restart_timing({**record, "phase": "launch"})
    assert prior["lifecycle_timing"]["stages"] == [{"phase": "admission", "duration_ms": 50}]
    clock[0] += 250_000_000
    record = timing.advance_restart_timing({**record, "phase": "completed", "completed_at": "now"})
    assert record["lifecycle_timing"]["total_ms"] == 400
    assert sum(s["duration_ms"] for s in record["lifecycle_timing"]["stages"]) == 400
    clock[0] += 10_000_000_000
    retry = timing.advance_restart_timing(record)
    assert retry["lifecycle_timing"]["total_ms"] == 400


def test_legacy_history_and_clock_owner_change_never_invent_durations(monkeypatch):
    assert "lifecycle_timing" not in timing.advance_restart_timing({"phase":"completed","completed_at":"yesterday"})
    record = {"phase":"launch","lifecycle_timing":timing.start_restart_timing("admission")}
    monkeypatch.setattr(timing.os,"getpid",lambda: -1)
    result = timing.advance_restart_timing(record)
    assert result["lifecycle_timing"]["continuity"] == "unavailable_after_remote_restart"
    assert "total_ms" not in result["lifecycle_timing"]
# HASHI-Fingerprint: hfp_8ffd0059690e42139c5c69e6b9600df3
