"""Bounded monotonic phase timings, independent of lifecycle decisions."""
from __future__ import annotations
import os
import time


def start_restart_timing(phase: str) -> dict:
    now = time.perf_counter_ns()
    return {"schema": 1, "actor_pid": os.getpid(), "clock": "perf_counter_ns",
            "started_monotonic_ns": now, "started_epoch_ns": time.time_ns(),
            "phase": phase, "phase_started_ns": now, "stages": []}


def advance_restart_timing(record: dict) -> dict:
    value = dict(record)
    timing = dict(value.get("lifecycle_timing") or {})
    if not timing:
        if value.get("completed_at"):
            return value  # Legacy history cannot acquire invented timings.
        timing = start_restart_timing(str(value.get("phase") or "accepted"))
    if timing.get("actor_pid") != os.getpid():
        timing["continuity"] = "unavailable_after_remote_restart"
        value["lifecycle_timing"] = timing
        return value
    now = time.perf_counter_ns()
    phase = str(value.get("phase") or "unknown")
    terminal = bool(value.get("completed_at"))
    stages = list(timing.get("stages") or [])
    if not timing.get("finished") and (phase != timing["phase"] or terminal):
        stages.append({"phase": timing["phase"],
                       "duration_ms": max(0, now - timing["phase_started_ns"]) / 1e6})
        timing["phase"] = phase
        timing["phase_started_ns"] = now
        timing["stages"] = stages[-32:]
    if not timing.get("finished"):
        timing["total_ms"] = max(0, now - timing["started_monotonic_ns"]) / 1e6
        timing["observed_epoch_ns"] = time.time_ns()
    if terminal:
        timing["finished"] = True
    value["lifecycle_timing"] = timing
    return value
# HASHI-Fingerprint: hfp_9e4ae5f17eba49aab11b394e1a1666a9
