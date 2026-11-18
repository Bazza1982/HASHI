from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import re
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiohttp import ClientSession, ClientTimeout, web

from browser_gateway.audit import OLLAuditLogger
from browser_gateway.store import BrowserGatewayStore
from browser_gateway.upload_sessions import UploadBusy, UploadSessions
from orchestrator.pathing import resolve_instance_id
from orchestrator.runtime_defaults import DEFAULT_WORKBENCH_URL

logger = logging.getLogger("hashi.oll_gateway")


class RateLimiter:
    """Optional per-device rolling budget; zero means no selected rate limit."""

    def __init__(self, limit: int = 0, window_s: int = 60, *, per_device=None, clock=None):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("rate limit must be a non-negative integer")
        if isinstance(window_s, bool) or not isinstance(window_s, int) or window_s <= 0:
            raise ValueError("rate window must be a positive integer")
        self.limit = limit
        self.window_s = window_s
        self._clock = clock or time.monotonic
        self._per_device = dict(per_device or {})
        if any(not isinstance(key, str) or not key.strip()
               or isinstance(value, bool) or not isinstance(value, int) or value < 0
               for key, value in self._per_device.items()):
            raise ValueError("per-device rate limits must be non-negative integers")
        self._events: dict[str, deque[float]] = {}

    def limit_for(self, key: str) -> int:
        return self._per_device.get(key, self.limit)

    def admit(self, key: str) -> int | None:
        """Return retry seconds on denial, or None after admission."""
        limit = self.limit_for(key)
        if limit == 0:
            return None
        now = self._clock()
        # Drop expired buckets so retained accounting scales with active devices.
        for device, events in list(self._events.items()):
            while events and now - events[0] >= self.window_s:
                events.popleft()
            if not events:
                self._events.pop(device, None)
        bucket = self._events.setdefault(key, deque())
        if len(bucket) >= limit:
            return max(1, math.ceil(self.window_s - (now - bucket[0])))
        bucket.append(now)
        return None


class EventBroker:
    def __init__(self):
        self._queues: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, thread_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._queues[thread_id].add(queue)
        return queue

    def unsubscribe(self, thread_id: str, queue: asyncio.Queue) -> None:
        queues = self._queues.get(thread_id)
        if not queues:
            return
        queues.discard(queue)
        if not queues:
            self._queues.pop(thread_id, None)

    async def publish(self, thread_id: str, event: dict[str, Any]) -> None:
        for queue in list(self._queues.get(thread_id, set())):
            await queue.put(event)


