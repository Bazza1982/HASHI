"""
Hashi Remote API Server.

FastAPI application exposing endpoints for inter-HASHI communication:

  GET  /health          — instance info + peer list
  GET  /peers           — discovered LAN peers
  POST /hchat           — receive hchat from remote, relay to local workbench
  POST /terminal/exec   — execute shell command (auth-gated)
  POST /files/push      — receive a file and atomically write it to a path
  GET  /files/stat      — inspect a remote file path and checksum
  POST /pair/request    — initiate pairing
  GET  /pair/status     — check pairing request status
  POST /pair/approve    — approve a pending pairing request
  POST /pair/reject     — reject a pending pairing request

Adapted from Lily Remote (agent/api/server.py) — screen/input endpoints
replaced with hchat relay and terminal execution.
"""

import base64
import hashlib
import asyncio
import json
import logging
import os
import platform
import re
import socket
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib import request as urllib_request
from urllib.error import URLError

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..security.auth import (
    has_shared_token,
    is_lan_mode,
    protocol_auth_mode,
    set_lan_mode,
    set_pairing_manager,
    set_shared_token,
    try_authenticate_request,
    verify_protocol_request,
    verify_token,
)
from ..security.shared_token import load_shared_token
from ..security.pairing import PairingManager
from ..terminal.executor import TerminalExecutor, AuthLevel
from ..audit.logger import get_audit_logger

logger = logging.getLogger(__name__)

# These are set by main.py after startup
_instance_info: dict = {}
_peer_registry = None
_pairing_manager: Optional[PairingManager] = None
_terminal_executor: Optional[TerminalExecutor] = None
_protocol_manager = None
_hashi_root: Optional[str] = None
_workbench_port: int = 18800
_HCHAT_HEADER_RE = re.compile(r"^\[hchat from (?P<agent>\w+)(?:@(?P<instance>[\w-]+))?\]\s*(?P<body>.*)$", re.DOTALL)


def _redacted_protocol_status() -> dict[str, Any]:
    status = _protocol_manager.get_protocol_status() if _protocol_manager is not None else {}
    capabilities = []
    remote_supervisor = {}
    if status:
        capabilities = list(status.get("capabilities") or [])
        remote_supervisor = dict(status.get("remote_supervisor") or {})
    return {
        "protocol_version": status.get("protocol_version", "2.0"),
        "display_handle": getattr(_protocol_manager, "display_handle", f"@{str(_instance_info.get('instance_id') or 'hashi').lower()}"),
        "capabilities": capabilities,
        "remote_supervisor": remote_supervisor,
        "protocol_auth_mode": protocol_auth_mode(),
        "shared_token_configured": has_shared_token(),
        "lan_mode": is_lan_mode(),
        "trusted_view": False,
    }


# ─────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────

class HchatPayload(BaseModel):
    from_instance: str          # e.g. "HASHI9"
    to_agent: str               # e.g. "lily"
    text: str                   # The message content (already formatted)
    to_instance: Optional[str] = None  # Final target instance for HASHI1 exchange
    source_hchat_format: bool = False  # If True, text is raw hchat format
    reply_route: Optional[dict] = None  # Sender's routing info for reply delivery


class TerminalExecPayload(BaseModel):
    command: str
    cwd: Optional[str] = None


class FilePushPayload(BaseModel):
    dest_path: str
    content_b64: str
    sha256: Optional[str] = None
    overwrite: bool = False
    create_dirs: bool = True


class HashiStartPayload(BaseModel):
    reason: Optional[str] = None


class PairRequestPayload(BaseModel):
    client_id: str
    client_name: str


class ProtocolHandshakePayload(BaseModel):
    from_instance: str
    display_handle: Optional[str] = None
    protocol_version: str = "2.0"
    capabilities: list[str] = []
    hashi_version: Optional[str] = None
    agents: list[dict] = []
    agent_directory: dict = {}
    remote_port: Optional[int] = None          # Sender's own Hashi Remote port
    workbench_port: Optional[int] = None       # Sender's workbench port
    platform: Optional[str] = None             # Sender's platform
    host_identity: Optional[str] = None
    environment_kind: Optional[str] = None
    address_candidates: list[dict] = []
    observed_candidates: list[dict] = []


