"""Small allowlisted projections into the existing Function logging stream.

No media, conversational text, endpoints, exception messages or credentials are
accepted. Context follows an existing async task; it owns no call state.
"""

from __future__ import annotations

import logging
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar

from .contract import CallError, OPERATIONS


# Shared Functions' bridge logger owns the durable file sink. The root logger
# is filtered console output and is not a diagnostic journal.
logger = logging.getLogger("BridgeU.Bridge.frontend_call")
_context = ContextVar("call_diagnostic_context", default={})
_identifier = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
_generation = re.compile(r"gen-[A-Za-z0-9-]{1,120}\Z")
_code = re.compile(r"call_[a-z][a-z0-9_]{0,119}\Z")
_name = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}\Z")
_state = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
_model = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}\Z")
_provider = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,99}\Z")
_identifiers = {
    "generation", "requested_generation", "call_id", "turn_id", "client_id",
    "agent_id", "session_id", "run_id", "request_id", "message_id", "provider_request_id",
}
_numbers = {
    "context_generation", "sequence", "camera_epoch", "frame_sequence", "segment", "attempt",
    "duration_ms", "call_duration_ms", "wait_ms", "frame_age_ms", "audio_bytes",
    "image_bytes", "response_bytes", "http_status", "error_status", "lookup_count",
    "response_duration_ms", "pending_tasks", "speech_segments", "text_chars",
}
_booleans = {"camera_enabled", "pending_replaced", "cached", "terminal", "speech_truncated", "admission_in_flight"}
_choices = {
    "operation": set(OPERATIONS),
    "stage": {
        "validation", "scope_validation", "config_read", "config_context", "config_freeze",
        "start", "camera", "frame_validation", "turn_validation", "speech_validation",
        "stt", "vision", "vision_wait", "admission", "run_result", "tts", "profile",
        "auth", "owner", "content_type", "body_read", "json", "invoke", "response", "shutdown",
    },
    "media_kind": {"stt", "tts", "vision"},
    "target_location": {"cloud", "local"},
    "gateway": {"OpenRouter"},
    "reason": {
        "explicit_end", "lease_expired", "max_duration", "service_shutdown", "camera_enabled",
        "camera_disabled", "call_ended", "task_cancelled", "turn_changed", "stale_frame",
        "epoch_changed", "newer_observation", "rate_limit", "interval", "camera_unavailable",
        "non_speech_annotation", "no_speech",
    },
    "verification": {"verified", "unverified"},
}


def body_facts(body):
    if not isinstance(body, dict):
        return {}
    facts = {key: body[key] for key in (
        "operation", "call_id", "turn_id", "client_id", "agent_id", "session_id",
        "context_generation", "sequence", "frame_sequence",
    ) if key in body}
    facts["requested_generation"] = body.get("generation")
    return facts


@contextmanager
def diagnostic_context(**facts):
    token = _context.set({**_context.get(), **facts})
    try:
        yield
    finally:
        _context.reset(token)


def elapsed_ms(clock, started):
    return max(0, int((clock() - started) * 1000))


def error_facts(exc, fallback):
    return {
        "error_code": exc.code if isinstance(exc, CallError) else fallback,
        "exception_type": type(exc).__name__,
        "cause_type": type(exc.__cause__).__name__ if exc.__cause__ else None,
    }


def receipt_facts(receipt):
    if not isinstance(receipt, dict):
        return {}
    return {
        "provider_generation_id": receipt.get("generation_id"),
        "verification": receipt.get("verification"),
        "gateway": receipt.get("gateway"),
        "requested_model": receipt.get("requested_model"),
        "actual_provider": receipt.get("actual_provider"),
    }


def emit(event, **facts):
    # A full disk or broken logging handler must never become a call failure.
    # There is deliberately no recursive logging fallback.
    try:
        _emit(event, **facts)
    except Exception:
        return


def _emit(event, **facts):
    if not logger.isEnabledFor(logging.INFO):
        return
    record = {"event": event}
    for key, value in {**_context.get(), **facts}.items():
        if key in _identifiers and isinstance(value, str) and _identifier.fullmatch(value):
            record[key] = value
        elif key == "provider_generation_id" and isinstance(value, str) and _generation.fullmatch(value):
            record[key] = value
        elif key == "error_code" and isinstance(value, str) and _code.fullmatch(value):
            record[key] = value
        elif key in ("exception_type", "cause_type") and isinstance(value, str) and _name.fullmatch(value):
            record[key] = value
        elif key in ("phase", "turn_phase", "run_state") and isinstance(value, str) and _state.fullmatch(value):
            # These are PAO/service-owned tokens, not a copied state catalogue.
            record[key] = value
        elif key == "requested_model" and isinstance(value, str) and _model.fullmatch(value) and "://" not in value and "//" not in value:
            record[key] = value
        elif key == "actual_provider" and isinstance(value, str) and _provider.fullmatch(value):
            record[key] = value
        elif key in _numbers and type(value) is int and -3000 <= value <= 2**53:
            record[key] = value
        elif key in _booleans and type(value) is bool:
            record[key] = value
        elif key in _choices and isinstance(value, str) and value in _choices[key]:
            record[key] = value
    logger.info("call diagnostic %s", json.dumps(record, ensure_ascii=True, sort_keys=True))
