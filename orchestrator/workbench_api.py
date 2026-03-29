from __future__ import annotations
import asyncio
import json
import mimetypes
import time
from pathlib import Path
from uuid import uuid4

from aiohttp import web

from orchestrator.admin_local_testing import execute_local_command, supported_commands
from orchestrator.conversation_router import ConversationRouter
from orchestrator.pathing import resolve_path_value
from orchestrator.transfer_store import TransferStore


def _read_jsonl_recent(file_path: Path, limit: int = 50) -> dict:
    if not file_path.exists():
        return {"messages": [], "offset": 0}

    text = file_path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    records = []
    for line in lines:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("role") not in {"user", "assistant", "thinking"} or not obj.get("text"):
            continue
        records.append(obj)

    return {
        "messages": records[-limit:],
        "offset": len(text.encode("utf-8")),
    }


def _read_jsonl_increment(file_path: Path, offset: int = 0) -> dict:
    if not file_path.exists():
        return {"messages": [], "offset": 0}

    size = file_path.stat().st_size
    safe_offset = offset if 0 <= offset <= size else 0
    with open(file_path, "rb") as f:
        f.seek(safe_offset)
        chunk = f.read()

    messages = []
    for line in chunk.decode("utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("role") not in {"user", "assistant", "thinking"} or not obj.get("text"):
            continue
        messages.append(obj)

    return {"messages": messages, "offset": size}


class WorkbenchApiServer:
    TRANSFER_ACCEPT_PREFIX = "TRANSFER_ACCEPTED "
    FORK_ACCEPT_PREFIX = "FORK_ACCEPTED "

    def __init__(self, config_path: Path, global_config, runtimes: list | None = None, secrets: dict | None = None, orchestrator=None):
        self.config_path = config_path
        self.global_config = global_config
        self.runtimes = runtimes or []
        self.orchestrator = orchestrator
        self.admin_token = ((secrets or {}).get("workbench_admin_token") or "").strip()
        self.bridge_router = ConversationRouter(
            config_path=self.config_path,
            capabilities_path=self.config_path.parent / "agent_capabilities.json",
            store_path=self.config_path.parent / "state" / "bridge_conversations.sqlite",
            runtimes=self._runtime_list(),
        )
        self.transfer_store = TransferStore(self.config_path.parent / "state" / "bridge_transfers.sqlite")
        self.app = web.Application(client_max_size=64 * 1024 * 1024)
        self.app.router.add_get("/api/agents", self.handle_agents)
        self.app.router.add_get("/api/transcript/{name}", self.handle_transcript_recent)
        self.app.router.add_get("/api/transcript/{name}/poll", self.handle_transcript_poll)
        self.app.router.add_post("/api/chat", self.handle_chat)
        self.app.router.add_post("/api/bridge/message", self.handle_bridge_message)
        self.app.router.add_post("/api/bridge/reply", self.handle_bridge_reply)
        self.app.router.add_post("/api/bridge/transfer", self.handle_bridge_transfer)
        self.app.router.add_post("/api/bridge/fork", self.handle_bridge_fork)
        self.app.router.add_post("/api/bridge/cos", self.handle_bridge_cos)
        self.app.router.add_get("/api/bridge/transfer/{transfer_id}", self.handle_bridge_transfer_get)
        self.app.router.add_post("/api/bridge/spawn", self.handle_bridge_spawn)
        self.app.router.add_get("/api/bridge/message/{message_id}", self.handle_bridge_message_get)
        self.app.router.add_get("/api/bridge/thread/{thread_id}", self.handle_bridge_thread)
        self.app.router.add_get("/api/bridge/capabilities/{agent}", self.handle_bridge_capabilities)
        self.app.router.add_get("/api/admin/commands/{name}", self.handle_admin_commands)
        self.app.router.add_post("/api/admin/command", self.handle_admin_command)
        self.app.router.add_post("/api/agents/{name}/command", self.handle_agent_command)
        self.app.router.add_post("/api/admin/smoke", self.handle_admin_smoke)
        self.app.router.add_post("/api/admin/start-agent", self.handle_admin_start_agent)
        self.app.router.add_post("/api/admin/stop-agent", self.handle_admin_stop_agent)
        self.app.router.add_post("/api/admin/shutdown", self.handle_admin_shutdown)
        self.app.router.add_get("/api/health", self.handle_health)
        self.runner = None
        self.site = None

    def _learn_reply_route(self, text: str, reply_route: dict) -> None:
        """Auto-learn sender's routing info from reply_route metadata in hchat messages."""
        try:
            from tools.hchat_send import parse_return_address, update_contact
            info = parse_return_address(text)
            if not info:
                return
            inst = reply_route.get("instance_id", info.get("instance_id", ""))
            host = reply_route.get("host", "")
            port = reply_route.get("port", 0)
            wb_port = reply_route.get("wb_port", port)
            ttl = reply_route.get("ttl", 3600)
            if inst and host and port:
                update_contact(info["agent"], inst, host, port, wb_port=wb_port, ttl=ttl)
        except Exception:
            pass  # non-critical — don't break message delivery

    def _runtime_list(self) -> list:
        if self.orchestrator is not None:
            return list(getattr(self.orchestrator, "runtimes", []))
        return list(self.runtimes)

    def _load_agent_rows(self) -> list[dict]:
        raw = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        return [agent for agent in raw.get("agents", []) if agent.get("is_active", True)]

    def _runtime_map(self) -> dict:
        return {runtime.name: runtime for runtime in self._runtime_list()}

    def _refresh_bridge_router(self) -> None:
        self.bridge_router.refresh(self._runtime_list())

    def _check_admin_auth(self, request) -> bool:
        if not self.admin_token:
            return True
        provided = (
            request.headers.get("X-Workbench-Token")
            or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        )
        return provided == self.admin_token

    def _default_smoke_commands(self, runtime) -> list[str]:
        commands = ["/status", "/model"]
        available = set(supported_commands(runtime))
        if "backend" in available:
            commands.append("/backend")
        if "memory" in available:
            commands.append("/memory")
        if "effort" in available:
            commands.append("/effort")
        if "think" in available:
            commands.append("/think")
        return commands

    async def _wait_for_assistant_reply(
        self,
        transcript_path: Path,
        offset: int,
        timeout_s: float,
        expected_source: str | None = None,
        expected_prompt: str | None = None,
    ) -> dict:
        deadline = time.monotonic() + timeout_s
        current_offset = offset
        matched_prompt = False
        while time.monotonic() < deadline:
            data = _read_jsonl_increment(transcript_path, current_offset)
            current_offset = data.get("offset", current_offset)
            new_messages = data.get("messages", [])
            if expected_source or expected_prompt:
                for message in new_messages:
                    role = message.get("role")
                    text = message.get("text")
                    if not text:
                        continue
                    if role == "user":
                        source_ok = expected_source is None or message.get("source") == expected_source
                        prompt_ok = expected_prompt is None or text == expected_prompt
                        if source_ok and prompt_ok:
                            matched_prompt = True
                            continue
                    if matched_prompt and role == "assistant":
                        return {
                            "received": True,
                            "offset": current_offset,
                            "assistant_text": text,
                            "new_messages": new_messages,
                        }
            else:
                assistants = [m for m in new_messages if m.get("role") == "assistant" and m.get("text")]
                if assistants:
                    return {
                        "received": True,
                        "offset": current_offset,
                        "assistant_text": assistants[-1]["text"],
                        "new_messages": new_messages,
                    }
            await asyncio.sleep(0.5)
        return {"received": False, "offset": current_offset, "assistant_text": None, "new_messages": []}

    def _resolve_transcript_path(self, agent_row: dict, runtime) -> Path:
        if runtime is not None and getattr(runtime, "transcript_log_path", None):
            return Path(runtime.transcript_log_path)

        workspace_dir = resolve_path_value(
            agent_row["workspace_dir"],
            config_dir=self.config_path.parent,
            bridge_home=self.global_config.bridge_home,
        ) or (self.config_path.parent / agent_row["workspace_dir"])
        if agent_row.get("type") == "flex":
            return workspace_dir / "transcript.jsonl"
        return workspace_dir / "conversation_log.jsonl"

    def _metadata_for_agent(self, agent_row: dict, runtime) -> dict:
        if runtime is not None:
            metadata = runtime.get_runtime_metadata()
        else:
            transcript_path = self._resolve_transcript_path(agent_row, runtime)
            workspace_dir = resolve_path_value(
                agent_row["workspace_dir"],
                config_dir=self.config_path.parent,
                bridge_home=self.global_config.bridge_home,
            ) or (self.config_path.parent / agent_row["workspace_dir"])
            engine = agent_row.get("engine") or agent_row.get("active_backend", "unknown")
            model = agent_row.get("model", "unknown")
            if agent_row.get("type") == "flex":
                for backend in agent_row.get("allowed_backends", []):
                    if backend.get("engine") == agent_row.get("active_backend"):
                        model = backend.get("model", model)
                        break
            metadata = {
                "id": agent_row["name"],
                "name": agent_row["name"],
                "display_name": agent_row.get("display_name", agent_row["name"]),
                "emoji": agent_row.get("emoji", "🤖"),
                "engine": engine,
                "active_backend": agent_row.get("active_backend", engine),
                "model": model,
                "allowed_backends": [dict(backend) for backend in agent_row.get("allowed_backends", [])],
                "workspace_dir": str(workspace_dir),
                "transcript_path": str(transcript_path),
                "online": False,
                "status": "offline",
                "type": agent_row.get("type", "fixed"),
                "telegram_connected": False,
                "channels": {
                    "telegram": False,
                    "workbench": False,
                    "whatsapp": self._is_whatsapp_available(),
                },
            }
        return metadata

    def _is_whatsapp_available(self) -> bool:
        if self.orchestrator is None:
            return False
        wa = getattr(self.orchestrator, "whatsapp", None)
        return wa is not None and getattr(wa, "_client", None) is not None

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", self.global_config.workbench_port)
        await self.site.start()

    async def shutdown(self):
        if self.runner:
            await self.runner.cleanup()

    async def handle_agents(self, request):
        runtime_map = self._runtime_map()
        agents = [
            self._metadata_for_agent(agent_row, runtime_map.get(agent_row["name"]))
            for agent_row in self._load_agent_rows()
        ]
        return web.json_response({"agents": agents})

    async def handle_transcript_recent(self, request):
        name = request.match_info["name"]
        limit = max(1, min(int(request.query.get("limit", 50)), 200))
        runtime_map = self._runtime_map()
        agent_row = next((row for row in self._load_agent_rows() if row["name"] == name), None)
        if agent_row is None:
            return web.json_response({"error": "agent not found"}, status=404)
        transcript_path = self._resolve_transcript_path(agent_row, runtime_map.get(name))
        return web.json_response(_read_jsonl_recent(transcript_path, limit=limit))

    async def handle_transcript_poll(self, request):
        name = request.match_info["name"]
        offset = int(request.query.get("offset", 0))
        runtime_map = self._runtime_map()
        agent_row = next((row for row in self._load_agent_rows() if row["name"] == name), None)
        if agent_row is None:
            return web.json_response({"error": "agent not found"}, status=404)
        transcript_path = self._resolve_transcript_path(agent_row, runtime_map.get(name))
        return web.json_response(_read_jsonl_increment(transcript_path, offset=offset))

    def _classify_upload(self, filename: str, declared_media_type: str = "", content_type: str = "") -> str:
        if declared_media_type:
            return declared_media_type.lower()

        mime = content_type or mimetypes.guess_type(filename)[0] or ""
        suffix = Path(filename).suffix.lower()
        if mime.startswith("image/"):
            if suffix == ".webp":
                return "sticker"
            return "photo"
        if mime.startswith("audio/"):
            if suffix == ".ogg":
                return "voice"
            return "audio"
        if mime.startswith("video/"):
            return "video"
        return "document"

    async def _save_upload(self, runtime, part) -> tuple[Path, str]:
        filename = part.filename or f"upload_{uuid4().hex}"
        safe_name = f"{uuid4().hex}_{Path(filename).name}"
        local_path = runtime.media_dir / safe_name
        with open(local_path, "wb") as f:
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                f.write(chunk)
        return local_path, filename

    async def handle_chat(self, request):
        runtime_map = self._runtime_map()

        if request.content_type.startswith("multipart/"):
            reader = await request.multipart()
            fields = {}
            uploads = []
            while True:
                part = await reader.next()
                if part is None:
                    break
                if part.filename:
                    uploads.append(part)
                else:
                    fields[part.name] = await part.text()

            agent_name = fields.get("agent") or fields.get("agentId")
            runtime = runtime_map.get(agent_name)
            if runtime is None:
                return web.json_response({"ok": False, "error": "agent not found"}, status=404)

            text = fields.get("text", "").strip()
            caption = fields.get("caption", "").strip()
            emoji = fields.get("sticker_emoji", "").strip()
            declared_media_type = fields.get("media_type", "").strip()

            request_ids = []
            if text and not uploads:
                request_id = await runtime.enqueue_api_text(text)
                request_ids.append(request_id)

            for part in uploads:
                local_path, original_name = await self._save_upload(runtime, part)
                media_kind = self._classify_upload(original_name, declared_media_type, part.headers.get("Content-Type", ""))
                request_id = await runtime.enqueue_api_media(
                    local_path=local_path,
                    media_kind=media_kind,
                    filename=original_name,
                    caption=caption or text,
                    emoji=emoji,
                )
                request_ids.append(request_id)

            if not request_ids:
                return web.json_response({"ok": False, "error": "empty payload"}, status=400)

            return web.json_response({"ok": True, "request_id": request_ids[0], "request_ids": request_ids})

        payload = await request.json()
        agent_name = payload.get("agent") or payload.get("agentId")
        text = (payload.get("text") or "").strip()
        runtime = runtime_map.get(agent_name)
        if runtime is None:
            return web.json_response({"ok": False, "error": "agent not found"}, status=404)
        if not text:
            return web.json_response({"ok": False, "error": "text is required"}, status=400)

        # Auto-learn reply route from hchat messages (updates contacts.json)
        reply_route = payload.get("reply_route")
        if reply_route and isinstance(reply_route, dict):
            self._learn_reply_route(text, reply_route)

        request_id = await runtime.enqueue_api_text(text)
        return web.json_response({"ok": True, "request_id": request_id})

    async def handle_bridge_message(self, request):
        self._refresh_bridge_router()
        try:
            payload = await request.json()
            result = await self.bridge_router.send_message(payload)
            return web.json_response(result)
        except PermissionError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=403)
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        except RuntimeError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=409)

    async def handle_bridge_reply(self, request):
        self._refresh_bridge_router()
        try:
            payload = await request.json()
            result = await self.bridge_router.submit_reply(payload)
            return web.json_response(result)
        except PermissionError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=403)
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    def _validate_transfer_payload(self, payload: dict) -> dict:
        required = [
            "transfer_id",
            "source_agent",
            "source_instance",
            "target_agent",
            "target_instance",
            "created_at",
            "recent_context_block",
            "last_user_message",
            "last_assistant_message",
        ]
        missing = [key for key in required if not str(payload.get(key) or "").strip()]
        if missing:
            raise ValueError(f"missing transfer fields: {', '.join(missing)}")
        mode = str(payload.get("mode") or "transfer").strip().lower()
        if mode not in {"transfer", "fork"}:
            raise ValueError("mode must be transfer or fork")
        return {
            "transfer_id": str(payload["transfer_id"]).strip(),
            "mode": mode,
            "source_agent": str(payload["source_agent"]).strip(),
            "source_instance": str(payload["source_instance"]).strip(),
            "target_agent": str(payload["target_agent"]).strip(),
            "target_instance": str(payload["target_instance"]).strip(),
            "created_at": str(payload["created_at"]).strip(),
            "exchange_count": int(payload.get("exchange_count") or 0),
            "word_count": int(payload.get("word_count") or 0),
            "recent_context_block": str(payload["recent_context_block"]).strip(),
            "recent_rounds": payload.get("recent_rounds") if isinstance(payload.get("recent_rounds"), list) else [],
            "last_user_message": str(payload["last_user_message"]).strip(),
            "last_assistant_message": str(payload["last_assistant_message"]).strip(),
            "source_runtime": payload.get("source_runtime") if isinstance(payload.get("source_runtime"), dict) else {},
            "source_workspace_dir": str(payload.get("source_workspace_dir") or "").strip(),
            "source_transcript_path": str(payload.get("source_transcript_path") or "").strip(),
            "transfer_guidance": payload.get("transfer_guidance") if isinstance(payload.get("transfer_guidance"), dict) else {},
            "task_state": payload.get("task_state") if isinstance(payload.get("task_state"), dict) else {},
            "handoff_summary": str(payload.get("handoff_summary") or "").strip(),
            "memory_files": payload.get("memory_files") if isinstance(payload.get("memory_files"), dict) else {},
        }

    def _accept_prefix_for_mode(self, mode: str) -> str:
        return self.FORK_ACCEPT_PREFIX if mode == "fork" else self.TRANSFER_ACCEPT_PREFIX

    def _build_transfer_prompt(self, package: dict) -> str:
        mode = str(package.get("mode") or "transfer").strip().lower()
        action_label = "fork" if mode == "fork" else "transfer"
        intro = (
            "SYSTEM: This is a bridge-managed fork.\n"
            "You are receiving a parallel context branch. The source session remains active.\n"
            if mode == "fork"
            else "SYSTEM: This is a bridge-managed transfer.\n"
        )
        guidance = package.get("transfer_guidance") or {}
        task_state = package.get("task_state") or {}
        memory_files = package.get("memory_files") or {}
        memory_sections = []
        for name in ("project.md", "decisions.md", "tasks.md"):
            text = str(memory_files.get(name) or "").strip()
            if text:
                memory_sections.append(f"[{name}]\n{text}")
        memory_block = "\n\n".join(memory_sections).strip()
        handoff_summary = str(package.get("handoff_summary") or "").strip()
        return (
            intro
            +
            f"You are NOT {package['source_agent']}. Do not imitate that agent's identity, persona, or relationship style.\n"
            "Keep your own identity, permissions, and system instructions.\n\n"
            f"Continue the {action_label}ed work using the operational context below.\n\n"
            "--- TRANSFER METADATA ---\n"
            f"Mode: {mode}\n"
            f"Transfer ID: {package['transfer_id']}\n"
            f"From: {package['source_agent']}@{package['source_instance']}\n"
            f"To: {package['target_agent']}@{package['target_instance']}\n"
            f"Created at: {package['created_at']}\n"
            f"Recent exchanges captured: {package['exchange_count']}\n"
            f"Recent words captured: {package['word_count']}\n\n"
            "--- CONTEXT WEIGHTING RULES ---\n"
            f"- {guidance.get('recent_turn_weighting') or 'Prefer the newest exchanges for current intent, task state, and next actions.'}\n"
            f"- {guidance.get('older_turn_weighting') or 'Treat older exchanges as background only.'}\n"
            f"- {guidance.get('conflict_rule') or 'If there is any conflict, prefer the newer context.'}\n\n"
            "--- STRUCTURED TASK STATE ---\n"
            f"Latest user request: {task_state.get('latest_user_request') or package['last_user_message']}\n"
            f"Latest source reply: {task_state.get('latest_source_reply') or package['last_assistant_message']}\n"
            f"Recent exchange count retained: {task_state.get('recent_exchange_count') or package['exchange_count']}\n"
            f"Memory files available: {', '.join(task_state.get('memory_files_available') or []) or 'none'}\n\n"
            "--- HANDOFF SUMMARY ---\n"
            f"{handoff_summary or 'No handoff summary was available.'}\n\n"
            "--- MEMORY FILE CONTEXT ---\n"
            f"{memory_block or 'No project/decision/task memory files were available.'}\n\n"
            "--- CONTINUITY CONTEXT ---\n"
            "Last user message:\n"
            f"{package['last_user_message']}\n\n"
            "Last assistant message from source:\n"
            f"{package['last_assistant_message']}\n\n"
            f"{package['recent_context_block']}\n\n"
            "--- REQUIRED FIRST RESPONSE ---\n"
            f"Start your first reply with: {self._accept_prefix_for_mode(mode)}{package['transfer_id']}\n"
            "Then, in your own voice:\n"
            "1. State what task is currently in progress.\n"
            "2. Continue directly from the next unfinished step.\n"
            "3. Do not ask the user to restate context unless a critical field is missing."
        )

    async def _notify_transfer_chat(self, runtime, text: str, *, purpose: str) -> dict[str, Any]:
        if not getattr(runtime, "telegram_connected", False):
            runtime.logger.warning(
                "Transfer system notification not delivered because Telegram is disconnected.",
                extra={"purpose": purpose},
            )
            return {"delivered": False, "reason": "telegram_disconnected", "chunks": 0}
        try:
            _, chunk_count = await runtime.send_long_message(
                chat_id=runtime._primary_chat_id(),
                text=text,
                request_id=f"transfer-{uuid4().hex[:8]}",
                purpose=purpose,
            )
            return {
                "delivered": chunk_count > 0,
                "reason": "sent" if chunk_count > 0 else "telegram_send_skipped",
                "chunks": chunk_count,
            }
        except Exception:
            runtime.logger.warning("Failed to deliver transfer system notification.", exc_info=True)
            return {"delivered": False, "reason": "send_exception", "chunks": 0}

    def _classify_transfer_ack(self, transfer_id: str, result: dict[str, Any], *, mode: str = "transfer") -> dict[str, Any]:
        if not result.get("success"):
            return {"ok": False, "error": result.get("error") or "transfer bootstrap failed"}
        raw_text = str(result.get("text") or "").strip()
        if not raw_text:
            return {"ok": False, "error": "target completed transfer bootstrap without a visible reply"}
        expected = f"{self._accept_prefix_for_mode(mode)}{transfer_id}"
        if raw_text.startswith(expected):
            return {"ok": True, "raw_text": raw_text, "ack_mode": "explicit"}
        return {"ok": True, "raw_text": raw_text, "ack_mode": "implicit"}

    def _finalize_transfer_status(self, *notifications: dict[str, Any]) -> tuple[str, str]:
        if any(not note.get("delivered") for note in notifications):
            return "accepted_but_chat_offline", "offline"
        return "accepted", "online"

    async def handle_bridge_transfer(self, request):
        return await self._handle_bridge_handoff(request, mode="transfer")

    async def handle_bridge_fork(self, request):
        return await self._handle_bridge_handoff(request, mode="fork")

    async def _handle_bridge_handoff(self, request, *, mode: str):
        payload = await request.json()
        payload["mode"] = mode
        try:
            package = self._validate_transfer_payload(payload)
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

        from orchestrator.ticket_manager import detect_instance

        current_instance = detect_instance(self.global_config.project_root)
        if str(package["target_instance"]).upper() != str(current_instance).upper():
            return web.json_response(
                {"ok": False, "error": f"target instance mismatch: this endpoint is {current_instance}"},
                status=409,
            )
        runtime = self._runtime_map().get(package["target_agent"])
        if runtime is None:
            return web.json_response({"ok": False, "error": "target agent not found"}, status=404)
        if package["source_agent"] == package["target_agent"] and package["source_instance"] == package["target_instance"]:
            return web.json_response({"ok": False, "error": f"cannot {mode} to the same agent"}, status=400)
        if not getattr(runtime, "startup_success", False):
            return web.json_response({"ok": False, "error": "target runtime is offline"}, status=409)
        if mode == "transfer" and getattr(runtime, "has_active_transfer", None) and runtime.has_active_transfer():
            return web.json_response({"ok": False, "error": "target already has an active transfer"}, status=409)

        transfer_id = package["transfer_id"]
        self.transfer_store.create_transfer(package, status="received")
        self.transfer_store.append_event(transfer_id, "received", {"target_agent": package["target_agent"]})
        incoming_notice = await self._notify_transfer_chat(
            runtime,
            (
                f"Incoming {'fork' if mode == 'fork' else 'transfer'} received from "
                f"{package['source_agent']}@{package['source_instance']}.\n"
                f"{'Fork' if mode == 'fork' else 'Transfer'} ID: {transfer_id}"
            ),
            purpose=f"{mode}-incoming",
        )
        self.transfer_store.append_event(transfer_id, "incoming_notice", incoming_notice)
        bridge_prompt = self._build_transfer_prompt(package)
        request_id = await runtime.enqueue_api_text(
            bridge_prompt,
            source=f"bridge-{mode}:{transfer_id}",
            deliver_to_telegram=True,
        )
        if request_id is None:
            self.transfer_store.update_transfer(
                transfer_id,
                status="failed",
                error_code="enqueue_failed",
                error_text=f"failed to enqueue {mode} request on target",
            )
            self.transfer_store.append_event(transfer_id, "failed", {"reason": "enqueue_failed"})
            await self._notify_transfer_chat(
                runtime,
                (
                    f"{'Fork' if mode == 'fork' else 'Transfer'} failed before enqueue.\n"
                    f"{'Fork' if mode == 'fork' else 'Transfer'} ID: {transfer_id}"
                ),
                purpose=f"{mode}-failed",
            )
            return web.json_response({"ok": False, "error": f"failed to enqueue {mode} request on target"}, status=409)

        self.transfer_store.update_transfer(transfer_id, status="queued_on_target", request_id=request_id)
        self.transfer_store.append_event(transfer_id, "queued_on_target", {"request_id": request_id})
        loop = asyncio.get_running_loop()
        ack_future = loop.create_future()

        async def _listener(result: dict) -> None:
            if ack_future.done():
                return
            ack_future.set_result(self._classify_transfer_ack(transfer_id, result, mode=mode))

        runtime.register_request_listener(request_id, _listener)
        timeout_s = max(10.0, min(float(payload.get("timeout_s") or 90.0), 180.0))
        try:
            ack_result = await asyncio.wait_for(ack_future, timeout=timeout_s)
        except asyncio.TimeoutError:
            ack_result = {"ok": False, "error": f"target did not acknowledge {mode} within {int(timeout_s)}s"}

        if not ack_result.get("ok"):
            error_text = str(ack_result.get("error") or f"{mode} failed")
            self.transfer_store.update_transfer(
                transfer_id,
                status="failed",
                error_code="target_ack_failed",
                error_text=error_text,
            )
            self.transfer_store.append_event(transfer_id, "failed", {"reason": error_text})
            await self._notify_transfer_chat(
                runtime,
                (
                    f"{'Fork' if mode == 'fork' else 'Transfer'} failed.\n"
                    f"{'Fork' if mode == 'fork' else 'Transfer'} ID: {transfer_id}\n"
                    f"Reason: {error_text}"
                ),
                purpose=f"{mode}-failed",
            )
            return web.json_response({"ok": False, "error": error_text, "transfer_id": transfer_id}, status=409)

        ack_text = str(ack_result.get("raw_text") or "")
        ack_mode = str(ack_result.get("ack_mode") or "explicit")
        accepted_notice = await self._notify_transfer_chat(
            runtime,
            (
                f"{'Fork' if mode == 'fork' else 'Transfer'} accepted from "
                f"{package['source_agent']}@{package['source_instance']}.\n"
                f"{'Fork' if mode == 'fork' else 'Transfer'} ID: {transfer_id}"
            ),
            purpose=f"{mode}-accepted",
        )
        self.transfer_store.append_event(transfer_id, "accepted_notice", accepted_notice)
        final_status, target_chat_status = self._finalize_transfer_status(incoming_notice, accepted_notice)
        self.transfer_store.update_transfer(transfer_id, status=final_status, ack_text=ack_text)
        self.transfer_store.append_event(
            transfer_id,
            "accepted",
            {
                "request_id": request_id,
                "ack_mode": ack_mode,
                "target_chat_status": target_chat_status,
            },
        )
        return web.json_response(
            {
                "ok": True,
                "transfer_id": transfer_id,
                "request_id": request_id,
                "status": final_status,
                "ack_mode": ack_mode,
                "target_chat_status": target_chat_status,
            }
        )

    async def handle_bridge_cos(self, request):
        """Handle cross-instance Chief of Staff query. Routes to Lily for precedent-based decision support."""
        payload = await request.json()
        question = str(payload.get("question") or "").strip()
        from_agent = str(payload.get("from_agent") or "").strip()
        if not question or not from_agent:
            return web.json_response({"ok": False, "error": "question and from_agent required"}, status=400)

        lily_runtime = self._runtime_map().get("lily")
        if lily_runtime is None or not getattr(lily_runtime, "startup_success", False):
            return web.json_response({"ok": False, "error": "lily is offline", "reason": "lily_offline"}, status=503)

        cos_result = await lily_runtime.cos_query(question, timeout_s=float(payload.get("timeout_s") or 30.0))
        return web.json_response({"ok": cos_result.get("answered", False), **cos_result})

    async def handle_bridge_transfer_get(self, request):
        transfer_id = request.match_info["transfer_id"]
        record = self.transfer_store.get_transfer(transfer_id)
        if record is None:
            return web.json_response({"ok": False, "error": "transfer not found"}, status=404)
        return web.json_response({"ok": True, "transfer": record})

    async def handle_bridge_spawn(self, request):
        return web.json_response(
            {
                "ok": False,
                "error": "spawn is reserved for a later phase and is not implemented in Phase 1",
            },
            status=501,
        )

    async def handle_bridge_thread(self, request):
        self._refresh_bridge_router()
        thread_id = request.match_info["thread_id"]
        thread = self.bridge_router.get_thread(thread_id)
        if thread is None:
            return web.json_response({"ok": False, "error": "thread not found"}, status=404)
        return web.json_response({"ok": True, "thread": thread})

    async def handle_bridge_message_get(self, request):
        self._refresh_bridge_router()
        message_id = request.match_info["message_id"]
        message = self.bridge_router.get_message(message_id)
        if message is None:
            return web.json_response({"ok": False, "error": "message not found"}, status=404)
        return web.json_response({"ok": True, "message": message})

    async def handle_bridge_capabilities(self, request):
        self._refresh_bridge_router()
        agent_name = request.match_info["agent"]
        capability = self.bridge_router.get_capability(agent_name)
        if capability is None:
            return web.json_response({"ok": False, "error": "agent not found"}, status=404)
        return web.json_response({"ok": True, "capability": capability})

    async def handle_admin_commands(self, request):
        if not self._check_admin_auth(request):
            return web.json_response({"ok": False, "error": "admin auth failed"}, status=403)

        name = request.match_info["name"]
        runtime = self._runtime_map().get(name)
        if runtime is None:
            return web.json_response({"ok": False, "error": "agent not found"}, status=404)
        return web.json_response({"ok": True, "agent": name, "commands": supported_commands(runtime)})

    async def handle_admin_command(self, request):
        if not self._check_admin_auth(request):
            return web.json_response({"ok": False, "error": "admin auth failed"}, status=403)

        payload = await request.json()
        agent_name = payload.get("agent") or payload.get("agentId")
        command = (payload.get("command") or "").strip()
        chat_id = payload.get("chat_id")

        runtime = self._runtime_map().get(agent_name)
        if runtime is None:
            return web.json_response({"ok": False, "error": "agent not found"}, status=404)
        if not command:
            return web.json_response({"ok": False, "error": "command is required"}, status=400)

        result = await execute_local_command(runtime, command, chat_id=chat_id)
        status = 200 if result.get("ok") else 400
        result["agent"] = agent_name
        return web.json_response(result, status=status)

    async def handle_agent_command(self, request):
        """Handle /api/agents/{name}/command - simpler endpoint for frontend."""
        agent_name = request.match_info.get("name")
        payload = await request.json()
        command = (payload.get("command") or "").strip()

        runtime = self._runtime_map().get(agent_name)
        if runtime is None:
            return web.json_response({"ok": False, "error": "agent not found"}, status=404)
        if not command:
            return web.json_response({"ok": False, "error": "command is required"}, status=400)

        result = await execute_local_command(runtime, command)
        status_code = 200 if result.get("ok") else 400
        result["agent"] = agent_name
        return web.json_response(result, status=status_code)

    async def handle_admin_smoke(self, request):
        if not self._check_admin_auth(request):
            return web.json_response({"ok": False, "error": "admin auth failed"}, status=403)

        payload = await request.json()
        runtime_map = self._runtime_map()

        requested_agent = payload.get("agent")
        requested_agents = payload.get("agents")
        if requested_agents:
            target_names = [name for name in requested_agents if name in runtime_map]
        elif requested_agent:
            target_names = [requested_agent] if requested_agent in runtime_map else []
        else:
            target_names = [rt.name for rt in self._runtime_list()]

        if not target_names:
            return web.json_response({"ok": False, "error": "no matching agents"}, status=404)

        include_commands = bool(payload.get("include_commands", True))
        include_chat = bool(payload.get("include_chat", True))
        chat_text = (payload.get("chat_text") or "Smoke test ping. Reply with one short line.").strip()
        timeout_s = float(payload.get("timeout_s", 45))
        timeout_s = max(5.0, min(timeout_s, 180.0))

        command_plan = payload.get("commands")
        results = []

        for name in target_names:
            runtime = runtime_map[name]
            agent_result = {"agent": name, "commands": [], "chat": None}

            if include_commands:
                commands = command_plan if isinstance(command_plan, list) and command_plan else self._default_smoke_commands(runtime)
                for command in commands:
                    cmd_result = await execute_local_command(runtime, str(command))
                    agent_result["commands"].append(cmd_result)

            if include_chat:
                agent_row = next((row for row in self._load_agent_rows() if row["name"] == name), None)
                transcript_path = self._resolve_transcript_path(agent_row, runtime) if agent_row else Path(runtime.get_runtime_metadata()["transcript_path"])
                start_offset = transcript_path.stat().st_size if transcript_path.exists() else 0
                request_id = await runtime.enqueue_api_text(chat_text, source="api-smoke")
                wait_result = await self._wait_for_assistant_reply(
                    transcript_path,
                    start_offset,
                    timeout_s,
                    expected_source="api-smoke",
                    expected_prompt=chat_text,
                )
                wait_result["request_id"] = request_id
                wait_result["prompt"] = chat_text
                agent_result["chat"] = wait_result

            results.append(agent_result)

        all_ok = True
        for result in results:
            for cmd in result["commands"]:
                if not cmd.get("ok"):
                    all_ok = False
            if include_chat and result["chat"] and not result["chat"].get("received"):
                all_ok = False

        return web.json_response({"ok": all_ok, "results": results})

    async def handle_health(self, request):
        running_agents = [runtime.name for runtime in self._runtime_list() if runtime.startup_success]
        return web.json_response({"ok": True, "agents": running_agents})

    async def handle_admin_start_agent(self, request):
        if not self._check_admin_auth(request):
            return web.json_response({"ok": False, "error": "admin auth failed"}, status=403)
        if self.orchestrator is None:
            return web.json_response({"ok": False, "error": "orchestrator unavailable"}, status=503)
        payload = await request.json()
        agent_name = payload.get("agent") or payload.get("agentId")
        if not agent_name:
            return web.json_response({"ok": False, "error": "agent is required"}, status=400)
        ok, message = await self.orchestrator.start_agent(str(agent_name))
        status = 200 if ok else 400
        return web.json_response({"ok": ok, "agent": agent_name, "message": message}, status=status)

    async def handle_admin_stop_agent(self, request):
        if not self._check_admin_auth(request):
            return web.json_response({"ok": False, "error": "admin auth failed"}, status=403)
        if self.orchestrator is None:
            return web.json_response({"ok": False, "error": "orchestrator unavailable"}, status=503)
        payload = await request.json()
        agent_name = payload.get("agent") or payload.get("agentId")
        if not agent_name:
            return web.json_response({"ok": False, "error": "agent is required"}, status=400)
        ok, message = await self.orchestrator.stop_agent(str(agent_name))
        status = 200 if ok else 400
        return web.json_response({"ok": ok, "agent": agent_name, "message": message}, status=status)

    async def handle_admin_shutdown(self, request):
        if not self._check_admin_auth(request):
            return web.json_response({"ok": False, "error": "admin auth failed"}, status=403)
        if self.orchestrator is None:
            return web.json_response({"ok": False, "error": "orchestrator unavailable"}, status=503)
        payload = await request.json() if request.can_read_body else {}
        reason = str((payload or {}).get("reason") or "admin-api")
        self.orchestrator.request_shutdown(reason=reason)
        return web.json_response({"ok": True, "message": f"Shutdown requested ({reason})."})