class ProtocolMessagePayload(BaseModel):
    message_id: str
    conversation_id: str
    in_reply_to: Optional[str] = None
    from_instance: str
    from_agent: str
    to_instance: str
    to_agent: str
    body: dict
    hop_count: int = 0
    ttl: int = 8
    route_trace: list[str] = []
    message_type: str = "agent_message"
    created_at: Optional[str] = None


# ─────────────────────────────────────────────────────────────
# App factory
# ─────────────────────────────────────────────────────────────

MAX_FILE_PUSH_BYTES = 256 * 1024 * 1024


def _resolve_file_push_destination(dest_path: str) -> Path:
    raw = str(dest_path or "").strip()
    if not raw:
        raise ValueError("dest_path is required")
    if "\x00" in raw:
        raise ValueError("dest_path contains an invalid NUL byte")

    expanded = Path(raw).expanduser()
    if expanded.is_absolute():
        return expanded

    if _hashi_root:
        root = Path(_hashi_root).resolve()
        resolved = (root / expanded).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError("relative dest_path must stay inside the Hashi root")
        return resolved

    raise ValueError("relative dest_path is not allowed because Hashi root is unavailable")


def _decode_file_push_content(payload: FilePushPayload) -> tuple[bytes, str]:
    try:
        data = base64.b64decode(payload.content_b64.encode("ascii"), validate=True)
    except Exception as exc:
        raise ValueError(f"content_b64 is not valid base64: {exc}") from exc

    if len(data) > MAX_FILE_PUSH_BYTES:
        raise ValueError(f"file exceeds max size of {MAX_FILE_PUSH_BYTES} bytes")

    digest = hashlib.sha256(data).hexdigest()
    expected = str(payload.sha256 or "").strip().lower()
    if expected and expected != digest:
        raise ValueError("sha256 mismatch")
    return data, digest


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_hashi_pid_state() -> dict[str, Any]:
    state: dict[str, Any] = {
        "pid_file_exists": False,
        "pid": None,
        "pid_alive": False,
    }
    if not _hashi_root:
        return state
    pid_path = Path(_hashi_root) / ".bridge_u_f.pid"
    state["pid_file_exists"] = pid_path.exists()
    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
        if not raw.isdigit():
            return state
        pid = int(raw)
    except Exception:
        return state
    state["pid"] = pid
    state["pid_alive"] = _process_exists(pid)
    return state


def _hashi_start_command() -> list[str]:
    if not _hashi_root:
        raise ValueError("Hashi root is unavailable")
    root = Path(_hashi_root)
    system = platform.system().lower()
    if system == "windows":
        ctl = root / "bin" / "bridge_ctl.ps1"
        if ctl.exists():
            return [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ctl),
                "-Action",
                "start",
                "-Resume",
            ]
        bat = root / "bin" / "bridge-u.bat"
        if bat.exists():
            return ["cmd.exe", "/c", str(bat), "--resume-last", "--no-pause"]
    launcher = root / "bin" / "bridge-u.sh"
    if launcher.exists():
        return [str(launcher), "--resume-last"]
    raise FileNotFoundError("No supported HASHI launcher found under bin/")


def _start_hashi_process() -> dict[str, Any]:
    if not _hashi_root:
        raise ValueError("Hashi root is unavailable")
    root = Path(_hashi_root)
    cmd = _hashi_start_command()
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "remote_rescue_hashi_start.log"
    log_handle = log_path.open("ab")
    kwargs: dict[str, Any] = {
        "cwd": str(root),
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
    }
    if platform.system().lower() == "windows":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        if flags:
            kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    finally:
        log_handle.close()
    return {
        "pid": proc.pid,
        "command": cmd,
        "log_path": str(log_path),
    }


def _append_rescue_audit(
    *,
    requester: str,
    reason: str | None,
    outcome: str,
    command: list[str] | None = None,
    pid: int | None = None,
    log_path: str | None = None,
    status: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    if not _hashi_root:
        return
    audit_path = Path(_hashi_root) / "logs" / "remote_rescue_audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "requester": requester,
        "reason": reason,
        "command": command,
        "pid": pid,
        "log_path": log_path,
        "outcome": outcome,
        "status_state": (status or {}).get("state"),
        "error": error,
    }
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _workbench_health_url() -> str:
    return f"http://127.0.0.1:{_workbench_port}/api/health"


