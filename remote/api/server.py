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
from urllib.parse import urlsplit
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..attachments import AttachmentStore
from ..security.auth import (
    authenticate_request_detailed,
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
from ..security.shared_token import build_auth_headers
from ..security.shared_token import load_shared_token
from ..security.pairing import PairingManager
from ..terminal.executor import TerminalExecutor, AuthLevel
from ..audit.logger import get_audit_logger
from ..local_http import local_http_hosts, local_http_url

logger = logging.getLogger(__name__)

# These are set by main.py after startup
_instance_info: dict = {}
_peer_registry = None
_pairing_manager: Optional[PairingManager] = None
_terminal_executor: Optional[TerminalExecutor] = None
_protocol_manager = None
_hashi_root: Optional[str] = None
_workbench_port: int = 18800
_attachment_store: Optional[AttachmentStore] = None
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


class AttachmentUploadPayload(BaseModel):
    message_id: str
    from_instance: str
    attachment_id: str
    filename: str
    mime_type: Optional[str] = None
    content_b64: str
    sha256: Optional[str] = None


class AttachmentCommitItem(BaseModel):
    attachment_id: str
    pending_upload_id: str
    filename: Optional[str] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    sha256: Optional[str] = None
    caption: Optional[str] = None


class AttachmentCancelPayload(BaseModel):
    message_id: str
    from_instance: str
    pending_upload_ids: list[str]
    reason: Optional[str] = None


class ProtocolMessageWithAttachmentsPayload(BaseModel):
    message_id: str
    conversation_id: str
    in_reply_to: Optional[str] = None
    from_instance: str
    from_agent: str
    to_instance: str
    to_agent: str
    body: dict
    attachments: list[AttachmentCommitItem]
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


def _attachment_summary_lines(attachments: list[dict[str, Any]]) -> list[str]:
    lines = ["", "[Remote attachments]"]
    for item in attachments:
        filename = str(item.get("filename") or "attachment")
        size_bytes = int(item.get("size_bytes") or 0)
        mime_type = str(item.get("mime_type") or "application/octet-stream")
        lines.append(f"- {filename} ({mime_type}, {size_bytes} bytes)")
    return lines


def _merge_attachment_text(body: dict[str, Any], attachments: list[dict[str, Any]]) -> dict[str, Any]:
    merged = dict(body or {})
    base_text = str((body or {}).get("text") or "").strip()
    attachment_block = "\n".join(_attachment_summary_lines(attachments))
    merged["text"] = f"{base_text}{attachment_block}" if base_text else attachment_block.strip()
    merged["attachments"] = attachments
    return merged


def _post_json_with_optional_hmac(url: str, payload: dict[str, Any], *, timeout: int = 15) -> dict[str, Any]:
    body_bytes = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    shared_token = load_shared_token(Path(_hashi_root) if _hashi_root else None)
    if shared_token:
        headers.update(
            build_auth_headers(
                shared_token=shared_token,
                method="POST",
                path=urlsplit(url).path,
                from_instance=str(_instance_info.get("instance_id") or ""),
                body_bytes=body_bytes,
            )
        )
    req = urllib_request.Request(url, data=body_bytes, headers=headers, method="POST")
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            result = json.loads(body) if body else {}
        except Exception:
            raise
        if isinstance(result, dict):
            result["__http_status"] = exc.code
        return result


def _process_exists(pid: int) -> bool:
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            process_query_limited_information = 0x1000
            handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
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


def _sanitize_rescue_reason(reason: str | None, *, limit: int = 500) -> dict[str, Any]:
    raw = "" if reason is None else str(reason)
    collapsed = " ".join(raw.replace("\r", " ").replace("\n", " ").split())
    trimmed = collapsed[:limit]
    return {
        "reason": trimmed,
        "provided": bool(raw.strip()),
        "truncated": len(collapsed) > limit,
        "original_length": len(collapsed),
    }


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
        "launcher_kind": Path(cmd[0]).name.lower(),
        "platform": platform.system().lower(),
    }


