"""Authenticated, point-to-point HAL dispatch and terminal Run evidence."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from aiohttp import web

from hal.dispatch import DispatchResult, classify_dispatch_error
from orchestrator.agent_move.remote_client import (
    AgentMoveRemoteError,
    candidate_remote_base_urls,
    request_authenticated_json,
)
from remote.security.shared_token import load_shared_token

_PEER_PATH = "/workbench/v1/proxy/api/v1/hal/peer"
_AGENT = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}", re.IGNORECASE | re.ASCII)
_KEY = re.compile(r"hal:[a-z0-9_-]{1,128}:[0-9]{1,6}", re.ASCII)
_MAX_PROMPT = 64 * 1024


class RemoteTerminalUnavailable(RuntimeError):
    """The peer could not yet prove a terminal outcome."""


class HalPeerClient:
    def __init__(self, root: Path, instance_id: str):
        self.root = Path(root)
        self.instance = str(instance_id or "").upper()

    def _call(
        self, target: str, agent: str, key: str, *, text: str | None = None,
    ) -> dict[str, Any]:
        from orchestrator.runtime_remote import load_instances
        from orchestrator.version_remote import resolve_instance_entry

        target = target.upper()
        token = load_shared_token(self.root)
        if not token or not self.instance or target == self.instance:
            raise RemoteTerminalUnavailable(f"HAL peer @{target} authentication unavailable")
        try:
            instance, entry = resolve_instance_entry(load_instances(project_root=self.root), target)
            if instance != target:
                raise RemoteTerminalUnavailable(f"HAL peer @{target} identity mismatch")
            candidates = candidate_remote_base_urls(entry)
        except (AgentMoveRemoteError, ValueError, RuntimeError) as exc:
            raise RemoteTerminalUnavailable(f"HAL peer @{target} route unavailable") from exc

        body = {
            "operation": "terminal" if text is None else "prompt",
            "source_instance": self.instance,
            "target_instance": target,
            "agent_id": agent,
            "idempotency_key": key,
        }
        if text is not None:
            body["text"] = text
        for base_url in candidates:
            sent_prompt = False
            try:
                if text is not None:
                    status = request_authenticated_json(
                        f"{base_url}/workbench/v1/status", shared_token=token,
                        from_instance=self.instance, timeout=3,
                    )
                    if (
                        str((status.get("instance") or {}).get("instance_id") or "").upper() != target
                        or status.get("authenticated_instance") != self.instance
                    ):
                        continue
                    sent_prompt = True
                result = request_authenticated_json(
                    f"{base_url}{_PEER_PATH}", method="POST", payload=body,
                    shared_token=token, from_instance=self.instance, timeout=4,
                )
            except AgentMoveRemoteError:
                # Once a verified target was sent a prompt, let the durable HAL
                # retry path reconcile it using the same Run key.
                if sent_prompt:
                    break
                continue
            if (
                str(result.get("instance_id") or "").upper() == target
                and result.get("idempotency_key") == key
                and result.get("agent_id") == agent
            ):
                return result
            if text is not None:
                break
        raise RemoteTerminalUnavailable(f"HAL peer @{target} unavailable or identity could not be verified")

    async def dispatch_prompt(self, target: str, agent: str, text: str, key: str) -> DispatchResult:
        try:
            result = await asyncio.to_thread(self._call, target, agent, key, text=text)
        except RemoteTerminalUnavailable as exc:
            return DispatchResult(False, True, str(exc))
        run_id = result.get("run_id")
        if result.get("accepted") is True and isinstance(run_id, str) and run_id:
            return DispatchResult(True, False, f"Run {run_id} accepted on @{target.upper()}")
        return DispatchResult(
            False, result.get("retryable") is True,
            f"HAL peer @{target.upper()}: {result.get('error_code') or 'run receipt unavailable'}",
        )

    async def terminal_event(self, target: str, agent: str, key: str) -> dict | None:
        result = await asyncio.to_thread(self._call, target, agent, key)
        event = result.get("event")
        if event is not None and (
            not isinstance(event, dict)
            or event.get("agent_id") != agent
            or event.get("run_idempotency_key") != key
            or not event.get("run_id")
        ):
            raise RemoteTerminalUnavailable(f"HAL peer @{target.upper()} returned unrelated Run evidence")
        return event


async def handle_hal_peer_request(server, owner_id: str, body: bytes) -> web.Response:
    """Accept a single prompt or read its terminal evidence in the owner scope."""
    from orchestrator.automation_events import terminal_event_for_key
    from orchestrator.runtime_session import AGENT_ACTIVITY_SURFACE
    from orchestrator.session_store import SessionConflict

    local = str(server.session_store.instance_id or "").upper()
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        payload = None
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error_code": "hal_peer_invalid"}, status=400)
    operation = payload.get("operation")
    agent = payload.get("agent_id")
    key = payload.get("idempotency_key")
    source = payload.get("source_instance")
    if (
        not local or payload.get("target_instance") != local
        or not isinstance(source, str) or not source or source.upper() == local
        or owner_id.startswith("enterprise:")
        or operation not in ("prompt", "terminal")
        or not isinstance(agent, str) or not _AGENT.fullmatch(agent)
        or not isinstance(key, str) or not _KEY.fullmatch(key)
    ):
        return web.json_response({"ok": False, "error_code": "hal_peer_scope_invalid"}, status=400)

    reply = {"ok": True, "instance_id": local, "agent_id": agent, "idempotency_key": key}
    if operation == "terminal":
        event = await asyncio.to_thread(
            terminal_event_for_key, server.session_store, owner_id=owner_id, run_idempotency_key=key,
        )
        return web.json_response({**reply, "event": event if event and event["agent_id"] == agent else None})

    text = payload.get("text")
    try:
        text_bytes = text.encode("utf-8") if isinstance(text, str) else b""
    except UnicodeEncodeError:
        text_bytes = b""
    if not text_bytes.strip() or len(text_bytes) > _MAX_PROMPT:
        return web.json_response({**reply, "accepted": False, "error_code": "hal_peer_prompt_invalid"})
    runtime = server._runtime_map().get(agent)
    if runtime is None:
        return web.json_response({**reply, "accepted": False, "retryable": True, "error_code": "agent_unavailable"})

    # The Agent's canonical activity Session is stable across Workbench
    # selection, Telegram routing and worker restarts. PAO owns Run admission
    # and idempotency for the same key in that Session.
    try:
        session = await asyncio.to_thread(
            server.session_store.ensure_agent_activity_session,
            owner_id=owner_id, agent_id=agent,
        )
        request_id = await runtime.enqueue_api_text(
            text, source="hal", chat_id=None, deliver_to_telegram=False, idempotency_key=key,
            request_metadata={
                "session_id": session["session_id"], "owner_id": owner_id,
                "session_surface": AGENT_ACTIVITY_SURFACE, "origin": "hal",
            },
        )
        if not request_id:
            raise SessionConflict("HAL Run was not admitted")
        run = await asyncio.to_thread(
            server.session_store.get_run_by_request, request_id,
            owner_id=owner_id, agent_id=agent,
        )
    except Exception as exc:  # noqa: BLE001 — preserve admission classification without exposing the prompt
        transient, kind = classify_dispatch_error(exc)
        return web.json_response({**reply, "accepted": False, "retryable": transient, "error_code": kind})
    if run["session_id"] != session["session_id"] or run["idempotency_key"] != key:
        return web.json_response({**reply, "accepted": False, "error_code": "run_receipt_mismatch"})
    return web.json_response({**reply, "accepted": True, "run_id": run["run_id"]})
