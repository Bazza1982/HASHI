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
        self.app = web.Application(client_max_size=64 * 1024 * 1024)
        self.app.router.add_get("/api/agents", self.handle_agents)
        self.app.router.add_get("/api/transcript/{name}", self.handle_transcript_recent)
        self.app.router.add_get("/api/transcript/{name}/poll", self.handle_transcript_poll)
        self.app.router.add_post("/api/chat", self.handle_chat)
        self.app.router.add_post("/api/bridge/message", self.handle_bridge_message)
        self.app.router.add_post("/api/bridge/reply", self.handle_bridge_reply)
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
                "model": model,
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