def _append_rescue_audit(
    *,
    requester: str,
    reason: str | None,
    reason_truncated: bool = False,
    reason_original_length: int | None = None,
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
        "reason_truncated": bool(reason_truncated),
        "reason_original_length": reason_original_length,
        "command": command,
        "pid": pid,
        "log_path": log_path,
        "outcome": outcome,
        "status_state": (status or {}).get("state"),
        "error": error,
    }
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _read_rescue_log(name: str, tail: int = 120) -> dict[str, Any]:
    if not _hashi_root:
        raise ValueError("Hashi root is unavailable")
    root = Path(_hashi_root)
    files = {
        "start": root / "logs" / "remote_rescue_hashi_start.log",
        "audit": root / "logs" / "remote_rescue_audit.jsonl",
        "supervisor": root / "logs" / "hashi-remote-supervisor.log",
    }
    key = str(name or "start").strip().lower()
    if key not in files:
        raise ValueError("log name must be one of: start, audit, supervisor")
    path = files[key]
    try:
        requested_tail = int(tail)
    except (TypeError, ValueError) as exc:
        raise ValueError("tail must be a positive integer") from exc
    if requested_tail <= 0:
        raise ValueError("tail must be a positive integer")
    effective_tail = min(requested_tail, 1000)
    truncated = requested_tail != effective_tail
    if not path.exists():
        return {
            "ok": True,
            "name": key,
            "path": str(path),
            "exists": False,
            "requested_tail": requested_tail,
            "effective_tail": effective_tail,
            "tail_truncated": truncated,
            "lines": [],
        }
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-effective_tail:]
    return {
        "ok": True,
        "name": key,
        "path": str(path),
        "exists": True,
        "requested_tail": requested_tail,
        "effective_tail": effective_tail,
        "tail_truncated": truncated,
        "lines": lines,
    }


def _authenticate_rescue_control(request: Request, *, body_bytes: bytes = b"") -> str:
    client_id, auth_reason = authenticate_request_detailed(
        request,
        body_bytes=body_bytes,
        allow_lan=True,
    )
    if client_id:
        return client_id
    detail = "Not authenticated" if auth_reason == "auth_required" else "Invalid or expired token"
    raise HTTPException(status_code=401, detail=detail)


def _workbench_health_url() -> str:
    return local_http_url(_workbench_port, "/api/health")


