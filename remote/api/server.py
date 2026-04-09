"""
Hashi Remote API Server.

FastAPI application exposing endpoints for inter-HASHI communication:

  GET  /health          — instance info + peer list
  GET  /peers           — discovered LAN peers
  POST /hchat           — receive hchat from remote, relay to local workbench
  POST /terminal/exec   — execute shell command (auth-gated)
  POST /pair/request    — initiate pairing
  GET  /pair/status     — check pairing request status
  POST /pair/approve    — approve a pending pairing request
  POST /pair/reject     — reject a pending pairing request

Adapted from Lily Remote (agent/api/server.py) — screen/input endpoints
replaced with hchat relay and terminal execution.
"""

import json
import logging
import platform
import socket
import time
from contextlib import asynccontextmanager
from typing import Any, Optional
from urllib import request as urllib_request
from urllib.error import URLError

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..security.auth import verify_token, set_pairing_manager, set_lan_mode
from ..security.pairing import PairingManager
from ..terminal.executor import TerminalExecutor, AuthLevel
from ..audit.logger import get_audit_logger

logger = logging.getLogger(__name__)

# These are set by main.py after startup
_instance_info: dict = {}
_peer_registry = None
_pairing_manager: Optional[PairingManager] = None
_terminal_executor: Optional[TerminalExecutor] = None
_hashi_root: Optional[str] = None
_workbench_port: int = 18800


# ─────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────

class HchatPayload(BaseModel):
    from_instance: str          # e.g. "HASHI9"
    to_agent: str               # e.g. "lily"
    text: str                   # The message content (already formatted)
    source_hchat_format: bool = False  # If True, text is raw hchat format
    reply_route: Optional[dict] = None  # Sender's routing info for reply delivery


class TerminalExecPayload(BaseModel):
    command: str
    cwd: Optional[str] = None


class PairRequestPayload(BaseModel):
    client_id: str
    client_name: str


# ─────────────────────────────────────────────────────────────
# App factory
# ─────────────────────────────────────────────────────────────

def create_app(
    instance_info: dict,
    pairing_manager: PairingManager,
    terminal_executor: TerminalExecutor,
    peer_registry=None,
    workbench_port: int = 18800,
    hashi_root: str = None,
) -> FastAPI:
    """Create the FastAPI application with all context injected."""

    global _instance_info, _peer_registry, _pairing_manager, _terminal_executor
    global _workbench_port, _hashi_root

    _instance_info = instance_info
    _peer_registry = peer_registry
    _pairing_manager = pairing_manager
    _terminal_executor = terminal_executor
    _workbench_port = workbench_port
    _hashi_root = hashi_root

    set_pairing_manager(pairing_manager)
    set_lan_mode(pairing_manager.lan_mode)

    app = FastAPI(
        title="Hashi Remote",
        description="HASHI inter-instance communication and control",
        version="1.0.0",
        docs_url="/docs",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Health ──────────────────────────────────────────────

    @app.get("/health")
    async def health():
        peers = []
        if _peer_registry:
            peers = [p.to_dict() for p in _peer_registry.get_peers()]
        return {
            "ok": True,
            "instance": _instance_info,
            "hostname": socket.gethostname(),
            "platform": platform.system().lower(),
            "ts": time.time(),
            "peers": peers,
        }

    # ── Peers ────────────────────────────────────────────────

    @app.get("/peers")
    async def list_peers():
        peers = []
        if _peer_registry:
            peers = [p.to_dict() for p in _peer_registry.get_peers()]
        return {"ok": True, "peers": peers, "count": len(peers)}

    # ── HChat relay ─────────────────────────────────────────

    @app.post("/hchat")
    async def receive_hchat(payload: HchatPayload, client_id: str = Depends(verify_token)):
        """
        Receive an hchat message from a remote HASHI instance and
        relay it into the local Workbench API.
        """
        audit = get_audit_logger()
        audit.log_hchat_received(
            from_instance=payload.from_instance,
            to_agent=payload.to_agent,
            text_snippet=payload.text,
        )

        # Format for workbench injection
        if payload.source_hchat_format:
            message_text = payload.text  # already formatted
        else:
            message_text = f"[hchat from {payload.from_instance.lower()}] {payload.text}"

        # POST to local Workbench API
        workbench_url = f"http://127.0.0.1:{_workbench_port}/api/chat"
        wb_payload = {
            "agent": payload.to_agent.lower(),
            "text": message_text,
        }
        if payload.reply_route:
            wb_payload["reply_route"] = payload.reply_route
        post_data = json.dumps(wb_payload).encode("utf-8")

        req = urllib_request.Request(
            workbench_url,
            data=post_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib_request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                if result.get("ok"):
                    logger.info("HChat relayed: %s → %s", payload.from_instance, payload.to_agent)
                    return {"ok": True, "relayed": True}
                else:
                    logger.warning("Workbench rejected hchat: %s", result)
                    return JSONResponse(
                        status_code=502,
                        content={"ok": False, "error": "Workbench rejected message", "detail": result},
                    )
        except URLError as e:
            logger.error("Workbench unreachable: %s", e)
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": "Local workbench unreachable", "detail": str(e)},
            )

    # ── Terminal exec ────────────────────────────────────────

    @app.post("/terminal/exec")
    async def terminal_exec(payload: TerminalExecPayload, client_id: str = Depends(verify_token)):
        """Execute a shell command on this machine."""
        if not _terminal_executor:
            raise HTTPException(status_code=503, detail="Terminal executor not available")

        audit = get_audit_logger()
        allowed, auth_level = _terminal_executor.is_allowed(payload.command)
        audit.log_terminal_exec(client_id, payload.command, allowed)

        result = await _terminal_executor.execute(payload.command, cwd=payload.cwd)
        return result.to_dict()

    # ── Pairing ──────────────────────────────────────────────

    @app.post("/pair/request")
    async def pair_request(payload: PairRequestPayload):
        if _pairing_manager.is_auto_approved():
            # LAN mode: auto-approve immediately
            token = _pairing_manager.approve_request_direct(
                payload.client_id, payload.client_name
            )
            audit = get_audit_logger()
            audit.log_pairing_request(payload.client_id, payload.client_name, auto_approved=True)
            return {"ok": True, "auto_approved": True, "token": token}

        req = _pairing_manager.create_pairing_request(payload.client_id, payload.client_name)
        audit = get_audit_logger()
        audit.log_pairing_request(payload.client_id, payload.client_name, auto_approved=False)
        return {
            "ok": True,
            "auto_approved": False,
            "request_id": req.client_id,
            "challenge": req.challenge,
            "expires_at": req.expires_at,
        }

    @app.get("/pair/status/{client_id}")
    async def pair_status(client_id: str):
        req = _pairing_manager.get_request(client_id)
        if not req:
            raise HTTPException(status_code=404, detail="Pairing request not found")
        return {"ok": True, "state": req.state.value, "client_id": client_id}

    @app.post("/pair/approve/{client_id}")
    async def pair_approve(client_id: str, client_id_auth: str = Depends(verify_token)):
        token = _pairing_manager.approve_request(client_id)
        if not token:
            raise HTTPException(status_code=404, detail="Request not found or expired")
        return {"ok": True, "token": token}

    @app.post("/pair/reject/{client_id}")
    async def pair_reject(client_id: str, client_id_auth: str = Depends(verify_token)):
        ok = _pairing_manager.reject_request(client_id)
        return {"ok": ok}

    return app
