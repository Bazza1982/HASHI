"""Strict, content-bounded Standby configuration and control contracts."""

from __future__ import annotations

import math
import re

PROFILE_VERSION = 1
MAX_BODY_BYTES = 64 * 1024
CONTROL_RETENTION_SECONDS = 7 * 86400
DEFAULTS = {
    "duration_seconds": 7200,
    "inputs": [],
    "recording": {"transcript": False, "notes": False},
    "allow_external_processing": False,
}
LIVE_OPERATIONS = frozenset({
    "start_shadow", "arm", "resume", "renew_lease", "ingest_audio", "ingest_frame",
    "interpret_profile", "finish_notes", "promote_artifact", "feedback",
})
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_REV = re.compile(r"^[a-f0-9]{64}$")


class StandbyError(RuntimeError):
    def __init__(self, code: str, status: int = 400):
        self.code, self.status = code, status
        super().__init__(code)


def identifier(value, *, maximum=128):
    if not isinstance(value, str) or len(value) > maximum or not _ID.fullmatch(value):
        raise StandbyError("standby_request_invalid")
    return value


def revision(value):
    if not isinstance(value, str) or not _REV.fullmatch(value):
        raise StandbyError("standby_request_invalid")
    return value


def integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise StandbyError("standby_request_invalid")
    return value


def text(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise StandbyError("standby_request_invalid")
    return value.strip()


def keys(value, *, required=(), optional=()):
    if (not isinstance(value, dict) or not set(required) <= value.keys()
            or value.keys() - set(required) - set(optional)):
        raise StandbyError("standby_request_invalid")
    return value


def binding(value):
    keys(value, required=("agent_id", "session_id", "context_generation"))
    return {
        "agent_id": identifier(value["agent_id"], maximum=64),
        "session_id": identifier(value["session_id"]),
        "context_generation": integer(value["context_generation"], 1, 2**31 - 1),
    }


def normalize_profile(value):
    keys(value, required=("name", "raw_goal", "confirmed_interpretation", "targets"),
         optional=tuple(DEFAULTS))
    targets = value["targets"]
    if not isinstance(targets, list) or not 1 <= len(targets) <= 8:
        raise StandbyError("standby_request_invalid")
    parsed, ids = [], set()
    for target in targets:
        keys(target, required=("id", "description", "action"))
        target_id = identifier(target["id"], maximum=64)
        if (target_id in ids or type(target["action"]) is not str
                or target["action"] not in {"none", "wake_agent", "private_note", "timer_reminder"}):
            raise StandbyError("standby_request_invalid")
        ids.add(target_id)
        parsed.append({"id": target_id, "description": text(target["description"], 1000),
                       "action": target["action"]})
    inputs = value.get("inputs", DEFAULTS["inputs"])
    if (not isinstance(inputs, list) or len(inputs) > 4
            or any(type(item) is not str for item in inputs)
            or len(set(inputs)) != len(inputs)
            or set(inputs) - {"microphone", "system_audio", "camera", "timer"}):
        raise StandbyError("standby_request_invalid")
    recording = value.get("recording", DEFAULTS["recording"])
    keys(recording, required=("transcript", "notes"))
    if any(type(item) is not bool for item in recording.values()):
        raise StandbyError("standby_request_invalid")
    external = value.get("allow_external_processing", False)
    if type(external) is not bool:
        raise StandbyError("standby_request_invalid")
    return {
        "schema_version": PROFILE_VERSION,
        "name": text(value["name"], 100),
        "raw_goal": text(value["raw_goal"], 4000),
        "confirmed_interpretation": text(value["confirmed_interpretation"], 4000),
        "targets": parsed, "inputs": inputs.copy(), "recording": dict(recording),
        "duration_seconds": integer(value.get("duration_seconds", DEFAULTS["duration_seconds"]), 60, 8 * 3600),
        "allow_external_processing": external,
    }


def operation_time(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise StandbyError("standby_request_invalid")
    return float(value)