def _fetch_workbench_health(timeout: float = 1.0) -> dict[str, Any] | None:
    for host in local_http_hosts():
        req = urllib_request.Request(local_http_url(_workbench_port, "/api/health", host=host), method="GET")
        try:
            with urllib_request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            continue
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
    global _workbench_port, _hashi_root, _protocol_manager, _attachment_store

    _instance_info = instance_info
    _peer_registry = peer_registry
    _pairing_manager = pairing_manager
    _terminal_executor = terminal_executor
    _protocol_manager = protocol_manager
    _workbench_port = workbench_port
    _hashi_root = hashi_root
    _attachment_store = AttachmentStore(
        root=Path(hashi_root) if hashi_root else Path.cwd(),
        instance_id=str(instance_info.get("instance_id") or "hashi"),
    )

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

    def _local_remote_peer_view() -> dict[str, Any]:
        now = time.time()
        instance_id = str(_instance_info.get("instance_id") or "").strip().upper() or "HASHI"
        protocol_status = _protocol_manager.get_protocol_status() if _protocol_manager else {}
        profile_fn = getattr(_protocol_manager, "_local_network_profile", None) if _protocol_manager else None
        local_profile = profile_fn() if callable(profile_fn) else {}
        agents_fn = getattr(_protocol_manager, "get_local_agents_snapshot", None) if _protocol_manager else None
        directory_fn = getattr(_protocol_manager, "get_local_agent_directory_state", None) if _protocol_manager else None
        remote_agents = agents_fn() if callable(agents_fn) else []
        directory = directory_fn() if callable(directory_fn) else {}
        port = int(_instance_info.get("remote_port") or 0)

        display_network_host = ""
        for item in list(local_profile.get("address_candidates") or []):
            if not isinstance(item, dict):
                continue
            host = str(item.get("host") or "").strip()
            scope = str(item.get("scope") or "").strip().lower()
            if host and host not in {"127.0.0.1", "localhost", "0.0.0.0"} and scope in {"lan", "overlay", "routable", "peer"}:
                display_network_host = host
                break

        return {
            "instance_id": instance_id,
            "display_name": str(_instance_info.get("display_name") or instance_id),
            "display_handle": f"@{instance_id.lower()}",
            "host": "127.0.0.1",
            "port": port,
            "workbench_port": int(_instance_info.get("workbench_port") or _workbench_port or 0),
            "platform": str(_instance_info.get("platform") or platform.system().lower()),
            "version": "unknown",
            "hashi_version": str(_instance_info.get("hashi_version") or "unknown"),
            "protocol_version": str(protocol_status.get("protocol_version") or "2.0"),
            "capabilities": list(protocol_status.get("capabilities") or []),
            "properties": {
                "preferred_backend": "self",
                "discovery": "self",
                "live_status": "online",
                "handshake_state": "self",
                "last_handshake_at": now,
                "last_seen_ok": now,
                "directory_state": str(directory.get("directory_state") or "unknown"),
                "agent_snapshot_version": str(directory.get("version") or ""),
                "remote_agents": list(remote_agents or []),
                "address_candidates": list(local_profile.get("address_candidates") or []),
                "observed_candidates": list(local_profile.get("observed_candidates") or []),
                "host_identity": str(local_profile.get("host_identity") or ""),
                "environment_kind": str(local_profile.get("environment_kind") or ""),
            },
            "resolved_route_host": "127.0.0.1",
            "resolved_route_port": port,
            "display_network_host": display_network_host,
            "same_host": True,
            "route_kind": "self",
        }

    def _include_local_remote(peers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        local_id = str(_instance_info.get("instance_id") or "").strip().upper()
        if not local_id:
            return peers
        filtered = [peer for peer in peers if str(peer.get("instance_id") or "").strip().upper() != local_id]
        return [_local_remote_peer_view(), *filtered]

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
        peers = _include_local_remote(peers)
        authenticated = try_authenticate_request(request, allow_loopback=True)
        if not authenticated:
            return {"ok": True, "peers": [], "count": len(peers), "trusted_view": False}
        return {"ok": True, "peers": peers, "count": len(peers)}

    @app.get("/protocol/status")
    async def protocol_status(request: Request):
        if _protocol_manager is None:
            return {"ok": False, "error": "protocol manager unavailable"}
        authenticated = try_authenticate_request(request, allow_loopback=True)
        rescue_start_enabled = bool(_terminal_executor and _terminal_executor.allows_level(AuthLevel.L3_RESTART))
        if not authenticated:
            return {
                "ok": True,
                **_redacted_protocol_status(),
                "rescue_start_enabled": rescue_start_enabled,
                "rescue_start_requirement": "L3_RESTART",
            }
        return {
            "ok": True,
            **_protocol_manager.get_protocol_status(),
            "protocol_auth_mode": protocol_auth_mode(),
            "shared_token_configured": has_shared_token(),
            "lan_mode": is_lan_mode(),
            "rescue_start_enabled": rescue_start_enabled,
            "rescue_start_requirement": "L3_RESTART",
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

    @app.post("/attachments/upload")
    async def attachment_upload(request: Request, payload: AttachmentUploadPayload):
        if _attachment_store is None:
            return JSONResponse(status_code=503, content={"ok": False, "error": "attachment store unavailable"})
        body_bytes = await request.body()
        client_id, auth_reason = authenticate_request_detailed(
            request,
            body_bytes=body_bytes,
            from_instance=payload.from_instance,
            allow_lan=True,
        )
        if not client_id:
            return JSONResponse(
                status_code=401,
                content={"ok": False, "error": "Attachment upload authentication failed", "code": auth_reason},
            )
        try:
            staged = _attachment_store.upload_pending(
                message_id=payload.message_id,
                from_instance=payload.from_instance,
                attachment_id=payload.attachment_id,
                filename=payload.filename,
                mime_type=payload.mime_type,
                content_b64=payload.content_b64,
                sha256=payload.sha256,
            )
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
        logger.info(
            "Attachment staged by %s: message_id=%s attachment_id=%s",
            client_id,
            payload.message_id,
            payload.attachment_id,
        )
        return {"ok": True, "attachment": staged}

    @app.post("/attachments/upload/cancel")
    async def attachment_upload_cancel(request: Request, payload: AttachmentCancelPayload):
        if _attachment_store is None:
            return JSONResponse(status_code=503, content={"ok": False, "error": "attachment store unavailable"})
        body_bytes = await request.body()
        client_id, auth_reason = authenticate_request_detailed(
            request,
            body_bytes=body_bytes,
            from_instance=payload.from_instance,
            allow_lan=True,
        )
        if not client_id:
            return JSONResponse(
                status_code=401,
                content={"ok": False, "error": "Attachment cancel authentication failed", "code": auth_reason},
            )
        try:
            removed = _attachment_store.cancel_pending_uploads(
                message_id=payload.message_id,
                from_instance=payload.from_instance,
                pending_upload_ids=list(payload.pending_upload_ids or []),
            )
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
        logger.info(
            "Attachment pending uploads canceled by %s: message_id=%s removed=%d reason=%s",
            client_id,
            payload.message_id,
            removed,
            str(payload.reason or "").strip() or "unspecified",
        )
        return {"ok": True, "removed": removed}

    @app.post("/protocol/message-with-attachments")
    async def protocol_message_with_attachments(request: Request, payload: ProtocolMessageWithAttachmentsPayload):
        if _protocol_manager is None:
            return JSONResponse(status_code=503, content={"ok": False, "error": "protocol manager unavailable"})
        if _attachment_store is None:
            return JSONResponse(status_code=503, content={"ok": False, "error": "attachment store unavailable"})
        body_bytes = await request.body()
        ok, reason, _auth_identity = verify_protocol_request(
            request,
            body_bytes=body_bytes,
            from_instance=payload.from_instance,
        )
        if not ok:
            logger.warning(
                "Protocol attachment message rejected: reason=%s from=%s",
                reason,
                payload.from_instance,
            )
            return JSONResponse(
                status_code=401,
                content={"ok": False, "error": "Protocol authentication failed", "code": reason},
            )
        try:
            normalized_attachments = _attachment_store.commit_message(
                message_id=payload.message_id,
                from_instance=payload.from_instance,
                attachments=[item.model_dump() for item in payload.attachments],
            )
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

        local_instance = str(_instance_info.get("instance_id") or "").strip().upper()
        if str(payload.to_instance or "").strip().upper() != local_instance:
            candidate_urls = _protocol_manager.resolve_forward_urls(
                str(payload.to_instance or "").strip().upper(),
                "/protocol/message-with-attachments",
            )
            if not candidate_urls:
                return JSONResponse(
                    status_code=404,
                    content={"ok": False, "error": f"Target instance '{payload.to_instance}' not in peer registry"},
                )
            forward_payload = payload.model_dump()
            forward_payload["body"] = _merge_attachment_text(payload.body, normalized_attachments)
            forward_payload["attachments"] = normalized_attachments
            last_exc = None
            for url in candidate_urls:
                try:
                    result = await asyncio.get_running_loop().run_in_executor(
                        None,
                        lambda u=url: _post_json_with_optional_hmac(u, forward_payload, timeout=15),
                    )
                    if _protocol_manager._response_is_error(result):
                        raise RuntimeError(result)
                    return JSONResponse(status_code=202, content=result)
                except Exception as exc:
                    last_exc = exc
                    logger.warning("Attachment forward via %s failed: %s", url, exc)
            return JSONResponse(
                status_code=502,
                content={"ok": False, "error": f"Failed to forward attachment message: {last_exc}"},
            )

        local_payload = ProtocolMessagePayload(
            message_id=payload.message_id,
            conversation_id=payload.conversation_id,
            in_reply_to=payload.in_reply_to,
            from_instance=payload.from_instance,
            from_agent=payload.from_agent,
            to_instance=payload.to_instance,
            to_agent=payload.to_agent,
            body=_merge_attachment_text(payload.body, normalized_attachments),
            hop_count=payload.hop_count,
            ttl=payload.ttl,
            route_trace=payload.route_trace,
            message_type=payload.message_type,
            created_at=payload.created_at,
        )
        status, result = await _protocol_manager.handle_protocol_message(local_payload.model_dump())
        if isinstance(result, dict):
            result = dict(result)
            result["attachments"] = normalized_attachments
        return JSONResponse(status_code=status, content=result)

    @app.get("/attachments/message/{message_id}")
    async def attachment_message_manifest(request: Request, message_id: str):
        if _attachment_store is None:
            return JSONResponse(status_code=503, content={"ok": False, "error": "attachment store unavailable"})
        client_id, auth_reason = authenticate_request_detailed(request, body_bytes=b"", allow_lan=True)
        if not client_id:
            return JSONResponse(
                status_code=401,
                content={"ok": False, "error": "Attachment manifest authentication failed", "code": auth_reason},
            )
        manifest = _attachment_store.get_message_manifest(message_id)
        if manifest is None:
            return JSONResponse(status_code=404, content={"ok": False, "error": "attachment message not found"})
        return {"ok": True, "manifest": manifest}

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
        workbench_url = local_http_url(_workbench_port, "/api/chat")
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
    async def hashi_control_status(request: Request):
        """Report whether the local HASHI core appears reachable."""
        _authenticate_rescue_control(request)
        return _hashi_control_status()

    @app.get("/control/hashi/logs")
    async def hashi_control_logs(
        request: Request,
        name: str = "start",
        tail: int = 120,
    ):
        """Return a bounded tail of fixed HASHI Remote rescue logs."""
        _authenticate_rescue_control(request)
        try:
            return _read_rescue_log(name, tail=tail)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    @app.post("/control/hashi/start")
    async def hashi_control_start(request: Request, payload: HashiStartPayload):
        """
        Start the local HASHI core through a fixed launcher command.

        This is intentionally not a generic shell endpoint. It is gated by
        L3_RESTART because it is a rescue operation that creates a long-lived
        local process.
        """
        body_bytes = await request.body()
        client_id = _authenticate_rescue_control(request, body_bytes=body_bytes)
        if not _terminal_executor or not _terminal_executor.allows_level(AuthLevel.L3_RESTART):
            return JSONResponse(
                status_code=403,
                content={
                    "ok": False,
                    "error": "HASHI start requires max_terminal_level=L3_RESTART",
                },
            )

        reason_meta = _sanitize_rescue_reason(payload.reason)
        current = _hashi_control_status()
        if current["hashi_running"] or current["pid_alive"]:
            _append_rescue_audit(
                requester=client_id,
                reason=reason_meta["reason"],
                reason_truncated=reason_meta["truncated"],
                reason_original_length=reason_meta["original_length"],
                outcome="already_running",
                pid=current.get("pid"),
                status=current,
            )
            return {
                "ok": True,
                "started": False,
                "already_running": True,
                "reason": reason_meta["reason"],
                "reason_truncated": reason_meta["truncated"],
                "status": current,
            }

        try:
            started = _start_hashi_process()
        except Exception as exc:
            logger.exception("HASHI rescue start failed")
            _append_rescue_audit(
                requester=client_id,
                reason=reason_meta["reason"],
                reason_truncated=reason_meta["truncated"],
                reason_original_length=reason_meta["original_length"],
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

        logger.info("HASHI rescue start requested by %s reason=%s pid=%s", client_id, reason_meta["reason"], started["pid"])
        _append_rescue_audit(
            requester=client_id,
            reason=reason_meta["reason"],
            reason_truncated=reason_meta["truncated"],
            reason_original_length=reason_meta["original_length"],
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
            "launcher_kind": started.get("launcher_kind"),
            "platform": started.get("platform"),
            "reason": reason_meta["reason"],
            "reason_truncated": reason_meta["truncated"],
            "status": status,
        }

    # ── File push ────────────────────────────────────────────

    @app.post("/files/push")
    async def file_push(request: Request, payload: FilePushPayload):
        """
        Receive a file from another HASHI instance and atomically write it to
        the requested destination path.
        """
        body_bytes = await request.body()
        client_id, auth_reason = authenticate_request_detailed(request, body_bytes=body_bytes, allow_lan=True)
        if not client_id:
            return JSONResponse(
                status_code=401,
                content={"ok": False, "error": "File transfer authentication failed", "code": auth_reason},
            )
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
    async def file_stat(request: Request, path: str):
        """Return existence, size, and sha256 for a remote path."""
        client_id, auth_reason = authenticate_request_detailed(request, body_bytes=b"", allow_lan=True)
        if not client_id:
            return JSONResponse(
                status_code=401,
                content={"ok": False, "error": "File transfer authentication failed", "code": auth_reason},
            )
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
