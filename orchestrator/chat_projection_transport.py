"""Worker-owned, read-only chat projection over the existing runtime.slash RPC.

The authenticated Backend API transports an opaque reserved command. Only the
Agent Worker interprets this envelope; no shared service or Core restart is
needed to adopt the projection. SessionStore remains the state owner.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from typing import Any

from orchestrator import runtime_session
from orchestrator.chat_transcript_projection import build_chat_projection


logger = logging.getLogger("HASHI.ChatProjection")
TRANSPORT_PREFIX = "__hashi_chat_projection_v1__:"
MAX_TRANSPORT_LENGTH = 1024
MAX_SAFE_OFFSET = 9007199254740991
_FIELDS = {
    "recent": frozenset({"version", "op", "limit"}),
    "poll": frozenset({"version", "op", "offset"}),
}


def _error(code: str, http_status: int) -> dict[str, Any]:
    return {
        "ok": False,
        "chat_projection_version": 1,
        "error_code": "chat_projection_" + code,
        "http_status": http_status,
    }


def _decode_transport(text: str) -> dict[str, Any]:
    encoded = text[len(TRANSPORT_PREFIX):]
    if len(text) > MAX_TRANSPORT_LENGTH or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", encoded):
        raise ValueError("invalid transport encoding")
    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("invalid transport encoding") from None
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        raise ValueError("invalid transport version")
    op = value.get("op")
    if not isinstance(op, str) or op not in _FIELDS or set(value) - _FIELDS[op]:
        raise ValueError("invalid transport operation")
    if op == "recent":
        limit = value.get("limit", 200)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("invalid projection limit")
    else:
        offset = value.get("offset")
        if type(offset) is not int or not 0 <= offset <= MAX_SAFE_OFFSET:
            raise ValueError("invalid projection offset")
    return value


async def try_dispatch_chat_projection_transport(
    runtime: Any,
    text: str,
    *,
    source_channel: str,
) -> dict[str, Any] | None:
    """Return None for ordinary text; reserved requests always terminate here."""
    if not isinstance(text, str) or not text.startswith(TRANSPORT_PREFIX):
        return None
    if str(source_channel or "").strip().lower() != "workbench_api":
        return _error("forbidden", 403)
    try:
        payload = _decode_transport(text)
    except ValueError:
        return _error("request_invalid", 400)
    # Updated shared callers still forward to the selected Worker. They do not
    # derive a Session or consult their own retained generation's projection.
    if getattr(runtime, "is_function_worker_proxy", False):
        return await runtime.execute_slash_command(text, source_channel=source_channel)
    config = getattr(runtime, "global_config", None)
    if str(getattr(config, "deployment_profile", "personal") or "personal") != "personal":
        return _error("governed_not_supported", 501)
    actor = getattr(config, "authorized_id", None)
    checker = getattr(runtime, "_is_authorized_user", None)
    if type(actor) is not int or actor <= 0 or (callable(checker) and not checker(actor)):
        return _error("forbidden", 403)
    try:
        session = runtime_session.current_session(runtime, surface="workbench", channel_key="default")
        store = runtime_session.ensure_store(runtime)
        owner_id = runtime_session.owner_id(runtime)
    except Exception:
        return _error("session_unavailable", 503)
    try:
        projection = build_chat_projection(
            store,
            session=session,
            owner_id=owner_id,
            limit=payload.get("limit", 200),
            offset=payload.get("offset"),
        )
    except Exception:
        # Neither the envelope nor transcript text belongs in the command audit
        # or failure logs. Return a typed terminal error instead of chat fallback.
        logger.warning("Chat projection read unavailable")
        return _error("read_unavailable", 503)
    return {"ok": True, "chat_projection_version": 1, "projection": projection}
