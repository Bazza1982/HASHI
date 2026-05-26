from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiohttp import ClientSession, web

from browser_gateway.audit import OLLAuditLogger
from browser_gateway.store import BrowserGatewayStore

logger = logging.getLogger("hashi.oll_gateway")


class RateLimiter:
    def __init__(self, limit: int = 30, window_s: int = 60):
        self.limit = limit
        self.window_s = window_s
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.time()
        bucket = self._events[key]
        while bucket and now - bucket[0] > self.window_s:
            bucket.popleft()
        if len(bucket) >= self.limit:
            return False
        bucket.append(now)
        return True


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
        workbench_url: str = "http://127.0.0.1:18800",
        state_db: Path,
        audit_log: Path,
        public_base_url: str = "",
    ):
        self.project_root = project_root
        self.host = host
        self.port = port
        self.workbench_url = workbench_url.rstrip("/")
        self.public_base_url = public_base_url.strip()
        self.store = BrowserGatewayStore(state_db)
        self.audit = OLLAuditLogger(audit_log)
        self.rate_limiter = RateLimiter()
        self.broker = EventBroker()
        self.runner = None
        self.site = None
        self.upload_root = self.project_root / "state" / "oll_uploads"
        self.upload_root.mkdir(parents=True, exist_ok=True)
        self.app = web.Application(client_max_size=32 * 1024 * 1024)
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
        timeout_s: float,
    ) -> dict[str, Any]:
        body = {
            "agent": agent,
            "text": text,
            "source": source,
            "timeout_s": timeout_s,
        }
        async with ClientSession() as session:
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
        instance_id = str(payload.get("instance_id") or "HASHI1").strip().upper()
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

    async def handle_file_upload(self, request: web.Request) -> web.Response:
        device = await self._require_device(request)
        if isinstance(device, web.Response):
            return device
        if not self.rate_limiter.allow(device["device_id"]):
            return self._json({"ok": False, "error": "rate limit exceeded"}, status=429)

        payload = await request.json()
        thread_id = str(payload.get("thread_id") or "").strip()
        filename = str(payload.get("filename") or "").strip()
        mime_type = str(payload.get("mime_type") or "application/octet-stream").strip()
        ciphertext_b64 = str(payload.get("ciphertext_b64") or "").strip()
        encryption = payload.get("encryption") or {}
        note = str(payload.get("note") or "").strip()
        plaintext_bytes = int(payload.get("plaintext_bytes") or 0)
        notify_agent = bool(payload.get("notify_agent", True))
        timeout_s = max(5.0, min(float(payload.get("timeout_s") or 120.0), 600.0))

        if not thread_id or not filename or not ciphertext_b64:
            return self._json({"ok": False, "error": "thread_id, filename, and ciphertext_b64 are required"}, status=400)

        thread = self.store.get_thread(thread_id, device["device_id"])
        if thread is None:
            return self._json({"ok": False, "error": "thread not found"}, status=404)

        try:
            ciphertext = base64.b64decode(ciphertext_b64, validate=True)
        except Exception:
            return self._json({"ok": False, "error": "ciphertext_b64 is invalid"}, status=400)

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
                timeout_s=timeout_s,
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
        if not self.rate_limiter.allow(device["device_id"]):
            return self._json({"ok": False, "error": "rate limit exceeded"}, status=429)

        payload = await request.json()
        thread_id = str(payload.get("thread_id") or "").strip()
        text = str(payload.get("text") or "").strip()
        timeout_s = max(5.0, min(float(payload.get("timeout_s") or 120.0), 600.0))
        if not thread_id or not text:
            return self._json({"ok": False, "error": "thread_id and text are required"}, status=400)

        thread = self.store.get_thread(thread_id, device["device_id"])
        if thread is None:
            return self._json({"ok": False, "error": "thread not found"}, status=404)

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
            timeout_s=timeout_s,
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
        return self._json({"ok": True, "device": data})
