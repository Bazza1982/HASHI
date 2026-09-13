"""Worker-owned Safe Voice confirmation for Workbench voice uploads.

The basic Workbench voice path is deliberately transcript-first.  A Worker
holds a short-lived transcript until an authenticated Workbench decision
arrives over the existing runtime.slash RPC.  No audio bytes are retained and
no request is admitted before an explicit confirmation.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
from datetime import datetime, timezone
import json
import logging
import re
import time
from typing import Any, Mapping
from uuid import uuid4

from orchestrator import runtime_session


logger = logging.getLogger("HASHI.VoiceConfirmation")
TRANSPORT_PREFIX = "__hashi_voice_confirmation_v1__:"
MAX_TRANSPORT_LENGTH = 1024
PENDING_TTL_SECONDS = 10 * 60
MAX_PENDING_CONFIRMATIONS = 64
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}")
_FIELDS = {
    "read": frozenset({"version", "op", "idempotency_key"}),
    "decide": frozenset({"version", "op", "pending_id", "decision"}),
}


def _error(code: str, http_status: int) -> dict[str, Any]:
    return {
        "ok": False,
        "voice_confirmation_version": 1,
        "error_code": code,
        "http_status": http_status,
    }


def _decode_transport(text: str) -> dict[str, Any]:
    encoded = text[len(TRANSPORT_PREFIX):]
    if len(text) > MAX_TRANSPORT_LENGTH or not re.fullmatch(
        r"[A-Za-z0-9_-]+={0,2}", encoded
    ):
        raise ValueError("invalid transport encoding")
    try:
        raw = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
        value = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("invalid transport encoding") from None
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or value["version"] != 1
    ):
        raise ValueError("invalid transport version")
    op = value.get("op")
    if not isinstance(op, str) or op not in _FIELDS or set(value) != _FIELDS[op]:
        raise ValueError("invalid transport operation")
    if op == "read":
        if not _ID.fullmatch(str(value.get("idempotency_key") or "")):
            raise ValueError("invalid idempotency key")
    else:
        if not _ID.fullmatch(str(value.get("pending_id") or "")):
            raise ValueError("invalid pending id")
        if value.get("decision") not in {"confirm", "discard"}:
            raise ValueError("invalid decision")
    return value


def _clock(runtime: Any) -> float:
    provider = getattr(runtime, "_voice_confirmation_clock", None)
    return float(provider()) if callable(provider) else time.time()


def _registry(runtime: Any) -> dict[str, dict[str, Any]]:
    registry = getattr(runtime, "_workbench_voice_confirmations", None)
    if not isinstance(registry, dict):
        registry = {}
        runtime._workbench_voice_confirmations = registry
    return registry


def _expire(runtime: Any) -> None:
    now = _clock(runtime)
    registry = _registry(runtime)
    for entry in registry.values():
        if entry.get("state") == "pending" and now >= float(entry["expires_at"]):
            entry["state"] = "expired"
            entry["terminal_at"] = now
            entry.pop("prompt", None)
            entry.pop("transcript", None)
            entry.pop("request_metadata", None)
    removable = sorted(
        (
            (float(entry.get("terminal_at") or entry.get("expires_at") or 0), pending_id)
            for pending_id, entry in registry.items()
            if entry.get("state") != "pending"
            and now - float(entry.get("terminal_at") or entry.get("expires_at") or 0)
            >= PENDING_TTL_SECONDS
        )
    )
    for _when, pending_id in removable:
        registry.pop(pending_id, None)


def register_pending_voice_confirmation(
    runtime: Any,
    *,
    transcript: str,
    prompt: str,
    audio_digest: str,
    source: str,
    summary: str,
    deliver_to_telegram: bool,
    request_metadata: Mapping[str, Any],
    idempotency_key: str,
    session: Mapping[str, Any],
) -> dict[str, Any]:
    """Register a fail-closed pending transcript in the selected Worker."""
    if not _ID.fullmatch(str(idempotency_key or "")):
        raise ValueError("voice confirmation requires a valid idempotency key")
    metadata = dict(request_metadata)
    expected_owner = runtime_session.owner_id(runtime)
    if (
        str(metadata.get("owner_id") or "") != expected_owner
        or str(metadata.get("session_surface") or "").strip().casefold()
        != "workbench"
        or str(metadata.get("session_channel_key") or "").strip() != "default"
    ):
        raise ValueError("voice confirmation requires the canonical Workbench route")
    _expire(runtime)
    registry = _registry(runtime)
    for entry in registry.values():
        if entry.get("idempotency_key") != idempotency_key:
            continue
        if (
            entry.get("audio_digest") != audio_digest
            or entry.get("session_id") != session.get("session_id")
            or int(entry.get("context_generation", -1))
            != int(session.get("context_generation", -2))
        ):
            raise ValueError("voice confirmation idempotency conflict")
        return entry
    if len(registry) >= MAX_PENDING_CONFIRMATIONS:
        oldest = min(registry.values(), key=lambda item: float(item.get("created_at") or 0))
        registry.pop(str(oldest["pending_id"]), None)
    now = _clock(runtime)
    pending_id = "voice-" + uuid4().hex
    entry = {
        "pending_id": pending_id,
        "state": "pending",
        "created_at": now,
        "expires_at": now + PENDING_TTL_SECONDS,
        "idempotency_key": idempotency_key,
        "audio_digest": audio_digest,
        "transcript": transcript,
        "prompt": prompt,
        "source": source,
        "summary": summary,
        "deliver_to_telegram": bool(deliver_to_telegram),
        "request_metadata": metadata,
        "owner_id": expected_owner,
        "session_id": str(session["session_id"]),
        "context_generation": int(session["context_generation"]),
        "lock": asyncio.Lock(),
    }
    registry[pending_id] = entry
    return entry


def discard_pending_voice_confirmations(runtime: Any) -> None:
    """Make every unconfirmed Workbench transcript terminal and inert."""
    now = _clock(runtime)
    for entry in _registry(runtime).values():
        if entry.get("state") != "pending":
            continue
        entry["state"] = "discarded"
        entry["terminal_at"] = now
        entry.pop("prompt", None)
        entry.pop("transcript", None)
        entry.pop("request_metadata", None)


def _confirmation(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "pending_id": entry["pending_id"],
        "state": "pending_confirmation",
        "transcript": entry["transcript"],
        "session_id": entry["session_id"],
        "context_generation": entry["context_generation"],
        "expires_at": datetime.fromtimestamp(
            float(entry["expires_at"]), tz=timezone.utc
        ).isoformat().replace("+00:00", "Z"),
    }


def _current_binding(runtime: Any) -> tuple[dict[str, Any], str] | None:
    try:
        session = runtime_session.current_session(
            runtime, surface="workbench", channel_key="default"
        )
        return session, runtime_session.owner_id(runtime)
    except Exception:
        return None


def _binding_matches(
    entry: Mapping[str, Any], session: Mapping[str, Any], owner_id: str
) -> bool:
    return (
        str(entry.get("owner_id") or "") == owner_id
        and str(entry.get("session_id") or "") == str(session.get("session_id") or "")
        and int(entry.get("context_generation", -1))
        == int(session.get("context_generation", -2))
    )


async def _decide(
    runtime: Any,
    entry: dict[str, Any],
    *,
    decision: str,
    session: Mapping[str, Any],
    owner_id: str,
) -> dict[str, Any]:
    async with entry["lock"]:
        _expire(runtime)
        state = str(entry.get("state") or "")
        if state == "confirmed":
            if decision == "confirm":
                return {
                    "ok": True,
                    "voice_confirmation_version": 1,
                    "state": "confirmed",
                    "request_id": entry["request_id"],
                    "session_id": entry["session_id"],
                    "context_generation": entry["context_generation"],
                }
            return _error("voice_confirmation_already_confirmed", 409)
        if state == "discarded":
            if decision == "discard":
                return {
                    "ok": True,
                    "voice_confirmation_version": 1,
                    "state": "discarded",
                    "session_id": entry["session_id"],
                    "context_generation": entry["context_generation"],
                }
            return _error("voice_confirmation_discarded", 409)
        if state == "expired":
            return _error("voice_confirmation_expired", 410)
        if not _binding_matches(entry, session, owner_id):
            entry["state"] = "discarded"
            entry["terminal_at"] = _clock(runtime)
            entry.pop("prompt", None)
            entry.pop("transcript", None)
            entry.pop("request_metadata", None)
            return _error("voice_session_changed", 409)
        if decision == "discard":
            entry["state"] = "discarded"
            entry["terminal_at"] = _clock(runtime)
            entry.pop("prompt", None)
            entry.pop("transcript", None)
            entry.pop("request_metadata", None)
            return {
                "ok": True,
                "voice_confirmation_version": 1,
                "state": "discarded",
                "session_id": entry["session_id"],
                "context_generation": entry["context_generation"],
            }
        try:
            request_id = await runtime.enqueue_request(
                runtime._primary_chat_id(),
                entry["prompt"],
                entry["source"],
                entry["summary"],
                deliver_to_telegram=entry["deliver_to_telegram"],
                request_metadata=dict(entry["request_metadata"]),
                idempotency_key=entry["idempotency_key"],
            )
        except Exception:
            logger.warning("Safe Voice confirmation admission failed")
            result = _error("voice_confirmation_outcome_unknown", 503)
            result["accepted"] = None
            return result
        if not isinstance(request_id, str) or not request_id.strip():
            return _error("voice_confirmation_admission_unavailable", 503)
        entry["state"] = "confirmed"
        entry["terminal_at"] = _clock(runtime)
        entry["request_id"] = request_id
        entry.pop("prompt", None)
        entry.pop("transcript", None)
        entry.pop("request_metadata", None)
        return {
            "ok": True,
            "voice_confirmation_version": 1,
            "state": "confirmed",
            "request_id": request_id,
            "session_id": entry["session_id"],
            "context_generation": entry["context_generation"],
        }


async def try_dispatch_voice_confirmation_transport(
    runtime: Any,
    text: str,
    *,
    source_channel: str,
) -> dict[str, Any] | None:
    """Return None for ordinary text; reserved requests always terminate here."""
    if not isinstance(text, str) or not text.startswith(TRANSPORT_PREFIX):
        return None
    if str(source_channel or "").strip().lower() != "workbench_api":
        return _error("voice_confirmation_forbidden", 403)
    try:
        payload = _decode_transport(text)
    except ValueError:
        return _error("voice_confirmation_request_invalid", 400)
    if getattr(runtime, "is_function_worker_proxy", False):
        return await runtime.execute_slash_command(text, source_channel=source_channel)
    config = getattr(runtime, "global_config", None)
    if str(getattr(config, "deployment_profile", "personal") or "personal") != "personal":
        return _error("voice_confirmation_governed_not_supported", 501)
    actor = getattr(config, "authorized_id", None)
    checker = getattr(runtime, "_is_authorized_user", None)
    if type(actor) is not int or actor <= 0 or (callable(checker) and not checker(actor)):
        return _error("voice_confirmation_forbidden", 403)
    binding = _current_binding(runtime)
    if binding is None:
        return _error("voice_confirmation_session_unavailable", 503)
    session, owner = binding
    _expire(runtime)
    registry = _registry(runtime)
    if payload["op"] == "read":
        entry = next(
            (
                candidate
                for candidate in registry.values()
                if candidate.get("idempotency_key") == payload["idempotency_key"]
            ),
            None,
        )
        if entry is None:
            return _error("voice_confirmation_not_found", 404)
        if entry.get("state") == "expired":
            return _error("voice_confirmation_expired", 410)
        if entry.get("state") != "pending":
            return _error("voice_confirmation_not_pending", 409)
        if not _binding_matches(entry, session, owner):
            return _error("voice_session_changed", 409)
        return {
            "ok": True,
            "voice_confirmation_version": 1,
            "confirmation": _confirmation(entry),
        }
    entry = registry.get(payload["pending_id"])
    if entry is None:
        result = _error("voice_confirmation_not_found", 404)
        if payload["decision"] == "confirm":
            # A Worker restart can erase the in-memory receipt after durable
            # admission. Absence therefore cannot prove that confirmation did
            # not cross the request boundary.
            result["accepted"] = None
        return result
    return await _decide(
        runtime, entry, decision=payload["decision"], session=session,
        owner_id=owner,
    )