class BrowserGatewayServer:
    def __init__(
        self,
        *,
        project_root: Path,
        host: str = "127.0.0.1",
        port: int = 8876,
        workbench_url: str = DEFAULT_WORKBENCH_URL,
        state_db: Path,
        audit_log: Path,
        public_base_url: str = "",
        chat_requests_per_minute: int = 0,
        upload_requests_per_minute: int = 0,
        device_rate_limits: dict[str, dict[str, int]] | None = None,
        json_body_bytes: int = 32 * 1024 * 1024,
        upload_chunk_bytes: int = 4 * 1024 * 1024,
        upload_max_bytes: int = 0,
    ):
        self.project_root = project_root
        self.instance_id = resolve_instance_id(project_root / "agents.json").upper()
        self.host = host
        self.port = port
        self.workbench_url = workbench_url.rstrip("/")
        self.public_base_url = public_base_url.strip()
        self.store = BrowserGatewayStore(state_db)
        self.audit = OLLAuditLogger(audit_log)
        overrides = {} if device_rate_limits is None else device_rate_limits
        if not isinstance(overrides, dict) or any(
            not isinstance(key, str) or not isinstance(value, dict)
            or bool(set(value) - {"chat", "upload"})
            for key, value in overrides.items()
        ):
            raise ValueError("device_rate_limits must map device ids to chat/upload limits")
        self.chat_rate_limiter = RateLimiter(
            chat_requests_per_minute,
            per_device={key: value["chat"] for key, value in overrides.items() if "chat" in value},
        )
        self.upload_rate_limiter = RateLimiter(
            upload_requests_per_minute,
            per_device={key: value["upload"] for key, value in overrides.items() if "upload" in value},
        )
        self.broker = EventBroker()
        self.runner = None
        self.site = None
        self.upload_root = self.project_root / "state" / "oll_uploads"
        self.upload_root.mkdir(parents=True, exist_ok=True)
        for name, value, minimum in (
            ("json_body_bytes", json_body_bytes, 1),
            ("upload_chunk_bytes", upload_chunk_bytes, 1),
            ("upload_max_bytes", upload_max_bytes, 0),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        self.upload_chunk_bytes = upload_chunk_bytes
        self.upload_max_bytes = upload_max_bytes
        self.upload_sessions = UploadSessions(self.store, self.project_root)
        self.app = web.Application(client_max_size=json_body_bytes)
        self.app.router.add_get("/browser/health", self.handle_health)
        self.app.router.add_post("/browser/pair/request", self.handle_pair_request)
        self.app.router.add_post("/browser/pair/complete", self.handle_pair_complete)
        self.app.router.add_post("/browser/auth/refresh", self.handle_auth_refresh)
        self.app.router.add_post("/browser/auth/logout", self.handle_auth_logout)
        self.app.router.add_post("/browser/device/recovery/set", self.handle_recovery_set)
        self.app.router.add_post("/browser/device/recovery/restore", self.handle_recovery_restore)
        self.app.router.add_get("/browser/agents", self.handle_agents)
        self.app.router.add_get("/browser/threads", self.handle_threads)
        self.app.router.add_post("/browser/thread/create", self.handle_thread_create)
        self.app.router.add_get("/browser/thread/{thread_id}", self.handle_thread_get)
        self.app.router.add_get("/browser/thread/{thread_id}/attachments", self.handle_thread_attachments)
        self.app.router.add_post("/browser/file/upload", self.handle_file_upload)
        self.app.router.add_post("/browser/file/upload/start", self.handle_upload_start)
        self.app.router.add_get("/browser/file/upload/{upload_id}", self.handle_upload_status)
        self.app.router.add_put("/browser/file/upload/{upload_id}", self.handle_upload_chunk)
        self.app.router.add_delete("/browser/file/upload/{upload_id}", self.handle_upload_cancel)
        self.app.router.add_post("/browser/file/upload/{upload_id}/finish", self.handle_upload_finish)
        self.app.router.add_post("/browser/chat/send", self.handle_chat_send)
        self.app.router.add_get("/browser/chat/stream/{thread_id}", self.handle_chat_stream)
        self.app.router.add_get("/browser/device/status", self.handle_device_status)

    async def start(self) -> None:
        self.audit.log("gateway_start", host=self.host, port=self.port, workbench_url=self.workbench_url)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()

    async def stop(self) -> None:
        self.audit.log("gateway_stop", host=self.host, port=self.port)
        if self.runner:
            await self.runner.cleanup()

    def _json(self, payload: dict[str, Any], status: int = 200) -> web.Response:
        return web.json_response(payload, status=status)

    def _authorized_device(self, request: web.Request) -> dict[str, Any] | None:
        auth = request.headers.get("Authorization", "").strip()
        if not auth.startswith("Bearer "):
            return None
        token = auth.removeprefix("Bearer ").strip()
        if not token:
            return None
        return self.store.authenticate(token)

    async def _require_device(self, request: web.Request) -> dict[str, Any] | web.Response:
        device = self._authorized_device(request)
        if device is None:
            return self._json({"ok": False, "error": "unauthorized"}, status=401)
        return device

    def _rate_limit_response(self, limiter: RateLimiter, device_id: str) -> web.Response | None:
        wait = limiter.admit(device_id)
        if wait is None:
            return None
        return web.json_response(
            {"ok": False, "error": "rate limit exceeded", "retry_after_seconds": wait},
            status=429,
            headers={"Retry-After": str(wait)},
        )

    async def _fetch_workbench_agents(self) -> list[dict[str, Any]]:
        async with ClientSession() as session:
            async with session.get(f"{self.workbench_url}/api/agents") as resp:
                payload = await resp.json()
                return payload.get("agents", [])

    async def _send_to_workbench(
        self,
        *,
        agent: str,
        text: str,
        source: str,
    ) -> dict[str, Any]:
        body = {
            "agent": agent,
            "text": text,
            "source": source,
        }
        timeout = ClientTimeout(total=None, connect=10.0, sock_connect=10.0)
        async with ClientSession(timeout=timeout) as session:
            async with session.post(f"{self.workbench_url}/api/browser/chat/send", json=body) as resp:
                try:
                    payload = await resp.json()
                except Exception:
                    text_body = await resp.text()
                    payload = {
                        "ok": False,
                        "error": f"workbench browser endpoint unavailable: HTTP {resp.status}",
                        "detail": text_body[:300],
                    }
                if resp.status >= 400:
                    payload.setdefault("ok", False)
                return payload

    async def handle_health(self, request: web.Request) -> web.Response:
        return self._json(
            {
                "ok": True,
                "service": "oll-browser-gateway",
                "host": self.host,
                "port": self.port,
                "workbench_url": self.workbench_url,
                "public_base_url": self.public_base_url,
            }
        )

    async def handle_pair_request(self, request: web.Request) -> web.Response:
        payload = await request.json()
        device_label = str(payload.get("device_label") or "OLL Browser").strip()
        pair = self.store.create_pair_request(device_label)
        self.audit.log("pair_request_created", device_id=pair.device_id, device_label=device_label)
        return self._json(
            {
                "ok": True,
                "device_id": pair.device_id,
                "pairing_code": pair.pairing_code,
                "expires_at": pair.expires_at,
            }
        )

    async def handle_pair_complete(self, request: web.Request) -> web.Response:
        payload = await request.json()
        device_id = str(payload.get("device_id") or "").strip()
        pairing_code = str(payload.get("pairing_code") or "").strip()
        result = self.store.complete_pair(device_id, pairing_code)
        if result is None:
            return self._json({"ok": False, "error": "invalid or expired pairing code"}, status=400)
        self.audit.log("pair_completed", device_id=device_id)
        return self._json({"ok": True, **result})

    async def handle_auth_refresh(self, request: web.Request) -> web.Response:
        device = await self._require_device(request)
        if isinstance(device, web.Response):
            return device
        refreshed = self.store.refresh_token(device["device_id"])
        if refreshed is None:
            return self._json({"ok": False, "error": "refresh failed"}, status=400)
        self.audit.log("token_refreshed", device_id=device["device_id"])
        return self._json({"ok": True, **refreshed})

    async def handle_auth_logout(self, request: web.Request) -> web.Response:
        device = await self._require_device(request)
        if isinstance(device, web.Response):
            return device
        self.store.revoke_token(device["device_id"])
        self.audit.log("token_revoked", device_id=device["device_id"])
        return self._json({"ok": True})

    async def handle_recovery_set(self, request: web.Request) -> web.Response:
        device = await self._require_device(request)
        if isinstance(device, web.Response):
            return device
        payload = await request.json()
        recovery_code_hash = str(payload.get("recovery_code_hash") or "").strip().lower()
        recovery_payload = payload.get("recovery_payload")
        if len(recovery_code_hash) < 32 or not isinstance(recovery_payload, dict):
            return self._json({"ok": False, "error": "recovery_code_hash and recovery_payload are required"}, status=400)
        ok = self.store.set_device_recovery(
            device["device_id"],
            recovery_code_hash=recovery_code_hash,
            recovery_payload_json=json.dumps(recovery_payload, ensure_ascii=False),
        )
        if not ok:
            return self._json({"ok": False, "error": "failed to store recovery payload"}, status=400)
        self.audit.log("recovery_backup_set", device_id=device["device_id"])
        return self._json({"ok": True, "device_id": device["device_id"]})

    async def handle_recovery_restore(self, request: web.Request) -> web.Response:
        payload = await request.json()
        device_id = str(payload.get("device_id") or "").strip()
        recovery_code_hash = str(payload.get("recovery_code_hash") or "").strip().lower()
        if not device_id or len(recovery_code_hash) < 32:
            return self._json({"ok": False, "error": "device_id and recovery_code_hash are required"}, status=400)
        recovery = self.store.get_device_recovery(device_id, recovery_code_hash)
        if recovery is None:
            return self._json({"ok": False, "error": "recovery payload not found"}, status=404)
        self.audit.log("recovery_restore_requested", device_id=device_id)
        return self._json(
            {
                "ok": True,
                "device_id": device_id,
                "device_label": recovery.get("device_label"),
                "recovery_payload": json.loads(recovery.get("recovery_payload_json") or "{}"),
                "recovery_updated_at": recovery.get("recovery_updated_at") or "",
            }
        )

    async def handle_agents(self, request: web.Request) -> web.Response:
        device = await self._require_device(request)
        if isinstance(device, web.Response):
            return device
        agents = await self._fetch_workbench_agents()
        self.audit.log("agents_listed", device_id=device["device_id"], count=len(agents))
        return self._json({"ok": True, "agents": agents})

    async def handle_threads(self, request: web.Request) -> web.Response:
        device = await self._require_device(request)
        if isinstance(device, web.Response):
            return device
        threads = self.store.list_threads(device["device_id"])
        return self._json({"ok": True, "threads": threads})

    async def handle_thread_create(self, request: web.Request) -> web.Response:
        device = await self._require_device(request)
        if isinstance(device, web.Response):
            return device
        payload = await request.json()
        agent_id = str(payload.get("agent_id") or "").strip()
        if not agent_id:
            return self._json({"ok": False, "error": "agent_id is required"}, status=400)
        title = str(payload.get("title") or "").strip()
        instance_id = str(payload.get("instance_id") or self.instance_id).strip().upper()
        thread = self.store.create_thread(device["device_id"], agent_id=agent_id, title=title, instance_id=instance_id)
        self.audit.log("thread_created", device_id=device["device_id"], thread_id=thread["thread_id"], agent_id=agent_id, instance_id=instance_id)
        return self._json({"ok": True, "thread": thread})

    async def handle_thread_get(self, request: web.Request) -> web.Response:
        device = await self._require_device(request)
        if isinstance(device, web.Response):
            return device
        thread_id = request.match_info["thread_id"]
        thread = self.store.get_thread(thread_id, device["device_id"])
        if thread is None:
            return self._json({"ok": False, "error": "thread not found"}, status=404)
        return self._json({"ok": True, "thread": thread})

    async def handle_thread_attachments(self, request: web.Request) -> web.Response:
        device = await self._require_device(request)
        if isinstance(device, web.Response):
            return device
        thread_id = request.match_info["thread_id"]
        thread = self.store.get_thread(thread_id, device["device_id"])
        if thread is None:
            return self._json({"ok": False, "error": "thread not found"}, status=404)
        attachments = self.store.list_attachments(thread_id, device["device_id"])
        return self._json({"ok": True, "attachments": attachments})

    async def handle_upload_start(self, request: web.Request) -> web.Response:
        device = await self._require_device(request)
        if isinstance(device, web.Response):
            return device
        payload = await request.json()
        if not isinstance(payload, dict):
            return self._json({"ok": False, "error": "upload metadata must be an object"}, status=400)
        thread_id = str(payload.get("thread_id") or "").strip()
        filename = str(payload.get("filename") or "").strip()
        digest = str(payload.get("ciphertext_sha256") or "").strip().lower()
        size = payload.get("ciphertext_bytes")
        plaintext_size = payload.get("plaintext_bytes", 0)
        encryption = payload.get("encryption", {})
        notify = payload.get("notify_agent", True)
        if (not thread_id or not filename or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or isinstance(size, bool) or not isinstance(size, int) or not 0 <= size < 2**63
                or isinstance(plaintext_size, bool) or not isinstance(plaintext_size, int)
                or not 0 <= plaintext_size < 2**63 or not isinstance(encryption, dict)
                or not isinstance(notify, bool)):
            return self._json({"ok": False, "error": "invalid upload metadata"}, status=400)
        if self.upload_max_bytes and size > self.upload_max_bytes:
            return self._json({"ok": False, "error": "selected upload limit exceeded",
                               "upload_max_bytes": self.upload_max_bytes}, status=413)
        if self.store.get_thread(thread_id, device["device_id"]) is None:
            return self._json({"ok": False, "error": "thread not found"}, status=404)
        rate_response = self._rate_limit_response(self.upload_rate_limiter, device["device_id"])
        if rate_response is not None:
            return rate_response
        metadata = {
            "filename": filename,
            "mime_type": str(payload.get("mime_type") or "application/octet-stream"),
            "plaintext_bytes": plaintext_size,
            "encryption": encryption,
            "note": str(payload.get("note") or ""),
            "notify_agent": notify,
        }
        row = self.upload_sessions.create(
            device_id=device["device_id"], thread_id=thread_id, metadata=metadata,
            ciphertext_bytes=size, ciphertext_sha256=digest,
        )
        return self._json({"ok": True, "upload_id": row["upload_id"],
                           "offset": 0, "chunk_bytes": self.upload_chunk_bytes})

    async def _get_upload(self, request: web.Request):
        device = await self._require_device(request)
        if isinstance(device, web.Response):
            return device, None
        row = self.upload_sessions.get(request.match_info["upload_id"], device["device_id"])
        if row is None or self.store.get_thread(row["thread_id"], device["device_id"]) is None:
            return self._json({"ok": False, "error": "upload not found"}, status=404), None
        return device, row

    async def handle_upload_status(self, request: web.Request) -> web.Response:
        device, row = await self._get_upload(request)
        if row is None:
            return device
        return self._json({
            "ok": True, "upload_id": row["upload_id"], "status": row["status"],
            "offset": row["received_bytes"], "ciphertext_bytes": row["ciphertext_bytes"],
            "chunk_bytes": self.upload_chunk_bytes,
            "notification_status": (
                "sending_or_unknown" if row["notification_status"] == "sending"
                else row["notification_status"]
            ),
            "attachment_id": row["attachment_id"] if row["status"] == "completed" else None,
        })

    async def handle_upload_chunk(self, request: web.Request) -> web.Response:
        device, row = await self._get_upload(request)
        if row is None:
            return device
        try:
            offset = int(request.headers.get("Upload-Offset", ""))
        except ValueError:
            return self._json({"ok": False, "error": "valid Upload-Offset is required"}, status=400)
        size = request.content_length
        if size is None or size <= 0:
            return self._json({"ok": False, "error": "positive Content-Length is required"}, status=400)
        if size > self.upload_chunk_bytes:
            return self._json({"ok": False, "error": "chunk exceeds selected limit",
                               "chunk_bytes": self.upload_chunk_bytes}, status=413)
        try:
            with self.upload_sessions.exclusive(row):
                row = self.upload_sessions.get(row["upload_id"], device["device_id"])
                if row is None:
                    return self._json({"ok": False, "error": "upload not found"}, status=404)
                if row["status"] != "open" or self.upload_sessions.final_path(row).exists():
                    return self._json({"ok": False, "error": "upload is not open for chunks"}, status=409)
                if offset != row["received_bytes"] or offset + size > row["ciphertext_bytes"]:
                    return self._json({"ok": False, "error": "upload offset mismatch",
                                       "offset": row["received_bytes"]}, status=409)
                path = self.upload_sessions.partial_path(row)
                if not path.is_file() or path.stat().st_size < offset:
                    return self._json({"ok": False, "error": "staged upload is incomplete"}, status=409)
                with path.open("r+b") as stream:
                    stream.truncate(offset)
                    stream.seek(offset)
                    written = 0
                    try:
                        async for chunk in request.content.iter_chunked(64 * 1024):
                            written += len(chunk)
                            if written > size:
                                raise ValueError("chunk exceeded declared length")
                            stream.write(chunk)
                        if written != size:
                            raise ValueError("chunk was incomplete")
                        stream.flush()
                        os.fsync(stream.fileno())
                        if not self.upload_sessions.advance(row, offset + written):
                            raise ValueError("upload offset changed")
                    except BaseException:
                        current = self.upload_sessions.get(row["upload_id"], device["device_id"])
                        if current is not None and current["received_bytes"] == offset:
                            stream.truncate(offset)
                            stream.flush()
                            os.fsync(stream.fileno())
                        raise
                return self._json({"ok": True, "upload_id": row["upload_id"],
                                   "offset": offset + written})
        except UploadBusy:
            return self._json({"ok": False, "error": "upload is busy; retry"}, status=409)
        except ValueError as exc:
            return self._json({"ok": False, "error": str(exc)}, status=409)

    async def handle_upload_cancel(self, request: web.Request) -> web.Response:
        device, row = await self._get_upload(request)
        if row is None:
            return device
        try:
            with self.upload_sessions.exclusive(row):
                if not self.upload_sessions.cancel(row):
                    return self._json({"ok": False, "error": "completed upload cannot be cancelled"}, status=409)
        except UploadBusy:
            return self._json({"ok": False, "error": "upload is busy; retry"}, status=409)
        return self._json({"ok": True, "cancelled": True})

    @staticmethod
    def _upload_digest(path: Path) -> str:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    async def handle_upload_finish(self, request: web.Request) -> web.Response:
        device, row = await self._get_upload(request)
        if row is None:
            return device
        try:
            with self.upload_sessions.exclusive(row):
                row = self.upload_sessions.get(row["upload_id"], device["device_id"])
                if row is None:
                    return self._json({"ok": False, "error": "upload not found"}, status=404)
                final_path = self.upload_sessions.final_path(row)
                partial_path = self.upload_sessions.partial_path(row)
                if row["received_bytes"] != row["ciphertext_bytes"]:
                    return self._json({"ok": False, "error": "upload is incomplete",
                                       "offset": row["received_bytes"]}, status=409)
                source = final_path if final_path.is_file() else partial_path
                if not source.is_file() or source.stat().st_size != row["ciphertext_bytes"]:
                    return self._json({"ok": False, "error": "staged upload size mismatch"}, status=409)
                if await asyncio.to_thread(self._upload_digest, source) != row["ciphertext_sha256"]:
                    return self._json({"ok": False, "error": "ciphertext hash mismatch"}, status=409)
                newly_completed = row["status"] == "open"
                if source == partial_path:
                    final_path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(partial_path, final_path)
                row = self.upload_sessions.finish(row)
                attachment = self.store.get_attachment(row["attachment_id"], device["device_id"])
                if attachment is None:
                    return self._json({"ok": False, "error": "attachment record unavailable"}, status=409)
                metadata = json.loads(row["metadata_json"])
                attachment["encryption"] = metadata["encryption"]
                if newly_completed:
                    self.audit.log("file_uploaded", device_id=device["device_id"],
                                   thread_id=row["thread_id"], attachment_id=row["attachment_id"],
                                   ciphertext_bytes=row["ciphertext_bytes"],
                                   plaintext_bytes=metadata["plaintext_bytes"])
                    await self.broker.publish(row["thread_id"], {
                        "type": "attachment_uploaded", "thread_id": row["thread_id"],
                        "attachment": attachment,
                    })
                if self.upload_sessions.claim_notification(row):
                    await self._notify_uploaded_attachment(row, attachment, metadata)
                    row = self.upload_sessions.get(row["upload_id"], device["device_id"])
                elif row["notification_status"] == "sending":
                    # Acquiring the upload lock proves no prior sender still holds
                    # this session. Its delivery result is unknown, not retried.
                    self.upload_sessions.finish_notification(
                        row, "unknown", {"ok": False, "error": "notification outcome unknown after interruption"}
                    )
                    row = self.upload_sessions.get(row["upload_id"], device["device_id"])
                result = (json.loads(row["notification_result_json"])
                          if row.get("notification_result_json") else None)
                return self._json({"ok": True, "attachment": attachment,
                                   "notification_status": row["notification_status"],
                                   "agent_result": result})
        except UploadBusy:
            return self._json({"ok": False, "error": "upload is busy; retry"}, status=409)

    async def _notify_uploaded_attachment(self, row: dict, attachment: dict, metadata: dict) -> None:
        thread = self.store.get_thread(row["thread_id"], row["device_id"])
        try:
            if thread is None:
                raise ValueError("thread no longer exists")
            reference = (
                "[Browser attachment]\n"
                f"attachment_id: {attachment['attachment_id']}\n"
                f"filename: {metadata['filename']}\n"
                f"mime_type: {metadata['mime_type']}\n"
                f"plaintext_bytes: {metadata['plaintext_bytes']}\n"
                f"ciphertext_bytes: {row['ciphertext_bytes']}\n"
                "encrypted: true\n"
                f"thread_id: {row['thread_id']}\n"
                f"note: {metadata['note'] or '(none)'}\n"
                "The file payload is stored in the HASHI Browser Gateway attachment registry."
            )
            message_id = self.store.append_message(row["thread_id"], "user", "queued",
                                                   text_preview=f"[attachment] {metadata['filename']}")
            source = f"browser-upload:{row['device_id']}:{row['thread_id']}:{row['attachment_id']}:{message_id}"
            result = await self._send_to_workbench(agent=thread["agent_id"], text=reference, source=source)
            self.store.complete_message(message_id, "completed" if result.get("ok") else "failed",
                                        text_preview=f"[attachment] {metadata['filename']}")
            self.store.append_message(row["thread_id"], "assistant",
                                      "completed" if result.get("ok") else "failed",
                                      text_preview=str(result.get("text") or result.get("error") or ""),
                                      source_tag=source)
            self.store.set_thread_checkpoint(row["thread_id"], str(result.get("request_id") or ""))
            await self.broker.publish(row["thread_id"], {
                "type": "reply_ready" if result.get("ok") else "reply_error",
                "thread_id": row["thread_id"], "request_id": result.get("request_id"),
                "attachment_id": row["attachment_id"], "text": result.get("text"),
                "error": result.get("error"),
            })
            self.upload_sessions.finish_notification(row, "sent" if result.get("ok") else "failed", result)
        except Exception as exc:
            self.upload_sessions.finish_notification(
                row, "unknown", {"ok": False, "error": f"notification outcome unknown: {type(exc).__name__}"}
            )

    async def handle_file_upload(self, request: web.Request) -> web.Response:
        device = await self._require_device(request)
        if isinstance(device, web.Response):
            return device
        payload = await request.json()
        thread_id = str(payload.get("thread_id") or "").strip()
        filename = str(payload.get("filename") or "").strip()
        mime_type = str(payload.get("mime_type") or "application/octet-stream").strip()
        ciphertext_b64 = str(payload.get("ciphertext_b64") or "").strip()
        encryption = payload.get("encryption") or {}
        note = str(payload.get("note") or "").strip()
        plaintext_bytes = int(payload.get("plaintext_bytes") or 0)
        notify_agent = bool(payload.get("notify_agent", True))

        if not thread_id or not filename or not ciphertext_b64:
            return self._json({"ok": False, "error": "thread_id, filename, and ciphertext_b64 are required"}, status=400)

        thread = self.store.get_thread(thread_id, device["device_id"])
        if thread is None:
            return self._json({"ok": False, "error": "thread not found"}, status=404)

        try:
            ciphertext = base64.b64decode(ciphertext_b64, validate=True)
        except Exception:
            return self._json({"ok": False, "error": "ciphertext_b64 is invalid"}, status=400)

        rate_response = self._rate_limit_response(self.upload_rate_limiter, device["device_id"])
        if rate_response is not None:
            return rate_response

        attachment_id = f"att-{uuid4().hex[:12]}"
        date_prefix = time.strftime("%Y%m%d")
        storage_relpath = f"oll_uploads/{date_prefix}/{attachment_id}.bin"
        storage_path = self.project_root / "state" / storage_relpath
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path.write_bytes(ciphertext)

        attachment = self.store.create_attachment(
            attachment_id=attachment_id,
            thread_id=thread_id,
            device_id=device["device_id"],
            filename=filename,
            mime_type=mime_type,
            plaintext_bytes=plaintext_bytes,
            ciphertext_bytes=len(ciphertext),
            storage_relpath=storage_relpath,
            encryption_json=json.dumps(encryption, ensure_ascii=False),
            note=note,
        )
        attachment["encryption"] = encryption
        self.audit.log(
            "file_uploaded",
            device_id=device["device_id"],
            thread_id=thread_id,
            attachment_id=attachment["attachment_id"],
            agent_id=thread["agent_id"],
            instance_id=thread["instance_id"],
            filename=filename,
            mime_type=mime_type,
            ciphertext_bytes=len(ciphertext),
            plaintext_bytes=plaintext_bytes,
        )
        await self.broker.publish(
            thread_id,
            {
                "type": "attachment_uploaded",
                "thread_id": thread_id,
                "attachment": attachment,
            },
        )

        result: dict[str, Any] | None = None
        if notify_agent:
            reference_text = (
                "[Browser attachment]\n"
                f"attachment_id: {attachment['attachment_id']}\n"
                f"filename: {filename}\n"
                f"mime_type: {mime_type}\n"
                f"plaintext_bytes: {plaintext_bytes}\n"
                f"ciphertext_bytes: {len(ciphertext)}\n"
                "encrypted: true\n"
                f"thread_id: {thread_id}\n"
                f"note: {note or '(none)'}\n"
                "The file payload is stored in the HASHI Browser Gateway attachment registry."
            )
            user_message_id = self.store.append_message(
                thread_id,
                "user",
                "queued",
                text_preview=f"[attachment] {filename}",
            )
            source_tag = f"browser-upload:{device['device_id']}:{thread_id}:{attachment['attachment_id']}:{user_message_id}"
            result = await self._send_to_workbench(
                agent=thread["agent_id"],
                text=reference_text,
                source=source_tag,
            )
            self.store.complete_message(
                user_message_id,
                "completed" if result.get("ok") else "failed",
                text_preview=f"[attachment] {filename}",
            )
            self.store.append_message(
                thread_id,
                "assistant",
                "completed" if result.get("ok") else "failed",
                text_preview=str(result.get("text") or result.get("error") or ""),
                source_tag=source_tag,
            )
            self.store.set_thread_checkpoint(thread_id, str(result.get("request_id") or ""))
            await self.broker.publish(
                thread_id,
                {
                    "type": "reply_ready" if result.get("ok") else "reply_error",
                    "thread_id": thread_id,
                    "request_id": result.get("request_id"),
                    "attachment_id": attachment["attachment_id"],
                    "text": result.get("text"),
                    "error": result.get("error"),
                },
            )

        response_payload = {"ok": True, "attachment": attachment}
        if result is not None:
            response_payload["agent_result"] = result
            response_payload["ok"] = bool(result.get("ok"))
        return self._json(response_payload, status=200 if response_payload["ok"] else 502)

    async def handle_chat_send(self, request: web.Request) -> web.Response:
        device = await self._require_device(request)
        if isinstance(device, web.Response):
            return device
        payload = await request.json()
        thread_id = str(payload.get("thread_id") or "").strip()
        text = str(payload.get("text") or "").strip()
        if not thread_id or not text:
            return self._json({"ok": False, "error": "thread_id and text are required"}, status=400)

        thread = self.store.get_thread(thread_id, device["device_id"])
        if thread is None:
            return self._json({"ok": False, "error": "thread not found"}, status=404)

        rate_response = self._rate_limit_response(self.chat_rate_limiter, device["device_id"])
        if rate_response is not None:
            return rate_response

        user_message_id = self.store.append_message(thread_id, "user", "queued", text_preview=text)
        source_tag = f"browser:{device['device_id']}:{thread_id}:{user_message_id}"
        self.audit.log(
            "chat_send",
            device_id=device["device_id"],
            thread_id=thread_id,
            agent_id=thread["agent_id"],
            instance_id=thread["instance_id"],
            request_id=user_message_id,
            bytes_in=len(text.encode("utf-8")),
        )

        result = await self._send_to_workbench(
            agent=thread["agent_id"],
            text=text,
            source=source_tag,
        )
        assistant_message_id = self.store.append_message(
            thread_id,
            "assistant",
            "completed" if result.get("ok") else "failed",
            text_preview=str(result.get("text") or result.get("error") or ""),
            source_tag=source_tag,
        )
        self.store.complete_message(
            user_message_id,
            "completed" if result.get("ok") else "failed",
            text_preview=text,
        )
        self.store.set_thread_checkpoint(thread_id, str(result.get("request_id") or ""))

        event = {
            "type": "reply_ready" if result.get("ok") else "reply_error",
            "thread_id": thread_id,
            "request_id": result.get("request_id"),
            "message_id": assistant_message_id,
            "text": result.get("text"),
            "error": result.get("error"),
        }
        await self.broker.publish(thread_id, event)
        self.audit.log(
            "chat_result",
            device_id=device["device_id"],
            thread_id=thread_id,
            agent_id=thread["agent_id"],
            instance_id=thread["instance_id"],
            request_id=result.get("request_id"),
            status="ok" if result.get("ok") else "failed",
            bytes_out=len(str(result.get("text") or "").encode("utf-8")),
        )
        return self._json({"ok": bool(result.get("ok")), "thread_id": thread_id, **result}, status=200 if result.get("ok") else 502)

    async def handle_chat_stream(self, request: web.Request) -> web.StreamResponse:
        device = await self._require_device(request)
        if isinstance(device, web.Response):
            return device
        thread_id = request.match_info["thread_id"]
        thread = self.store.get_thread(thread_id, device["device_id"])
        if thread is None:
            return self._json({"ok": False, "error": "thread not found"}, status=404)

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)

        queue = self.broker.subscribe(thread_id)
        try:
            # Send an initial padded comment so proxies flush the SSE stream promptly.
            await resp.write(f": connected\n:{' ' * 2048}\n\n".encode("utf-8"))
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    body = json.dumps(event, ensure_ascii=False)
                    await resp.write(f"event: {event['type']}\ndata: {body}\n\n".encode("utf-8"))
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.broker.unsubscribe(thread_id, queue)
        return resp

    async def handle_device_status(self, request: web.Request) -> web.Response:
        device = await self._require_device(request)
        if isinstance(device, web.Response):
            return device
        data = self.store.device_status(device["device_id"])
        return self._json({
            "ok": True,
            "device": data,
            "ingress_limits": {
                "window_seconds": self.chat_rate_limiter.window_s,
                "chat_requests": self.chat_rate_limiter.limit_for(device["device_id"]) or None,
                "upload_requests": self.upload_rate_limiter.limit_for(device["device_id"]) or None,
                "json_body_bytes": self.app._client_max_size,
                "upload_chunk_bytes": self.upload_chunk_bytes,
                "upload_max_bytes": self.upload_max_bytes or None,
            },
        })
