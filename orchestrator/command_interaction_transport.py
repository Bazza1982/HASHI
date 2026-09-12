"""Worker-owned command-menu transport over the existing runtime.slash RPC.

The shared Backend API and Supervisor deliberately remain unaware of this
envelope.  A Workbench server serializes its already allowlisted request into a
reserved command string; the current Agent Worker validates it again, derives
identity and Session state locally, and dispatches the disposable projection.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import Mapping
from typing import Any

from orchestrator.command_interactions import InteractionError, validate_operation
from orchestrator import command_interaction_bridge, runtime_session

logger = logging.getLogger("HASHI.CommandInteractions")

TRANSPORT_PREFIX = "__hashi_command_ui_v1__:"
MAX_TRANSPORT_LENGTH = 32 * 1024
_FIELDS = frozenset(
    {
        "version",
        "op",
        "client_id",
        "request_id",
        "ui_locale",
        "connection_binding",
        "command",
        "menu_id",
        "revision",
        "button_id",
    }
)


def _decode_transport(text: str) -> dict[str, Any]:
    encoded = text[len(TRANSPORT_PREFIX) :]
    if not encoded or len(text) > MAX_TRANSPORT_LENGTH:
        raise InteractionError("command_menu_request_invalid", 400)
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(
            (encoded + padding).encode("ascii"), altchars=b"-_", validate=True
        )
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeEncodeError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError):
        raise InteractionError("command_menu_request_invalid", 400) from None
    if not isinstance(value, Mapping) or set(value) - _FIELDS:
        raise InteractionError("command_menu_request_invalid", 400)
    payload = dict(value)
    validate_operation(payload)
    connection = payload.get("connection_binding")
    if not isinstance(connection, str) or not 16 <= len(connection) <= 256:
        raise InteractionError("command_menu_binding_missing", 400)
    return payload


async def try_dispatch_command_interaction_transport(
    runtime: Any,
    text: str,
    *,
    source_channel: str,
) -> dict[str, Any] | None:
    """Return ``None`` for ordinary text and a terminal result for this wire."""
    if not isinstance(text, str) or not text.startswith(TRANSPORT_PREFIX):
        return None
    try:
        if str(source_channel or "").strip().lower() != "workbench_api":
            raise InteractionError("command_menu_forbidden", 403)
        payload = _decode_transport(text)
        config = runtime.global_config
        if str(getattr(config, "deployment_profile", "personal") or "personal") != "personal":
            raise InteractionError("command_menu_governed_not_supported", 501)
        actor = getattr(config, "authorized_id", None)
        checker = getattr(runtime, "_is_authorized_user", None)
        if type(actor) is not int or (callable(checker) and not checker(actor)):
            raise InteractionError("command_menu_forbidden", 403)
        metadata: dict[str, Any] = {
            "actor_id": actor,
            "instance_id": str(getattr(config, "instance_id", "") or ""),
            "session_surface": "workbench",
            "session_channel_key": "default",
            "connection_binding": payload.pop("connection_binding"),
        }
        if payload["op"] != "catalogue":
            try:
                session = runtime_session.current_session(
                    runtime,
                    surface="workbench",
                    channel_key="default",
                )
                metadata.update(
                    owner_id=runtime_session.owner_id(runtime),
                    session_id=str(session["session_id"]),
                    context_generation=int(session["context_generation"]),
                )
            except Exception:
                raise InteractionError("command_menu_session_unavailable", 503) from None
        try:
            result = await command_interaction_bridge.dispatch_command_interaction(
                runtime, payload, metadata
            )
        except InteractionError:
            raise
        except Exception:
            logger.warning("Command interaction transport failed during dispatch")
            raise InteractionError("command_menu_outcome_unknown", 502) from None
        if not isinstance(result, Mapping):
            raise InteractionError("command_menu_response_invalid", 502)
        return dict(result)
    except InteractionError as exc:
        return exc.result()