def _fetch_workbench_health(timeout: float = 1.0) -> dict[str, Any] | None:
    req = urllib_request.Request(_workbench_health_url(), method="GET")
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _hashi_control_status() -> dict[str, Any]:
    health = _fetch_workbench_health(timeout=1.0)
    pid_state = _read_hashi_pid_state()
    if health:
        state = "running"
    elif pid_state["pid_alive"]:
        state = "starting_or_stuck"
    elif pid_state["pid_file_exists"]:
        state = "stale_pid"
    else:
        state = "offline"
    return {
        "ok": True,
        "state": state,
        "hashi_running": bool(health),
        "workbench_url": _workbench_health_url(),
        "workbench_health": health,
        **pid_state,
    }

def create_app(
    instance_info: dict,
    pairing_manager: PairingManager,
    terminal_executor: TerminalExecutor,
    peer_registry=None,
    protocol_manager=None,
    workbench_port: int = 18800,
    hashi_root: str = None,
) -> FastAPI:
    """Create the FastAPI application with all context injected."""

    global _instance_info, _peer_registry, _pairing_manager, _terminal_executor
    global _workbench_port, _hashi_root, _protocol_manager

    _instance_info = instance_info
    _peer_registry = peer_registry
    _pairing_manager = pairing_manager
    _terminal_executor = terminal_executor
    _protocol_manager = protocol_manager
    _workbench_port = workbench_port
    _hashi_root = hashi_root

    set_pairing_manager(pairing_manager)
    set_lan_mode(pairing_manager.lan_mode)
    set_shared_token(load_shared_token(Path(hashi_root) if hashi_root else None))

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
    async def health(request: Request):
        peers = []
        authenticated = try_authenticate_request(request, allow_loopback=True)
        if _peer_registry:
            if _protocol_manager:
                peers = [_protocol_manager.get_peer_view(p) for p in _peer_registry.get_peers()]
            else:
                peers = [p.to_dict() for p in _peer_registry.get_peers()]
        local_network_profile = _protocol_manager._local_network_profile() if _protocol_manager else None
        if not authenticated:
            instance_view = {
                "instance_id": _instance_info.get("instance_id"),
                "display_name": _instance_info.get("display_name"),
                "workbench_port": _instance_info.get("workbench_port"),
                "platform": _instance_info.get("platform"),
                "hashi_version": _instance_info.get("hashi_version"),
                "remote_port": _instance_info.get("remote_port"),
            }
            if _protocol_manager:
                instance_view["capabilities"] = list(_protocol_manager.get_protocol_status().get("capabilities") or [])
            return {
                "ok": True,
                "instance": instance_view,
                "hostname": socket.gethostname(),
                "platform": platform.system().lower(),
                "ts": time.time(),
                "peer_count": len(peers),
                "protocol_auth_mode": protocol_auth_mode(),
                "lan_mode": is_lan_mode(),
                "trusted_view": False,
                "shared_token_configured": has_shared_token(),
            }
        return {
            "ok": True,
            "instance": _instance_info,
            "hostname": socket.gethostname(),
            "platform": platform.system().lower(),
            "ts": time.time(),
            "local_network_profile": local_network_profile,
            "peers": peers,
            "protocol_auth_mode": protocol_auth_mode(),
            "lan_mode": is_lan_mode(),
            "trusted_view": True,
        }

    # ── Peers ────────────────────────────────────────────────

    @app.get("/peers")
    async def list_peers(request: Request):
        peers = []
        if _peer_registry:
            if _protocol_manager:
                peers = [_protocol_manager.get_peer_view(p) for p in _peer_registry.get_peers()]
            else:
                peers = [p.to_dict() for p in _peer_registry.get_peers()]
        authenticated = try_authenticate_request(request, allow_loopback=True)
        if not authenticated:
            return {"ok": True, "peers": [], "count": len(peers), "trusted_view": False}
        return {"ok": True, "peers": peers, "count": len(peers)}

    @app.get("/protocol/status")
    async def protocol_status(request: Request):
        if _protocol_manager is None:
            return {"ok": False, "error": "protocol manager unavailable"}
        authenticated = try_authenticate_request(request, allow_loopback=True)
        if not authenticated:
            return {"ok": True, **_redacted_protocol_status()}
        return {
            "ok": True,
            **_protocol_manager.get_protocol_status(),
            "protocol_auth_mode": protocol_auth_mode(),
            "shared_token_configured": has_shared_token(),
            "lan_mode": is_lan_mode(),
            "trusted_view": True,
        }

    @app.post("/protocol/handshake")
    async def protocol_handshake(request: Request, payload: ProtocolHandshakePayload):
        if _protocol_manager is None:
            return JSONResponse(status_code=503, content={"status": "handshake_reject", "reason": "protocol unavailable"})
        body_bytes = await request.body()
        ok, reason, _auth_identity = verify_protocol_request(
            request,
            body_bytes=body_bytes,
            from_instance=payload.from_instance,
        )
        if not ok:
            logger.warning("Protocol handshake rejected: reason=%s from=%s", reason, payload.from_instance)
            return JSONResponse(status_code=401, content={"status": "handshake_reject", "reason": reason})
        # Pass the sender's IP so we can register them as a peer (reverse registration)
        client_ip = request.client.host if request.client else None
        data = payload.model_dump()
        data["_client_ip"] = client_ip
        result = _protocol_manager.handle_handshake(data)
        status = 200 if str(result.get("status")) == "handshake_accept" else 409
        return JSONResponse(status_code=status, content=result)

    @app.get("/protocol/agents")
    async def protocol_agents():
        if _protocol_manager is None:
            return JSONResponse(status_code=503, content={"ok": False, "error": "protocol manager unavailable"})
        return {"ok": True, "agents": _protocol_manager.get_local_agents_snapshot()}

    @app.post("/protocol/message")
    async def protocol_message(request: Request, payload: ProtocolMessagePayload):
        if _protocol_manager is None:
            return JSONResponse(status_code=503, content={"ok": False, "error": "protocol manager unavailable"})
        body_bytes = await request.body()
        ok, reason, _auth_identity = verify_protocol_request(
            request,
            body_bytes=body_bytes,
            from_instance=payload.from_instance,
        )
        if not ok:
            logger.warning("Protocol message rejected: reason=%s from=%s", reason, payload.from_instance)
            return JSONResponse(
                status_code=401,
                content={
                    "ok": False,
                    "message_type": "error",
                    "body": {
                        "code": reason,
                        "message": "Protocol authentication failed",
                        "retryable": reason == "auth_required",
                        "from_instance": payload.from_instance,
                        "to_instance": payload.to_instance,
                        "to_agent": payload.to_agent,
                    },
                },
            )
        status, result = await _protocol_manager.handle_protocol_message(payload.model_dump())
        return JSONResponse(status_code=status, content=result)

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

        local_instance_id = str(_instance_info.get("instance_id", "")).upper()
        requested_instance = (payload.to_instance or "").strip().upper()

        if requested_instance and requested_instance != local_instance_id:
            try:
                from tools.hchat_send import parse_hchat_message, send_hchat

                parsed = parse_hchat_message(payload.text, default_instance=payload.from_instance)
                if not parsed:
                    return JSONResponse(
                        status_code=400,
                        content={"ok": False, "error": "Invalid exchange hchat format"},
                    )

                ok = send_hchat(
                    payload.to_agent,
                    parsed["agent"],
                    parsed["body"],
                    target_instance=requested_instance,
                    source_instance=parsed["instance_id"] or payload.from_instance,
                    reply_route_override=payload.reply_route,
                )
                if ok:
                    logger.info(
                        "HChat exchange relayed: %s/%s → %s@%s via %s",
                        parsed["instance_id"] or payload.from_instance,
                        parsed["agent"],
                        payload.to_agent,
                        requested_instance,
                        local_instance_id,
                    )
                    return {"ok": True, "relayed": True, "exchange": True}
                return JSONResponse(
                    status_code=502,
                    content={"ok": False, "error": "Exchange forwarding failed"},
                )
            except Exception as e:
                logger.exception("HChat exchange failed")
                return JSONResponse(
                    status_code=500,
                    content={"ok": False, "error": "Exchange forwarding error", "detail": str(e)},
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

    # ── HASHI process rescue control ─────────────────────────

    @app.get("/control/hashi/status")
    async def hashi_control_status(client_id: str = Depends(verify_token)):
        """Report whether the local HASHI core appears reachable."""
        return _hashi_control_status()

    @app.post("/control/hashi/start")
    async def hashi_control_start(payload: HashiStartPayload, client_id: str = Depends(verify_token)):
        """
        Start the local HASHI core through a fixed launcher command.

        This is intentionally not a generic shell endpoint. It is gated by
        L3_RESTART because it is a rescue operation that creates a long-lived
        local process.
        """
        if not _terminal_executor or not _terminal_executor.allows_level(AuthLevel.L3_RESTART):
            return JSONResponse(
                status_code=403,
                content={
                    "ok": False,
                    "error": "HASHI start requires max_terminal_level=L3_RESTART",
                },
            )

        current = _hashi_control_status()
        if current["hashi_running"] or current["pid_alive"]:
            _append_rescue_audit(
                requester=client_id,
                reason=payload.reason,
                outcome="already_running",
                pid=current.get("pid"),
                status=current,
            )
            return {
                "ok": True,
                "started": False,
                "already_running": True,
                "status": current,
            }

        try:
            started = _start_hashi_process()
        except Exception as exc:
            logger.exception("HASHI rescue start failed")
            _append_rescue_audit(
                requester=client_id,
                reason=payload.reason,
                outcome="failed",
                status=current,
                error=str(exc),
            )
            return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})

        deadline = time.monotonic() + 8.0
        status = _hashi_control_status()
        while not status["hashi_running"] and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            status = _hashi_control_status()

        logger.info("HASHI rescue start requested by %s reason=%s pid=%s", client_id, payload.reason, started["pid"])
        _append_rescue_audit(
            requester=client_id,
            reason=payload.reason,
            outcome="started",
            command=started["command"],
            pid=started["pid"],
            log_path=started["log_path"],
            status=status,
        )
        return {
            "ok": True,
            "started": True,
            "pid": started["pid"],
            "command": started["command"],
            "log_path": started["log_path"],
            "status": status,
        }

    # ── File push ────────────────────────────────────────────

    @app.post("/files/push")
    async def file_push(payload: FilePushPayload, client_id: str = Depends(verify_token)):
        """
        Receive a file from another HASHI instance and atomically write it to
        the requested destination path.
        """
        try:
            dest = _resolve_file_push_destination(payload.dest_path)
            data, digest = _decode_file_push_content(payload)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

        if dest.exists() and not payload.overwrite:
            return JSONResponse(
                status_code=409,
                content={"ok": False, "error": "destination exists; pass overwrite=true to replace it"},
            )
        if dest.exists() and dest.is_dir():
            return JSONResponse(status_code=409, content={"ok": False, "error": "destination is a directory"})
        if not dest.parent.exists():
            if not payload.create_dirs:
                return JSONResponse(status_code=409, content={"ok": False, "error": "destination parent does not exist"})
            dest.parent.mkdir(parents=True, exist_ok=True)

        tmp_path = dest.with_name(f".{dest.name}.hashi-upload-{int(time.time() * 1000)}")
        try:
            tmp_path.write_bytes(data)
            tmp_path.replace(dest)
        except Exception as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})

        logger.info("File push from %s wrote %s (%d bytes)", client_id, dest, len(data))
        return {
            "ok": True,
            "dest_path": str(dest),
            "bytes_written": len(data),
            "sha256": digest,
            "overwritten": bool(payload.overwrite),
        }

    @app.get("/files/stat")
    async def file_stat(path: str, client_id: str = Depends(verify_token)):
        """Return existence, size, and sha256 for a remote path."""
        try:
            target = _resolve_file_push_destination(path)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
        if not target.exists():
            return {"ok": True, "exists": False, "path": str(target)}
        if target.is_dir():
            return {"ok": True, "exists": True, "path": str(target), "type": "directory"}
        data = target.read_bytes()
        return {
            "ok": True,
            "exists": True,
            "path": str(target),
            "type": "file",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

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
