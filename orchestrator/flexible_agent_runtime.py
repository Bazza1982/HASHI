from __future__ import annotations
import re
import time
import asyncio
import inspect
import logging
import shutil
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Optional, Any
import json

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update, constants
from telegram.error import TimedOut as TelegramTimedOut
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from orchestrator.config import FlexibleAgentConfig, GlobalConfig
from orchestrator.agent_runtime import QueuedRequest, _safe_excerpt, _md_to_html, _print_user_message, _print_final_response, _print_thinking, resolve_authorized_telegram_ids
from orchestrator.agent_fyi import build_agent_fyi_primer
from orchestrator.bridge_memory import BridgeMemoryStore, BridgeContextAssembler, SysPromptManager
from orchestrator.flexible_backend_manager import FlexibleBackendManager
from orchestrator.flexible_backend_registry import (
    CLAUDE_MODEL_ALIASES,
    get_available_efforts,
    get_available_models,
    get_backend_label,
    normalize_effort,
    normalize_model,
)
from orchestrator.memory_index import MemoryIndex
from orchestrator.handoff_builder import HandoffBuilder
from orchestrator.media_utils import is_image_file, normalize_image_file
from orchestrator.parked_topics import ParkedTopicStore
from orchestrator.skill_manager import SkillDefinition, SkillManager
from orchestrator.voice_manager import VoiceManager

class FlexibleAgentRuntime:

    CODEX_CHUNK_LIMIT_ERROR = "Separator is not found, and chunk exceed the limit"
    CODEX_SCHEDULER_RETRY_DELAY_S = 120

    def __init__(self, config: FlexibleAgentConfig, global_config: GlobalConfig, telegram_token: str, secrets: dict, skill_manager: SkillManager | None = None):
        self.config = config
        self.global_config = global_config
        self.token = telegram_token
        self.secrets = secrets
        self.name = config.name

        self.session_started_at = datetime.now()
        self.session_id_dt = self.session_started_at.strftime("%Y-%m-%d_%H%M%S")
        self.session_dir = self.global_config.base_logs_dir / self.name / self.session_id_dt
        self.media_dir = self.global_config.base_media_dir / self.name
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)

        self.logger = logging.getLogger(f"FlexRuntime.{self.name}")
        self.telegram_logger = logging.getLogger(f"FlexRuntime.{self.name}.telegram")
        self.message_logger = logging.getLogger(f"FlexRuntime.{self.name}.messages")
        self.error_logger = logging.getLogger(f"FlexRuntime.{self.name}.errors")
        self.maintenance_logger = logging.getLogger(f"FlexRuntime.{self.name}.maintenance")
        self._setup_logging()

        self.startup_success = False
        self.backend_ready = False
        self.telegram_connected = False
        self.process_task = None
        self.queue = asyncio.Queue()
        self.request_seq = 0
        self.is_generating = False
        self.last_prompt = None
        self.last_response: dict | None = None
        self.current_request_meta: dict | None = None
        self.last_activity_at = datetime.now()
        self.last_success_at: datetime | None = None
        self.last_error_at: datetime | None = None
        self.last_error_summary: str | None = None
        self.last_backend_switch_at: datetime | None = None
        self.is_shutting_down = False
        self._scheduled_retry_tasks: set[asyncio.Task] = set()
        # Background tasks spawned when bg_mode detaches a long-running generation.
        self._background_tasks: set[asyncio.Task] = set()
        self._request_listeners: dict[str, list] = {}
        self._pending_request_results: dict[str, dict] = {}
        self.skill_manager = skill_manager
        self.agent_fyi_path = self.global_config.project_root / "docs" / "AGENT_FYI.md"
        self._pending_session_primer: str | None = None
        self._pending_auto_recall_context: str | None = None

        self.app = ApplicationBuilder().token(self.token).get_updates_connection_pool_size(8).build()

        # Workspace structure
        self.workspace_dir = config.workspace_dir
        # Load persisted verbose preference (.verbose file presence = ON, absence = OFF)
        _verbose_file = self.workspace_dir / ".verbose"
        self._verbose: bool = _verbose_file.exists()
        _think_file = self.workspace_dir / ".think"
        self._think: bool = _think_file.exists()
        self._think_buffer: list[str] = []
        self._openrouter_think_chunk: str = ""
        self._last_openrouter_think_snippet: str | None = None
        self.memory_dir = self.workspace_dir / "memory"
        self.sys_prompt_manager = SysPromptManager(self.workspace_dir)
        self.backend_state_dir = self.workspace_dir / "backend_state"
        self.transcript_log_path = self.workspace_dir / "transcript.jsonl"
        self.recent_context_path = self.workspace_dir / "recent_context.jsonl"
        self.handoff_path = self.workspace_dir / "handoff.md"
        self.state_path = self.workspace_dir / "state.json"
        self.runtime_session_path = self.workspace_dir / ".runtime_session.json"
        self.voice_manager = VoiceManager(self.workspace_dir, self.media_dir, ffmpeg_cmd="ffmpeg")
        self._authorized_telegram_ids = resolve_authorized_telegram_ids(self.config.extra, self.global_config.authorized_id)

        # Command policy
        # - default: allow all commands
        # - limited: disable execution/admin commands by default
        self._command_policy_mode = "allow_all"
        self._disabled_commands: set[str] = set()
        self._enabled_commands: set[str] = set()
        self._init_command_policy()

        # Initialize directories
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.backend_state_dir.mkdir(parents=True, exist_ok=True)

        # Initialize Memory and Handoff Subsystems
        self.memory_index = MemoryIndex(self.workspace_dir / "memory_index.sqlite")
        self.handoff_builder = HandoffBuilder(self.workspace_dir)
        self.parked_topics = ParkedTopicStore(self.workspace_dir)
        self.memory_store = BridgeMemoryStore(self.workspace_dir)
        self.context_assembler = BridgeContextAssembler(
            self.memory_store,
            self.config.system_md,
            active_skill_provider=self._get_active_skill_sections,
            sys_prompt_manager=self.sys_prompt_manager,
        )

        # Initialize FlexibleBackendManager
        self.backend_manager = FlexibleBackendManager(config, global_config, secrets)

    def _primary_chat_id(self) -> int:
        if self._authorized_telegram_ids:
            return self._authorized_telegram_ids[0]
        return self.global_config.authorized_id

    def _is_authorized_user(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        return user_id in self._authorized_telegram_ids

    def _init_command_policy(self):
        extra = self.config.extra or {}

        # Defaults for limited conversational agents
        if getattr(self.config, "type", "flex") == "limited":
            self._command_policy_mode = "denylist"
            self._disabled_commands.update(
                {
                    # high-risk / admin / execution commands
                    "credit",
                    "retry",
                    "sys",
                    "skill",
                    "backend",
                    "handoff",
                    "fyi",
                    "debug",
                    "start",
                    "stop",
                    "terminate",
                    "reboot",
                    "wa_on",
                    "wa_off",
                    "wa_send",
                }
            )
            # explicitly allowed convenience commands for conversational agents
            self._enabled_commands.update({"jobs", "verbose", "think", "voice", "whisper"})

        # Optional overrides (per-agent): extra.limited_policy
        policy = extra.get("limited_policy") if isinstance(extra, dict) else None
        if isinstance(policy, dict):
            mode = (policy.get("mode") or "denylist").lower()
            if mode in {"denylist", "allowlist"}:
                self._command_policy_mode = mode
            for name in policy.get("disabled_commands", []) or []:
                if isinstance(name, str) and name.strip():
                    self._disabled_commands.add(name.strip().lstrip("/").lower())
            for name in policy.get("enabled_commands", []) or []:
                if isinstance(name, str) and name.strip():
                    self._enabled_commands.add(name.strip().lstrip("/").lower())

        # help/status/new/wipe/clear/model/effort/mode should always be available
        self._enabled_commands.update({"help", "status", "new", "wipe", "reset", "clear", "memory", "model", "effort", "mode", "jobs", "verbose", "think", "voice", "whisper"})

    def _is_command_allowed(self, cmd: str) -> bool:
        cmd = (cmd or "").lstrip("/").lower()
        if not cmd:
            return True
        if cmd in self._disabled_commands:
            return False
        if self._command_policy_mode == "allow_all":
            return True
        if self._command_policy_mode == "allowlist":
            return cmd in self._enabled_commands
        # denylist
        return True

    def _wrap_cmd(self, cmd: str, handler):
        async def _wrapped(update: Update, context: Any):
            if not self._is_authorized_user(update.effective_user.id):
                return
            if not self._is_command_allowed(cmd):
                await self._reply_text(update, f"/{cmd} is disabled for this agent.")
                return
            return await handler(update, context)
        return _wrapped

    def _setup_logging(self):
        formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        configured = (
            (self.logger, "events.log"),
            (self.telegram_logger, "telegram.log"),
            (self.message_logger, "messages.log"),
            (self.error_logger, "errors.log"),
            (self.maintenance_logger, "maintenance.log"),
        )
        for cur_logger, filename in configured:
            cur_logger.handlers.clear()
            cur_logger.setLevel(logging.INFO)
            cur_logger.propagate = False
            if cur_logger in (self.logger, self.error_logger):
                cur_logger.propagate = True
            fh = logging.FileHandler(self.session_dir / filename, encoding="utf-8")
            fh.setFormatter(formatter)
            cur_logger.addHandler(fh)

    async def initialize(self) -> bool:
        self.logger.info(f"Initializing flex agent '{self.name}'...")
        result = await self.backend_manager.initialize_active_backend()
        # Apply session mode if agent is in fixed mode
        if result and self.backend_manager.agent_mode == "fixed":
            backend = self.backend_manager.current_backend
            if hasattr(backend, "set_session_mode"):
                backend.set_session_mode(True)
                self.logger.info(f"Fixed mode active — session persistence enabled on {self.config.active_backend}")
        return result

    def _format_retry_summary(self, summary: str) -> str:
        if not summary:
            return "Scheduled Retry"
        if " Retry [" in summary:
            return summary
        bracket_index = summary.rfind(" [")
        if bracket_index == -1:
            return f"{summary} Retry"
        return f"{summary[:bracket_index]} Retry{summary[bracket_index:]}"

    def _should_retry_codex_scheduler_failure(self, item: QueuedRequest, err_msg: str) -> bool:
        return (
            self.config.active_backend == "codex-cli"
            and item.source == "scheduler"
            and not item.is_retry
            and self.CODEX_CHUNK_LIMIT_ERROR in (err_msg or "")
        )

    async def _enqueue_codex_scheduler_retry(self, item: QueuedRequest):
        try:
            await asyncio.sleep(self.CODEX_SCHEDULER_RETRY_DELAY_S)
            retry_summary = self._format_retry_summary(item.summary)
            retry_request_id = await self.enqueue_request(
                item.chat_id,
                item.prompt,
                "scheduler-retry",
                retry_summary,
                silent=item.silent,
                is_retry=True,
            )
            if retry_request_id:
                self.logger.warning(
                    f"Enqueued retry for {self._extract_task_id(item.summary) or '<none>'} "
                    f"as {retry_request_id} after {self.CODEX_SCHEDULER_RETRY_DELAY_S}s."
                )
                self._log_maintenance(
                    item,
                    "retry_enqueued",
                    retry_request_id=retry_request_id,
                    retry_delay_s=self.CODEX_SCHEDULER_RETRY_DELAY_S,
                )
        except asyncio.CancelledError:
            raise

    def _schedule_codex_scheduler_retry(self, item: QueuedRequest):
        task_id = self._extract_task_id(item.summary) or "<none>"
        self.logger.warning(
            f"Scheduling one retry for {task_id} after {self.CODEX_SCHEDULER_RETRY_DELAY_S}s "
            f"because Codex hit the chunk-limit failure."
        )
        self._log_maintenance(
            item,
            "retry_scheduled",
            retry_delay_s=self.CODEX_SCHEDULER_RETRY_DELAY_S,
            reason="codex_chunk_limit",
        )
        task = asyncio.create_task(self._enqueue_codex_scheduler_retry(item))
        self._scheduled_retry_tasks.add(task)
        task.add_done_callback(self._scheduled_retry_tasks.discard)

    def next_request_id(self) -> str:
        self.request_seq += 1
        return f"req-{self.request_seq:04d}"

    async def enqueue_request(
        self,
        chat_id: int,
        prompt: str,
        source: str,
        summary: str,
        silent: bool = False,
        is_retry: bool = False,
        deliver_to_telegram: bool = True,
    ):
        if not prompt or not prompt.strip():
            self.error_logger.error(f"Rejected empty prompt from {source} (summary={summary!r})")
            return None
        item = QueuedRequest(
            request_id=self.next_request_id(),
            chat_id=chat_id,
            prompt=prompt,
            source=source,
            summary=summary,
            created_at=datetime.now().isoformat(),
            silent=silent,
            is_retry=is_retry,
            deliver_to_telegram=deliver_to_telegram,
        )
        await self.queue.put(item)
        self.message_logger.info(f"Queued {item.request_id} from {source} (summary={summary!r})")
        return item.request_id

    def register_request_listener(self, request_id: str, callback):
        self._request_listeners.setdefault(request_id, []).append(callback)
        pending = self._pending_request_results.pop(request_id, None)
        if pending is not None:
            result = callback(pending)
            if inspect.isawaitable(result):
                asyncio.create_task(result)

    async def _notify_request_listeners(self, request_id: str, payload: dict):
        callbacks = self._request_listeners.pop(request_id, [])
        if not callbacks:
            self._pending_request_results[request_id] = payload
            return
        for callback in callbacks:
            result = callback(payload)
            if inspect.isawaitable(result):
                await result

    async def enqueue_startup_bootstrap(self, chat_id: int):
        if self.backend_manager.current_backend:
            if hasattr(self.backend_manager.current_backend, "should_bootstrap_on_startup"):
                if self.backend_manager.current_backend.should_bootstrap_on_startup():
                    prompt = self.backend_manager.current_backend.get_startup_bootstrap_prompt()
                    if prompt:
                        await self.enqueue_request(chat_id, prompt, "startup", "Startup bootstrap", silent=True)

    async def _reply_text(self, update: Update, text: str, **kwargs):
        last_error = None
        for _ in range(2):
            try:
                return await update.message.reply_text(text, **kwargs)
            except Exception as e:
                last_error = e
                self.telegram_logger.warning(f"Reply failed: {e}")
                await asyncio.sleep(0.8)
        raise last_error

    async def _send_text(self, chat_id: int, text: str, **kwargs):
        last_error = None
        for _ in range(2):
            try:
                return await self.app.bot.send_message(chat_id=chat_id, text=text, **kwargs)
            except Exception as e:
                last_error = e
                self.telegram_logger.warning(f"Send failed: {e}")
                await asyncio.sleep(0.8)
        raise last_error

    def _backend_busy(self) -> bool:
        return self.is_generating or (not self.queue.empty())

    def _extract_task_id(self, summary: str) -> Optional[str]:
        if not summary:
            return None
        match = re.search(r"\[([^\]]+)\]", summary)
        return match.group(1) if match else None

    def _log_maintenance(self, item: QueuedRequest, stage: str, **fields):
        if not item.source.startswith("scheduler"):
            return
        task_id = self._extract_task_id(item.summary) or "<none>"
        parts = [
            f"stage={stage}",
            f"request_id={item.request_id}",
            f"source={item.source}",
            f"task_id={task_id}",
            f"summary={item.summary!r}",
        ]
        for key, value in fields.items():
            parts.append(f"{key}={value!r}")
        self.maintenance_logger.info(" ".join(parts))

    def get_display_name(self) -> str:
        if self.config.extra and self.config.extra.get("display_name"):
            return self.config.extra["display_name"]
        return self.name

    def get_agent_emoji(self) -> str:
        if self.config.extra and self.config.extra.get("emoji"):
            return self.config.extra["emoji"]
        return "ðŸ¤–"

    def get_current_model(self) -> str:
        if self.backend_manager.current_backend:
            return getattr(self.backend_manager.current_backend.config, "model", "unknown")
        for backend in self.config.allowed_backends:
            if backend.get("engine") == self.config.active_backend:
                return backend.get("model", "unknown")
        return "unknown"

    def get_runtime_metadata(self) -> dict:
        return {
            "id": self.name,
            "name": self.name,
            "display_name": self.get_display_name(),
            "emoji": self.get_agent_emoji(),
            "engine": self.config.active_backend,
            "model": self.get_current_model(),
            "workspace_dir": str(self.workspace_dir),
            "transcript_path": str(self.transcript_log_path),
            "online": bool(self.backend_ready),
            "status": self._compute_status_string(),
            "type": self.config.type,
            "telegram_connected": self.telegram_connected,
            "channels": {
                "telegram": self.telegram_connected,
                "workbench": True,
                "whatsapp": self._get_whatsapp_connected(),
            },
        }

    def _compute_status_string(self) -> str:
        if not self.backend_ready:
            return "offline"
        if self.telegram_connected:
            return "online"
        return "local"

    def _get_whatsapp_connected(self) -> bool:
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            return False
        wa = getattr(orchestrator, "whatsapp", None)
        return wa is not None and getattr(wa, "_client", None) is not None

    def _get_active_skill_sections(self) -> list[tuple[str, str, str]]:
        if not self.skill_manager:
            return []
        return self.skill_manager.build_toggle_sections(self.workspace_dir)

    def _arm_session_primer(self, context_line: str):
        primer = build_agent_fyi_primer(self.agent_fyi_path, context_line=context_line)
        if primer:
            self._pending_session_primer = primer

    def _load_runtime_session_state(self) -> dict:
        if not self.runtime_session_path.exists():
            return {}
        try:
            return json.loads(self.runtime_session_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_runtime_session_state(self, payload: dict):
        self.runtime_session_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )

    def _mark_runtime_started(self):
        state = self._load_runtime_session_state()
        state["last_started_at"] = datetime.now().isoformat()
        state["clean_shutdown"] = False
        self._save_runtime_session_state(state)

    def _mark_runtime_shutdown(self, clean: bool):
        state = self._load_runtime_session_state()
        state["last_stopped_at"] = datetime.now().isoformat()
        state["clean_shutdown"] = bool(clean)
        self._save_runtime_session_state(state)

    def prepare_post_start_state(self):
        previous = self._load_runtime_session_state()
        unexpected_restart = bool(previous) and not previous.get("clean_shutdown", True)
        self._mark_runtime_started()
        if not self.skill_manager:
            return
        active = self.skill_manager.get_active_toggle_ids(self.workspace_dir)
        if "recall" not in active or not unexpected_restart:
            return
        context_block, exchange_count, word_count = self.handoff_builder.build_recent_context_block(
            max_rounds=10,
            max_words=6000,
        )
        if exchange_count <= 0 or not context_block:
            return
        self._pending_auto_recall_context = (
            "This session is recovering from an unexpected interruption. Restore recent continuity from the bridge-managed transcript below and use it as background context only.\n\n"
            f"{context_block}"
        )
        self._arm_session_primer(
            f"Unexpected restart detected. Recall mode is ON, so restore the last {exchange_count} exchanges ({word_count} words) once before continuing."
        )

    def _build_fyi_request_prompt(self, prompt_text: str = "") -> str:
        primer = build_agent_fyi_primer(
            self.agent_fyi_path,
            context_line="This is an explicit FYI refresh. Re-orient to the local bridge environment before responding.",
        )
        request = (
            prompt_text.strip()
            if prompt_text.strip()
            else "Acknowledge the AGENT FYI catalog and briefly summarize the key bridge systems, commands, and capabilities you should remember."
        )
        if not primer:
            return request
        return f"{primer}\n\n--- NEW REQUEST ---\n{request}"

    def _consume_session_primer(self, item: QueuedRequest) -> str:
        if item.source.startswith("scheduler") or item.source.startswith("bridge:"):
            return item.prompt
        if item.silent:
            return item.prompt
        sections = []
        if self._pending_session_primer:
            sections.append(self._pending_session_primer)
            self._pending_session_primer = None
        if self._pending_auto_recall_context:
            sections.append(f"--- AUTO RECALL ---\n{self._pending_auto_recall_context}")
            self._pending_auto_recall_context = None
        if not sections:
            return item.prompt
        return "\n\n".join(sections + [item.prompt])

    def _extract_json_object(self, text: str) -> dict | None:
        raw = (text or "").strip()
        if not raw:
            return None
        candidates = [raw]
        match = re.search(r"\{[\s\S]*\}", raw)
        if match:
            candidates.insert(0, match.group(0))
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except Exception:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    def _fallback_park_summary(
        self,
        context_block: str,
        last_user_text: str,
        last_assistant_text: str,
        title_override: str | None = None,
    ) -> dict[str, str]:
        title = (title_override or _safe_excerpt(last_user_text or "Parked topic", 48)).strip() or "Parked topic"
        short = _safe_excerpt(last_user_text or last_assistant_text or title, 140)
        long_summary = _safe_excerpt(context_block or short, 1600)
        return {
            "title": title,
            "summary_short": short,
            "summary_long": long_summary,
        }

    def _build_park_summary_prompt(
        self,
        context_block: str,
        last_user_text: str,
        last_assistant_text: str,
        title_override: str | None = None,
    ) -> str:
        override_line = (
            f'User preferred title: "{title_override.strip()}"\n'
            if title_override and title_override.strip()
            else ""
        )
        return (
            "SYSTEM: You are preparing a parked conversation record for later resume.\n"
            "Return JSON only with keys: title, summary_short, summary_long.\n"
            "Rules:\n"
            "- title: 3-8 words, concrete, no numbering\n"
            "- summary_short: one sentence, under 140 chars\n"
            "- summary_long: one detailed paragraph covering goal, decisions, unresolved work, and next step\n"
            "- Do not include markdown fences or extra commentary\n\n"
            f"{override_line}"
            "--- CURRENT TOPIC CONTEXT ---\n"
            f"{context_block}\n\n"
            "--- LAST USER MESSAGE ---\n"
            f"{last_user_text or '(none)'}\n\n"
            "--- LAST ASSISTANT MESSAGE ---\n"
            f"{last_assistant_text or '(none)'}\n\n"
            "--- OUTPUT FORMAT ---\n"
            '{"title":"...","summary_short":"...","summary_long":"..."}'
        )

    async def _summarize_current_topic_for_parking(self, title_override: str | None = None) -> dict[str, Any] | None:
        context_block, exchange_count, _ = self.handoff_builder.build_recent_context_block(
            max_rounds=12,
            max_words=4500,
        )
        if exchange_count <= 0 or not context_block:
            return None

        recent_rounds = self.handoff_builder.get_recent_rounds(max_rounds=3)
        last_user_text = ""
        last_assistant_text = ""
        last_exchange_text = ""
        if recent_rounds:
            last_round = recent_rounds[-1]
            lines = []
            for entry in last_round:
                role = str(entry.get("role", "")).upper()
                text = (entry.get("text") or "").strip()
                if not text:
                    continue
                lines.append(f"{role}: {text}")
                if entry.get("role") == "user":
                    last_user_text = text
                elif entry.get("role") == "assistant":
                    last_assistant_text = text
            last_exchange_text = "\n".join(lines).strip()

        fallback = self._fallback_park_summary(
            context_block,
            last_user_text,
            last_assistant_text,
            title_override=title_override,
        )
        response = await self.backend_manager.generate_response(
            self._build_park_summary_prompt(
                context_block,
                last_user_text,
                last_assistant_text,
                title_override=title_override,
            ),
            request_id=f"park-{int(time.time())}",
            silent=True,
        )
        parsed = self._extract_json_object(response.text) if response and response.is_success else None
        if not parsed:
            parsed = fallback

        title = (title_override or parsed.get("title") or fallback["title"]).strip()
        summary_short = (parsed.get("summary_short") or fallback["summary_short"]).strip()
        summary_long = (parsed.get("summary_long") or fallback["summary_long"]).strip()
        return {
            "title": title or fallback["title"],
            "summary_short": summary_short or fallback["summary_short"],
            "summary_long": summary_long or fallback["summary_long"],
            "recent_context": context_block,
            "last_user_text": last_user_text,
            "last_assistant_text": last_assistant_text,
            "last_exchange_text": last_exchange_text,
        }

    def _format_parked_topics_text(self) -> str:
        topics = self.parked_topics.list_topics()
        if not topics:
            return (
                "Parked topics: none.\n\n"
                "Usage:\n"
                "/park - list parked topics\n"
                "/park chat [optional title] - park the current topic\n"
                "/park delete <slot> - delete a parked topic\n"
                "/load <slot> - restore a parked topic"
            )
        lines = ["Parked topics", ""]
        for topic in topics:
            slot_id = int(topic.get("slot_id", 0))
            title = topic.get("title") or f"Topic {slot_id}"
            short = topic.get("summary_short") or "(no short summary)"
            followup = topic.get("followup") or {}
            status = followup.get("status") or "scheduled"
            attempts = int(followup.get("attempts", 0))
            next_at = followup.get("next_at")
            suffix = f" | next {next_at}" if next_at else ""
            lines.append(f"[{slot_id}] {title}")
            lines.append(f"  {short}")
            lines.append(f"  reminders: {status} ({attempts}/3){suffix}")
        lines.extend(["", "Use /load <slot> to restore or /park delete <slot> to remove one."])
        return "\n".join(lines)

    def is_idle_for_proactive_message(self, min_idle_seconds: int = 900) -> bool:
        if self._backend_busy():
            return False
        last_user_ts = self.memory_store.get_last_user_turn_ts()
        if not last_user_ts:
            return True
        try:
            idle_for = (datetime.now() - datetime.fromisoformat(last_user_ts)).total_seconds()
        except Exception:
            return False
        return idle_for >= min_idle_seconds

    async def process_parked_topic_followups(self, now_dt: datetime | None = None):
        now_dt = now_dt or datetime.now()
        if not self.telegram_connected or not self.is_idle_for_proactive_message():
            return
        for topic in self.parked_topics.due_topics(now_dt):
            slot_id = int(topic.get("slot_id", 0))
            followup = topic.get("followup") or {}
            attempt = int(followup.get("attempts", 0)) + 1
            title = topic.get("title") or f"Topic {slot_id}"
            summary_short = topic.get("summary_short") or ""
            reminder_text = (
                f"Parked topic reminder [{slot_id}] {title}\n\n"
                f"{summary_short}\n\n"
                f"Do you still want to continue this topic?\n"
                f"Use /load {slot_id} to resume or /park delete {slot_id} to remove it.\n"
                f"Reminder {attempt}/3."
            )
            await self.send_long_message(
                chat_id=self._primary_chat_id(),
                text=reminder_text,
                request_id=f"park-reminder-{slot_id}-{attempt}",
                purpose="park-reminder",
            )
            self.parked_topics.record_followup_sent(slot_id, sent_at=now_dt)

    def _skills_by_type(self) -> dict[str, list[SkillDefinition]]:
        if not self.skill_manager:
            return {"action": [], "toggle": [], "prompt": []}
        return self.skill_manager.list_skills_by_type()

    def _mark_activity(self):
        self.last_activity_at = datetime.now()

    def _mark_success(self):
        self.last_success_at = datetime.now()
        self._mark_activity()

    def _mark_error(self, summary: str):
        self.last_error_at = datetime.now()
        self.last_error_summary = _safe_excerpt(summary or "", 180)
        self._mark_activity()

    def _format_age(self, value: datetime | None) -> str:
        if value is None:
            return "never"
        seconds = int((datetime.now() - value).total_seconds())
        if seconds < 60:
            return f"{seconds}s ago"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        return f"{seconds // 86400}d ago"

    def _process_info(self) -> str:
        current_backend = self.backend_manager.current_backend
        proc = getattr(current_backend, "current_proc", None) if current_backend else None
        if not proc:
            return "none"
        pid = getattr(proc, "pid", None)
        return f"alive (pid={pid})" if pid else "alive"

    def _job_counts(self) -> tuple[int, int]:
        if not self.skill_manager:
            return 0, 0
        heartbeat_count = sum(1 for job in self.skill_manager.list_jobs("heartbeat", agent_name=self.name) if job.get("enabled"))
        cron_count = sum(1 for job in self.skill_manager.list_jobs("cron", agent_name=self.name) if job.get("enabled"))
        return heartbeat_count, cron_count

    async def _send_voice_reply(self, chat_id: int, text: str, request_id: str) -> bool:
        # Guard: skip if Telegram not connected
        if not self.telegram_connected:
            return False
        try:
            asset = await self.voice_manager.synthesize_reply(self.name, request_id, text)
            if asset is None:
                return False
            max_attempts = 3
            last_error = None
            for attempt in range(1, max_attempts + 1):
                try:
                    with asset.ogg_path.open("rb") as f:
                        await self.app.bot.send_voice(chat_id=chat_id, voice=f)
                    self.telegram_logger.info(
                        f"Sent Telegram voice reply for request_id={request_id} "
                        f"(path={asset.ogg_path.name}, attempt={attempt})"
                    )
                    return True
                except TelegramTimedOut as e:
                    # TimedOut means the request may have reached Telegram but we didn't
                    # get an ack. Retrying risks sending a duplicate — don't retry.
                    self.telegram_logger.warning(
                        f"Voice reply timed out for {request_id} (not retrying to avoid duplicate): {e}"
                    )
                    raise
                except Exception as e:
                    last_error = e
                    if attempt >= max_attempts:
                        break
                    delay_s = float(attempt)
                    self.telegram_logger.warning(
                        f"Voice reply send attempt {attempt}/{max_attempts} failed for "
                        f"{request_id}: {e}. Retrying in {delay_s:.1f}s."
                    )
                    await asyncio.sleep(delay_s)
            raise last_error or RuntimeError("Unknown voice send failure")
        except Exception as e:
            self.error_logger.error(f"Voice reply failed for {request_id}: {e}")
            self._mark_error(f"Voice reply failed: {e}")
            return False

    def _build_status_text(self, detailed: bool = False) -> str:
        active_skills = sorted(self.skill_manager.get_active_toggle_ids(self.workspace_dir)) if self.skill_manager else []
        recall_on = "recall" in active_skills
        heartbeat_count, cron_count = self._job_counts()
        active_job = self.skill_manager.get_active_heartbeat_job(self.name) if self.skill_manager else None
        active_mode = "ON" if active_job and active_job.get("enabled") else "OFF"
        active_interval = (
            f"{max(1, int(active_job.get('interval_seconds', 600) // 60))} min"
            if active_job else
            "10 min"
        )
        current = self.current_request_meta or {}
        current_line = (
            f"{current.get('request_id')} • {current.get('source')} • {current.get('summary')}"
            if current else "none"
        )
        health_line = (
            f"⚠️ {self.last_error_summary} ({self._format_age(self.last_error_at)})"
            if self.last_error_summary else
            "✅ healthy"
        )
        # Channel status
        tg_status = "✓" if self.telegram_connected else "✗"
        wa_status = "✓" if self._get_whatsapp_connected() else "✗"
        channel_line = f"Telegram {tg_status} • WhatsApp {wa_status} • Workbench ✓"
        mode_str = getattr(self.backend_manager, "agent_mode", "flex")
        session_id_short = "none"
        if mode_str == "fixed" and getattr(self.backend_manager, "current_backend", None):
            sid = getattr(self.backend_manager.current_backend, "_session_id", None) or "none"
            session_id_short = sid[:8] + "…" if sid != "none" and len(sid) > 8 else sid
        lines = [
            f"🧠 {self.name}",
            f"🔀 Backend: {self.config.active_backend} • {self.get_current_model()} • mode: {mode_str} • sid: {session_id_short}",
            f"📶 Channels: {channel_line}",
            f"📡 Runtime: {'busy' if self.is_generating else 'idle'} • queue {self.queue.qsize()} • process {self._process_info()}",
            f"🧾 Current: {current_line}",
            f"🧠 Memory: skills {', '.join(active_skills) if active_skills else 'none'} • recall {'ON' if recall_on else 'OFF'} • FYI {'armed' if self._pending_session_primer else 'clear'}",
            f"🔔 Proactive: {active_mode} • every {active_interval} • hb {heartbeat_count} • cron {cron_count}",
            f"🩺 Health: {health_line}",
            f"🕒 Activity: last success {self._format_age(self.last_success_at)} • last activity {self._format_age(self.last_activity_at)}",
        ]
        if detailed:
            allowed = ", ".join(b["engine"] for b in self.config.allowed_backends)
            current_effort = self._get_current_effort() or "n/a"
            
            session_id = "none"
            if mode_str == "fixed" and getattr(self.backend_manager, "current_backend", None):
                session_id = getattr(self.backend_manager.current_backend, "_session_id", "none") or "none"
                
            lines.extend([
                "",
                f"📁 Workspace: {self.workspace_dir}",
                f"📝 Transcript: {self.transcript_log_path.name}",
                f"🚀 Started: {self.session_started_at.isoformat(timespec='seconds')}",
                f"🧩 Allowed Backends: {allowed}",
                f"🎛️ Effort: {current_effort}",
                f"⚙️ Mode: {mode_str} • Session ID: {session_id}",
                f"🔁 Retry Cache: prompt {'yes' if self.last_prompt else 'no'} • response {'yes' if self.last_response else 'no'}",
                f"🧷 Primers: FYI {'armed' if self._pending_session_primer else 'clear'} • auto-recall {'armed' if self._pending_auto_recall_context else 'clear'}",
                f"📚 Bridge Memory: {self.memory_store.get_stats()['turns']} turns • {self.memory_store.get_stats()['memories']} memories",
                f"📘 Handoff Files: recent {'yes' if self.recent_context_path.exists() else 'no'} • handoff {'yes' if self.handoff_path.exists() else 'no'}",
                f"🔍 Verbose: {'ON' if self._verbose else 'OFF'}",
                f"💭 Think: {'ON' if self._think else 'OFF'}",
                f"🕓 Last Switch: {self._format_age(self.last_backend_switch_at)}",
            ])
        else:
            lines.append("")
            lines.append("Use /status full for more detail.")
        return "\n".join(lines)

    def _skill_keyboard(self) -> InlineKeyboardMarkup:
        buttons = []
        grouped = self._skills_by_type()
        active_ids = self.skill_manager.get_active_toggle_ids(self.workspace_dir) if self.skill_manager else set()
        for skill_type in ("action", "toggle", "prompt"):
            for skill in grouped.get(skill_type, []):
                label = skill.id
                if skill.type == "toggle":
                    label = f"{skill.id} {'ON' if skill.id in active_ids else 'OFF'}"
                buttons.append([InlineKeyboardButton(label, callback_data=f"skill:show:{skill.id}")])
        return InlineKeyboardMarkup(buttons or [[InlineKeyboardButton("No skills", callback_data="skill:noop:none")]])

    def _skill_action_keyboard(self, skill: SkillDefinition) -> InlineKeyboardMarkup | None:
        buttons = []
        if skill.type == "toggle":
            buttons.append(
                [
                    InlineKeyboardButton("ON", callback_data=f"skill:toggle:{skill.id}:on"),
                    InlineKeyboardButton("OFF", callback_data=f"skill:toggle:{skill.id}:off"),
                ]
            )
        elif skill.type == "action" and skill.id not in {"cron", "heartbeat"}:
            buttons.append([InlineKeyboardButton("Run Now", callback_data=f"skill:run:{skill.id}")])
        elif skill.type == "prompt":
            buttons.append([InlineKeyboardButton("Show Usage", callback_data=f"skill:show:{skill.id}")])
        if skill.id in {"cron", "heartbeat"}:
            buttons.append([InlineKeyboardButton("Refresh Jobs", callback_data=f"skill:jobs:{skill.id}")])
        return InlineKeyboardMarkup(buttons) if buttons else None

    async def _render_skill_jobs(self, update_or_query, kind: str):
        from orchestrator.agent_runtime import _build_jobs_with_buttons
        text, markup = _build_jobs_with_buttons(self.name, self.skill_manager)
        if hasattr(update_or_query, "edit_message_text"):
            await update_or_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        else:
            await self._reply_text(update_or_query, text, parse_mode="HTML", reply_markup=markup)

    async def invoke_scheduler_skill(self, skill_id: str, args: str, task_id: str):
        if not self.skill_manager:
            self.error_logger.error(f"Scheduler skill invocation requested without skill manager: {skill_id}")
            return
        skill = self.skill_manager.get_skill(skill_id)
        if skill is None:
            self.error_logger.error(f"Unknown scheduler skill: {skill_id}")
            return
        if skill.type == "toggle":
            self.error_logger.error(f"Toggle skill cannot be scheduled: {skill_id}")
            return
        if skill.type == "action":
            ok, text = await self.skill_manager.run_action_skill(skill, self.workspace_dir, args=args)
            if ok and text:
                await self.send_long_message(
                    chat_id=self._primary_chat_id(),
                    text=text,
                    request_id=f"skill-{task_id}",
                    purpose="scheduler-skill",
                )
            elif text:
                self.error_logger.error(text)
            return
        prompt = self.skill_manager.build_prompt_for_skill(skill, args or "")
        if skill.backend:
            allowed = [b["engine"] for b in self.config.allowed_backends]
            if skill.backend not in allowed:
                self.error_logger.error(
                    f"Scheduled prompt skill {skill.id} targets disallowed backend {skill.backend}."
                )
                return
            if self.config.active_backend != skill.backend:
                switch_ok = await self.backend_manager.switch_backend(skill.backend)
                if not switch_ok:
                    self.error_logger.error(f"Failed to switch backend for scheduled skill {skill.id}.")
                    return
        await self.enqueue_request(
            chat_id=self._primary_chat_id(),
            prompt=prompt,
            source="scheduler-skill",
            summary=f"Skill Task [{task_id}]",
            silent=False,
        )

    def get_typing_placeholder(self) -> tuple[str, str | None]:
        extra = self.config.extra or {}
        text = extra.get("typing_message")
        parse_mode = extra.get("typing_parse_mode")
        if text:
            return text, parse_mode
        display_name = self.get_display_name()
        emoji = self.get_agent_emoji()
        return f"_{emoji}{display_name} is typing..._", constants.ParseMode.MARKDOWN

    def _build_media_prompt(self, media_kind: str, filename: str, caption: str = "", emoji: str = "") -> tuple[str, str]:
        kind = media_kind.lower()
        ext = Path(filename).suffix.lower()

        if kind == "document":
            if is_image_file(filename):
                prompt = f'User sent an image file "{filename}" (saved at {{local_path}}). View the image carefully and respond.'
                if caption:
                    prompt += f' Caption: "{caption}"'
                return prompt, caption or filename
            if ext == ".pdf":
                prompt = f'User sent a PDF document "{filename}" (saved at {{local_path}}). Extract the text, analyze the contents thoroughly, and respond.'
            elif ext in [".txt", ".md", ".csv", ".json", ".py", ".js", ".html"]:
                prompt = f'User sent a text/code file "{filename}" (saved at {{local_path}}). Read the raw contents carefully and respond.'
            else:
                prompt = f'User sent a document "{filename}" (saved at {{local_path}}). Attempt to read the file and respond.'
            if caption:
                prompt += f' Caption: "{caption}"'
            return prompt, filename

        if kind == "photo":
            prompt = "User sent a photo (saved at {local_path})."
            if caption:
                prompt += f' Caption: "{caption}"'
            prompt += " View the image and respond."
            return prompt, caption or filename

        if kind == "voice":
            return (
                "User sent a voice message (saved at {local_path}). Listen to the audio, transcribe it, and respond.",
                filename,
            )

        if kind == "audio":
            prompt = f'User sent an audio file "{filename}" (saved at {{local_path}}).'
            if caption:
                prompt += f' Caption: "{caption}"'
            prompt += " Listen to the audio and respond."
            return prompt, filename

        if kind == "video":
            prompt = f'User sent a video "{filename}" (saved at {{local_path}}).'
            if caption:
                prompt += f' Caption: "{caption}"'
            prompt += " Watch the video and respond."
            return prompt, filename

        if kind == "sticker":
            prompt = f"User sent a sticker (emoji: {emoji or ''}). React warmly."
            if caption:
                prompt += f' Caption: "{caption}"'
            return prompt, emoji or filename or "sticker"

        return f'User sent a file "{filename}" (saved at {{local_path}}). Read it if possible and respond.', filename

    async def enqueue_api_text(self, text: str, source: str = "api", deliver_to_telegram: bool = True):
        _print_user_message(self.name, text)
        return await self.enqueue_request(
            self._primary_chat_id(),
            text,
            source,
            _safe_excerpt(text),
            deliver_to_telegram=deliver_to_telegram,
        )

    async def enqueue_api_media(
        self,
        local_path: Path,
        media_kind: str,
        filename: str,
        caption: str = "",
        emoji: str = "",
        source: str = "api",
        deliver_to_telegram: bool = True,
    ):
        if media_kind.lower() in {"photo", "document"} and is_image_file(filename):
            local_path, filename = normalize_image_file(local_path, filename)
            media_kind = "photo"
        prompt, summary = self._build_media_prompt(media_kind, filename, caption=caption, emoji=emoji)
        rendered_prompt = prompt.replace("{local_path}", str(local_path))
        return await self.enqueue_request(
            self._primary_chat_id(),
            rendered_prompt,
            source,
            summary,
            deliver_to_telegram=deliver_to_telegram,
        )

    def bind_handlers(self):
        self.app.add_error_handler(self.handle_telegram_error)
        self.app.add_handler(CommandHandler("help", self._wrap_cmd("help", self.cmd_help)))
        self.app.add_handler(CommandHandler("start", self._wrap_cmd("start", self.cmd_start)))
        self.app.add_handler(CommandHandler("status", self._wrap_cmd("status", self.cmd_status)))
        self.app.add_handler(CommandHandler("sys", self._wrap_cmd("sys", self.cmd_sys)))
        self.app.add_handler(CommandHandler("credit", self._wrap_cmd("credit", self.cmd_credit)))
        self.app.add_handler(CommandHandler("voice", self._wrap_cmd("voice", self.cmd_voice)))
        self.app.add_handler(CommandHandler("whisper", self._wrap_cmd("whisper", self.cmd_whisper)))
        self.app.add_handler(CommandHandler("active", self._wrap_cmd("active", self.cmd_active)))
        self.app.add_handler(CommandHandler("fyi", self._wrap_cmd("fyi", self.cmd_fyi)))
        self.app.add_handler(CommandHandler("debug", self._wrap_cmd("debug", self.cmd_debug)))
        self.app.add_handler(CommandHandler("skill", self._wrap_cmd("skill", self.cmd_skill)))
        self.app.add_handler(CommandHandler("backend", self._wrap_cmd("backend", self.cmd_backend)))
        self.app.add_handler(CommandHandler("handoff", self._wrap_cmd("handoff", self.cmd_handoff)))
        self.app.add_handler(CommandHandler("park", self._wrap_cmd("park", self.cmd_park)))
        self.app.add_handler(CommandHandler("load", self._wrap_cmd("load", self.cmd_load)))
        self.app.add_handler(CommandHandler("model", self._wrap_cmd("model", self.cmd_model)))
        self.app.add_handler(CommandHandler("effort", self._wrap_cmd("effort", self.cmd_effort)))
        self.app.add_handler(CallbackQueryHandler(self.callback_model, pattern=r"^(model|backend|bmodel|effort|backend_menu)"))
        self.app.add_handler(CallbackQueryHandler(self.callback_voice, pattern=r"^voice:"))
        self.app.add_handler(CallbackQueryHandler(self.callback_start_agent, pattern=r"^startagent:"))
        self.app.add_handler(CallbackQueryHandler(self.callback_skill, pattern=r"^(skill|skilljob):"))
        self.app.add_handler(CallbackQueryHandler(self.callback_toggle, pattern=r"^tgl:"))
        self.app.add_handler(CommandHandler("mode", self._wrap_cmd("mode", self.cmd_mode)))
        self.app.add_handler(CommandHandler("new", self._wrap_cmd("new", self.cmd_new)))
        self.app.add_handler(CommandHandler("memory", self._wrap_cmd("memory", self.cmd_memory)))
        self.app.add_handler(CommandHandler("wipe", self._wrap_cmd("wipe", self.cmd_wipe)))
        self.app.add_handler(CommandHandler("reset", self._wrap_cmd("reset", self.cmd_reset)))
        self.app.add_handler(CommandHandler("clear", self._wrap_cmd("clear", self.cmd_clear)))
        self.app.add_handler(CommandHandler("stop", self._wrap_cmd("stop", self.cmd_stop)))
        self.app.add_handler(CommandHandler("terminate", self._wrap_cmd("terminate", self.cmd_terminate)))
        self.app.add_handler(CommandHandler("reboot", self._wrap_cmd("reboot", self.cmd_reboot)))
        self.app.add_handler(CommandHandler("retry", self._wrap_cmd("retry", self.cmd_retry)))
        self.app.add_handler(CommandHandler("verbose", self._wrap_cmd("verbose", self.cmd_verbose)))
        self.app.add_handler(CommandHandler("think", self._wrap_cmd("think", self.cmd_think)))
        self.app.add_handler(CommandHandler("jobs", self._wrap_cmd("jobs", self.cmd_jobs)))
        self.app.add_handler(CommandHandler("logo", self._wrap_cmd("logo", self.cmd_logo)))
        self.app.add_handler(CommandHandler("wa_on", self._wrap_cmd("wa_on", self.cmd_wa_on)))
        self.app.add_handler(CommandHandler("wa_off", self._wrap_cmd("wa_off", self.cmd_wa_off)))
        self.app.add_handler(CommandHandler("wa_send", self._wrap_cmd("wa_send", self.cmd_wa_send)))
        self.app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), self.handle_message))
        self.app.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
        self.app.add_handler(MessageHandler(filters.VOICE, self.handle_voice))
        self.app.add_handler(MessageHandler(filters.AUDIO, self.handle_audio))
        self.app.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))
        self.app.add_handler(MessageHandler(filters.VIDEO, self.handle_video))
        self.app.add_handler(MessageHandler(filters.Sticker.ALL, self.handle_sticker))

    async def handle_telegram_error(self, update: object, context):
        update_summary = "<no update>"
        if isinstance(update, Update):
            chat_id = update.effective_chat.id if update.effective_chat else "unknown"
            user_id = update.effective_user.id if update.effective_user else "unknown"
            message_id = update.effective_message.message_id if update.effective_message else "unknown"
            update_summary = f"chat_id={chat_id}, user_id={user_id}, message_id={message_id}"
        self.error_logger.error(
            f"Telegram update handler error ({update_summary}): {context.error}",
            exc_info=(type(context.error), context.error, context.error.__traceback__),
        )

    def handle_polling_error(self, error):
        from telegram.error import Conflict
        err_text = str(error) or "<no error message>"
        if isinstance(error, Conflict):
            self.error_logger.error(
                f"Telegram polling conflict for '{self.name}': another process is using this bot token. "
                f"Check for duplicate bridge/bridge-g-m instances running. ({err_text})"
            )
            return
        self.telegram_logger.warning(f"Polling error while fetching updates: {type(error).__name__}: {err_text}")
        if getattr(error, "__traceback__", None):
            self.error_logger.error(
                f"Telegram polling error: {type(error).__name__}: {err_text}",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def cmd_help(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        # Build help dynamically from the bot command list, filtered by command policy.
        cmds = self.get_bot_commands()
        enabled = [c for c in cmds if self._is_command_allowed(c.command)]
        disabled = sorted({c.command for c in cmds if not self._is_command_allowed(c.command)})

        lines = [f"Agent {self.name} ({getattr(self.config, 'type', 'flex')}) Commands", ""]
        for c in enabled:
            lines.append(f"/{c.command} - {c.description}")

        if disabled:
            lines.append("")
            lines.append("Disabled for this agent:")
            lines.append("  " + ", ".join(f"/{c}" for c in disabled))

        await self._reply_text(update, "\n".join(lines))

    def _startable_agent_keyboard(self) -> InlineKeyboardMarkup | None:
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            return None
        names = orchestrator.get_startable_agent_names(exclude_name=self.name)
        if not names:
            return None
        rows = [[InlineKeyboardButton(name, callback_data=f"startagent:{name}")] for name in names]
        rows.append([InlineKeyboardButton("ALL", callback_data="startagent:__all__")])
        return InlineKeyboardMarkup(rows)

    def _voice_keyboard(self) -> InlineKeyboardMarkup:
        rows = [
            [
                InlineKeyboardButton("Voice ON", callback_data="voice:toggle:on"),
                InlineKeyboardButton("Voice OFF", callback_data="voice:toggle:off"),
            ]
        ]
        active_alias = self.voice_manager.get_active_preset_alias()
        preset_buttons = []
        for alias, preset, available in self.voice_manager.get_voice_presets():
            base = preset.get("label") or alias
            label = f">> {base}" if alias == active_alias else base
            if available != "ready":
                label = f"{base} ({available})"
            preset_buttons.append(InlineKeyboardButton(label, callback_data=f"voice:use:{alias}"))
        for i in range(0, len(preset_buttons), 2):
            rows.append(preset_buttons[i:i + 2])
        rows.append([InlineKeyboardButton("Refresh", callback_data="voice:refresh:menu")])
        return InlineKeyboardMarkup(rows)

    async def cmd_start(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            await self._reply_text(update, "Dynamic lifecycle control is unavailable.")
            return
        arg = " ".join(context.args).strip().lower() if context.args else ""
        if arg == "all":
            names = orchestrator.get_startable_agent_names(exclude_name=self.name)
            if not names:
                await self._reply_text(update, "All agents are running.")
                return
            lines = []
            for name in names:
                ok, msg = await orchestrator.start_agent(name)
                lines.append(msg)
            await self._reply_text(update, "\n".join(lines))
            return
        keyboard = self._startable_agent_keyboard()
        if keyboard is None:
            await self._reply_text(update, "All agents are running.")
            return
        await self._reply_text(update, "Start another agent:", reply_markup=keyboard)

    async def callback_start_agent(self, update: Update, context: Any):
        query = update.callback_query
        if not self._is_authorized_user(query.from_user.id):
            return
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            await query.answer("Lifecycle control unavailable", show_alert=True)
            return
        _, agent_name = (query.data or "").split(":", 1)
        if agent_name == "__all__":
            await query.answer("Starting all agents...")
            names = orchestrator.get_startable_agent_names(exclude_name=self.name)
            lines = []
            for name in names:
                ok, msg = await orchestrator.start_agent(name)
                lines.append(msg)
            result_text = "\n".join(lines) if lines else "All agents are already running."
            await query.edit_message_text(result_text)
            return
        await query.answer(f"Starting {agent_name}...")
        ok, message = await orchestrator.start_agent(agent_name)
        await query.edit_message_text(message, reply_markup=self._startable_agent_keyboard())

    async def callback_voice(self, update: Update, context: Any):
        query = update.callback_query
        if not self._is_authorized_user(query.from_user.id):
            return
        parts = (query.data or "").split(":", 2)
        action = parts[1] if len(parts) > 1 else "refresh"
        value = parts[2] if len(parts) > 2 else ""
        message = None
        try:
            if action == "toggle":
                message = self.voice_manager.set_enabled(value == "on")
            elif action == "use":
                message = self.voice_manager.apply_voice_preset(value)
        except Exception as e:
            await query.answer(str(e), show_alert=True)
            return

        text = self.voice_manager.voice_menu_text()
        if message:
            text = f"{text}\n\n{message}"
        await query.edit_message_text(text, reply_markup=self._voice_keyboard())
        await query.answer()

    # ── toggle callback ──────────────────────────────────────────────────────────
    # Handles: tgl:verbose:on/off, tgl:think:on/off, tgl:mode:fixed/flex,
    #          tgl:retry:response/prompt, tgl:whisper:small/medium/large,
    #          tgl:active:on/off/<minutes>, tgl:reboot:min/max/same/<name>
    async def callback_toggle(self, update: Update, context: Any):
        query = update.callback_query
        if not self._is_authorized_user(query.from_user.id):
            await query.answer()
            return
        parts = (query.data or "").split(":", 2)
        if len(parts) < 3:
            await query.answer()
            return
        _, target, value = parts[0], parts[1], parts[2]

        if target == "verbose":
            self._verbose = value == "on"
            _f = self.workspace_dir / ".verbose"
            if self._verbose:
                _f.touch()
            else:
                _f.unlink(missing_ok=True)
            state = "ON 🔍" if self._verbose else "OFF"
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ ON" if self._verbose else "ON", callback_data="tgl:verbose:on"),
                InlineKeyboardButton("✅ OFF" if not self._verbose else "OFF", callback_data="tgl:verbose:off"),
            ]])
            await query.edit_message_text(f"Verbose mode: {state}", reply_markup=markup)
            await query.answer(f"Verbose {state}")

        elif target == "think":
            self._think = value == "on"
            _f = self.workspace_dir / ".think"
            if self._think:
                _f.touch()
            else:
                _f.unlink(missing_ok=True)
            state = "ON 💭" if self._think else "OFF"
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ ON" if self._think else "ON", callback_data="tgl:think:on"),
                InlineKeyboardButton("✅ OFF" if not self._think else "OFF", callback_data="tgl:think:off"),
            ]])
            await query.edit_message_text(f"Thinking display: {state}", reply_markup=markup)
            await query.answer(f"Think {state}")

        elif target == "mode":
            current = self.backend_manager.agent_mode
            if value == current:
                await query.answer(f"Already in {current} mode.")
                return
            self.backend_manager.agent_mode = value
            self.backend_manager._save_state()
            backend = self.backend_manager.current_backend
            if value == "fixed":
                if hasattr(backend, "set_session_mode"):
                    backend.set_session_mode(True)
                detail = "CLI session persists · /backend disabled"
            else:
                if hasattr(backend, "set_session_mode"):
                    backend.set_session_mode(False)
                detail = "Full context injection · /backend enabled"
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Fixed" if value == "fixed" else "Fixed", callback_data="tgl:mode:fixed"),
                InlineKeyboardButton("✅ Flex" if value == "flex" else "Flex", callback_data="tgl:mode:flex"),
            ]])
            await query.edit_message_text(f"Mode: <b>{value}</b>\n{detail}", parse_mode="HTML", reply_markup=markup)
            await query.answer(f"Switched to {value}")

        elif target == "retry":
            chat_id = query.message.chat_id
            await query.answer(f"Retrying {value}...")
            if value == "response":
                if self.last_response:
                    await self.send_long_message(chat_id=self.last_response["chat_id"], text=self.last_response["text"],
                                                  request_id=self.last_response.get("request_id"), purpose="retry-response")
                else:
                    transcript_text = self._load_last_text_from_transcript("assistant")
                    if transcript_text:
                        await self.send_long_message(chat_id=chat_id, text=transcript_text, purpose="retry-response")
                    elif self.last_prompt:
                        await self.enqueue_request(self.last_prompt.chat_id, self.last_prompt.prompt, "retry", "Retry request")
                    else:
                        await query.answer("Nothing to retry.", show_alert=True)
            else:  # prompt
                if self.last_prompt:
                    await self.enqueue_request(self.last_prompt.chat_id, self.last_prompt.prompt, "retry", "Retry request")
                else:
                    transcript_text = self._load_last_text_from_transcript("user")
                    if transcript_text:
                        await self.enqueue_request(chat_id, transcript_text, "retry", "Retry request")
                    else:
                        await query.answer("No previous prompt.", show_alert=True)

        elif target == "whisper":
            from orchestrator.voice_transcriber import get_transcriber
            mapping = {"small": "small", "medium": "medium", "large": "large-v3"}
            new_size = mapping.get(value)
            if not new_size:
                await query.answer("Unknown size.", show_alert=True)
                return
            transcriber = get_transcriber()
            transcriber.model_size = new_size
            transcriber._model = None
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ small" if value == "small" else "small", callback_data="tgl:whisper:small"),
                InlineKeyboardButton("✅ medium" if value == "medium" else "medium", callback_data="tgl:whisper:medium"),
                InlineKeyboardButton("✅ large" if value == "large" else "large", callback_data="tgl:whisper:large"),
            ]])
            await query.edit_message_text(f"Whisper model: <b>{new_size}</b>", parse_mode="HTML", reply_markup=markup)
            await query.answer(f"Set to {new_size}")

        elif target == "active":
            if not self.skill_manager:
                await query.answer("Skill manager not available.", show_alert=True)
                return
            if value == "off":
                _, msg = self.skill_manager.set_active_heartbeat(self.name, enabled=False)
            elif value == "on":
                _, msg = self.skill_manager.set_active_heartbeat(self.name, enabled=True,
                                                                  minutes=self.skill_manager.ACTIVE_HEARTBEAT_DEFAULT_MINUTES)
            else:
                try:
                    mins = int(value)
                    _, msg = self.skill_manager.set_active_heartbeat(self.name, enabled=True, minutes=mins)
                except ValueError:
                    await query.answer("Invalid value.", show_alert=True)
                    return
            status = self.skill_manager.describe_active_heartbeat(self.name)
            markup = self._active_keyboard()
            await query.edit_message_text(f"{status}\n\n{msg}", reply_markup=markup)
            await query.answer()

        elif target == "reboot":
            orchestrator = getattr(self, "orchestrator", None)
            if orchestrator is None:
                await query.answer("Hot restart unavailable.", show_alert=True)
                return
            if value == "min":
                mode, label = "min", f"Restarting only <b>{self.name}</b>..."
            elif value == "max":
                mode, label = "max", "Restarting all active agents..."
            elif value.isdigit():
                all_names = orchestrator.configured_agent_names()
                num = int(value)
                mode, label = "number", f"Restarting agent #{num} (<b>{all_names[num - 1]}</b>)..."
            else:
                mode, label = "same", "Restarting all running agents..."
            await query.edit_message_text(label, parse_mode="HTML")
            await query.answer()
            orchestrator.request_restart(mode=mode, agent_name=self.name,
                                          agent_number=int(value) if value.isdigit() else None)
        else:
            await query.answer()

    def _active_keyboard(self) -> InlineKeyboardMarkup:
        default_min = getattr(self.skill_manager, "ACTIVE_HEARTBEAT_DEFAULT_MINUTES", 10) if self.skill_manager else 10
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("ON", callback_data="tgl:active:on"),
                InlineKeyboardButton("OFF", callback_data="tgl:active:off"),
            ],
            [
                InlineKeyboardButton("10m", callback_data="tgl:active:10"),
                InlineKeyboardButton("30m", callback_data="tgl:active:30"),
                InlineKeyboardButton("60m", callback_data="tgl:active:60"),
            ],
        ])

    # ── lifecycle commands ───────────────────────────────────────────────────────
    async def cmd_terminate(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            await self._reply_text(update, "Dynamic lifecycle control is unavailable.")
            return
        await self._reply_text(update, "Shutting down.")
        asyncio.create_task(orchestrator.stop_agent(self.name))

    async def cmd_reboot(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            await self._reply_text(update, "Hot restart is unavailable.")
            return
        arg = " ".join(context.args).strip().lower() if context.args else ""
        if not arg or arg == "help":
            all_names = orchestrator.configured_agent_names()
            lines = ["<b>Reboot</b> — select target:"]
            for i, name in enumerate(all_names, 1):
                running = name in {rt.name for rt in orchestrator.runtimes}
                marker = "●" if running else "○"
                lines.append(f"  {i}. {marker} {name}")
            rows = [
                [
                    InlineKeyboardButton("This bot", callback_data="tgl:reboot:min"),
                    InlineKeyboardButton("All active", callback_data="tgl:reboot:max"),
                    InlineKeyboardButton("Running only", callback_data="tgl:reboot:same"),
                ]
            ]
            for i, name in enumerate(all_names, 1):
                rows.append([InlineKeyboardButton(f"#{i} {name}", callback_data=f"tgl:reboot:{i}")])
            markup = InlineKeyboardMarkup(rows)
            await self._reply_text(update, "\n".join(lines), parse_mode="HTML", reply_markup=markup)
            return
        if arg == "min":
            mode, label = "min", f"Restarting only <b>{self.name}</b>..."
        elif arg == "max":
            mode, label = "max", "Restarting all active agents..."
        elif arg.isdigit():
            num = int(arg)
            all_names = orchestrator.configured_agent_names()
            if num < 1 or num > len(all_names):
                await self._reply_text(update, f"Invalid agent number. Use 1–{len(all_names)}. /reboot help to list.")
                return
            mode, label = "number", f"Restarting agent #{num} (<b>{all_names[num - 1]}</b>)..."
        else:
            mode, label = "same", "Restarting all running agents..."
        await self._reply_text(update, label, parse_mode="HTML")
        orchestrator.request_restart(mode=mode, agent_name=self.name, agent_number=int(arg) if arg.isdigit() else None)

    async def cmd_wa_on(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            await self._reply_text(update, "WhatsApp lifecycle control is unavailable.")
            return
        ok, message = await orchestrator.start_whatsapp_transport(persist_enabled=True)
        await self._reply_text(update, message)

    async def cmd_wa_off(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            await self._reply_text(update, "WhatsApp lifecycle control is unavailable.")
            return
        ok, message = await orchestrator.stop_whatsapp_transport(persist_enabled=True)
        await self._reply_text(update, message)

    async def cmd_wa_send(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            await self._reply_text(update, "WhatsApp send control is unavailable.")
            return
        args = context.args or []
        if len(args) < 2:
            await self._reply_text(update, "Usage: /wa_send <+number> <message>")
            return
        phone_number = args[0].strip()
        text = " ".join(args[1:]).strip()
        if not text:
            await self._reply_text(update, "Usage: /wa_send <+number> <message>")
            return
        ok, message = await orchestrator.send_whatsapp_text(phone_number, text)
        await self._reply_text(update, message)

    async def cmd_fyi(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        prompt = self._build_fyi_request_prompt(" ".join(context.args or []))
        await self._reply_text(update, "Refreshing AGENT FYI...")
        await self.enqueue_request(
            update.effective_chat.id,
            prompt,
            "fyi",
            "AGENT FYI refresh",
        )


    async def cmd_sys(self, update, context):
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = [a.strip() for a in (context.args or []) if a.strip()]
        mgr = self.sys_prompt_manager

        if not args:
            await update.message.reply_text(mgr.display_all(), parse_mode="Markdown")
            return

        # /sys output <n> — return raw content of slot, no state change
        if args[0].lower() == "output":
            slot = args[1] if len(args) > 1 else ""
            if slot not in mgr.SLOTS:
                await update.message.reply_text("Usage: /sys output <1-10>")
                return
            text = mgr._slot(slot).get("text", "")
            await update.message.reply_text(text if text else "(empty)", parse_mode=None)
            return

        slot = args[0]
        if slot not in mgr.SLOTS:
            await update.message.reply_text(f"Invalid slot '{slot}'. Use 1-10.")
            return

        if len(args) == 1:
            await update.message.reply_text(mgr.display_slot(slot))
            return

        sub = args[1].lower()

        if sub == "on":
            await update.message.reply_text(mgr.activate(slot))
        elif sub == "off":
            await update.message.reply_text(mgr.deactivate(slot))
        elif sub == "delete":
            await update.message.reply_text(mgr.delete(slot))
        elif sub == "save":
            text = " ".join(args[2:])
            if not text:
                await update.message.reply_text("Usage: /sys <slot> save <message>")
                return
            await update.message.reply_text(mgr.save(slot, text))
        elif sub == "replace":
            text = " ".join(args[2:])
            if not text:
                await update.message.reply_text("Usage: /sys <slot> replace <message>")
                return
            await update.message.reply_text(mgr.replace(slot, text))
        else:
            await update.message.reply_text(
                "Usage:\n/sys - show all slots\n/sys <n> - show slot\n"
                "/sys <n> on|off|delete\n/sys <n> save <msg>\n/sys <n> replace <msg>\n"
                "/sys output <n> - return raw content of slot"
            )

    async def cmd_credit(self, update, context):
        if not self._is_authorized_user(update.effective_user.id):
            return
        backend = self.backend_manager.current_backend
        if not backend or not hasattr(backend, "get_key_info"):
            await update.message.reply_text("Credit info is only available for OpenRouter backends.")
            return
        key_info = await backend.get_key_info()
        if not key_info:
            await update.message.reply_text("Failed to fetch credit info.")
            return
        data = key_info.get("data", {})
        label = data.get("label", "unknown")
        usage = data.get("usage", "unknown")
        limit = data.get("limit", "unknown")
        limit_remaining = data.get("limit_remaining", "unknown")
        is_free_tier = data.get("is_free_tier", False)
        await update.message.reply_text(
            f"OpenRouter key: {label}\n"
            f"Usage: {usage}\n"
            f"Limit: {limit}\n"
            f"Remaining: {limit_remaining}\n"
            f"Free tier: {is_free_tier}"
        )
    async def cmd_voice(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = [a.strip() for a in (context.args or []) if a.strip()]
        if not args or args[0].lower() == "status":
            await self._reply_text(
                update,
                self.voice_manager.voice_menu_text(),
                reply_markup=self._voice_keyboard(),
            )
            return
        mode = args[0].lower()
        if mode in {"providers", "list"}:
            await self._reply_text(update, self.voice_manager.provider_hints())
            return
        if mode in {"voices", "menu"}:
            await self._reply_text(
                update,
                self.voice_manager.voice_menu_text(),
                reply_markup=self._voice_keyboard(),
            )
            return
        if mode == "use":
            if len(args) == 1:
                await self._reply_text(update, "Usage: /voice use <alias>")
                return
            try:
                await self._reply_text(update, self.voice_manager.apply_voice_preset(args[1]))
            except Exception as e:
                await self._reply_text(update, str(e))
            return
        if mode == "provider":
            if len(args) == 1:
                await self._reply_text(update, f"Current voice provider: {self.voice_manager.get_provider_name()}")
                return
            try:
                await self._reply_text(update, self.voice_manager.set_provider(args[1]))
            except Exception as e:
                await self._reply_text(update, str(e))
            return
        if mode == "name":
            if len(args) == 1:
                await self._reply_text(update, "Usage: /voice name <voice-name>")
                return
            await self._reply_text(update, self.voice_manager.set_voice_name(" ".join(args[1:])))
            return
        if mode == "rate":
            if len(args) == 1:
                await self._reply_text(update, "Usage: /voice rate <integer>")
                return
            try:
                await self._reply_text(update, self.voice_manager.set_rate(int(args[1])))
            except ValueError:
                await self._reply_text(update, "Voice rate must be an integer.")
            return
        if mode == "on":
            await self._reply_text(update, self.voice_manager.set_enabled(True))
            return
        if mode == "off":
            await self._reply_text(update, self.voice_manager.set_enabled(False))
            return
        await self._reply_text(update, "Usage: /voice [status|on|off|voices|use <alias>|providers|provider <name>|name <voice>|rate <n>]")

    async def cmd_whisper(self, update: Update, context: Any):
        """Set the local voice transcription model size.

        Usage:
          /whisper                -> show current
          /whisper small          -> faster, less accurate
          /whisper medium         -> balanced
          /whisper large          -> best accuracy (largest download/slowest)

        Notes:
        - This controls **local** transcription of Telegram voice/audio messages.
        - Changes take effect on next transcription; the model will be (re)loaded lazily.
        """
        if not self._is_authorized_user(update.effective_user.id):
            return

        from orchestrator.voice_transcriber import get_transcriber

        args = [a.strip().lower() for a in (context.args or []) if a.strip()]
        transcriber = get_transcriber()

        if not args:
            cur = transcriber.model_size
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ small" if cur == "small" else "small", callback_data="tgl:whisper:small"),
                InlineKeyboardButton("✅ medium" if cur == "medium" else "medium", callback_data="tgl:whisper:medium"),
                InlineKeyboardButton("✅ large" if cur.startswith("large") else "large", callback_data="tgl:whisper:large"),
            ]])
            await self._reply_text(update, f"Whisper model: <b>{cur}</b>", parse_mode="HTML", reply_markup=markup)
            return

        value = args[0]
        mapping = {
            "small": "small",
            "medium": "medium",
            # In Whisper naming, the common best-performing option is large-v3.
            "large": "large-v3",
            "large-v3": "large-v3",
        }
        if value not in mapping:
            await self._reply_text(update, "Usage: /whisper [small|medium|large]")
            return

        new_size = mapping[value]
        # Reset model so it reloads with the new size on next use.
        transcriber.model_size = new_size
        transcriber._model = None

        await self._reply_text(
            update,
            f"✅ Whisper model set to: {new_size}. It will load on the next voice message.",
        )

    async def _invoke_prompt_skill_from_command(self, update: Update, skill_id: str, args: list[str]):
        if not self.skill_manager:
            await self._reply_text(update, "Skill system is not configured.")
            return
        skill = self.skill_manager.get_skill(skill_id)
        if skill is None:
            await self._reply_text(update, f"Unknown skill: {skill_id}")
            return
        if skill.type != "prompt":
            await self._reply_text(update, f"Skill '{skill_id}' is not a prompt skill.")
            return
        prompt_text = " ".join(args or []).strip()
        if not prompt_text:
            await self._reply_text(update, f"Usage: /{skill_id} <prompt>")
            return
        if skill.backend:
            allowed = [b["engine"] for b in self.config.allowed_backends]
            if skill.backend not in allowed:
                await self._reply_text(
                    update,
                    f"Skill '{skill.id}' targets {skill.backend}, which is not allowed for this flex agent.",
                )
                return
            if self.config.active_backend != skill.backend:
                await self._reply_text(update, f"Switching backend to {skill.backend} for skill {skill.id}...")
                success, message = await self._switch_backend_mode(
                    update.effective_chat.id,
                    skill.backend,
                    with_context=bool(self._get_active_skill_sections()),
                )
                if not success:
                    await self._send_text(update.effective_chat.id, message)
                    return
        prompt = self.skill_manager.build_prompt_for_skill(skill, prompt_text)
        await self._reply_text(update, f"Running skill {skill.id}...")
        await self.enqueue_request(
            update.effective_chat.id,
            prompt,
            f"skill:{skill.id}",
            f"Skill {skill.id}",
        )

    async def cmd_debug(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        await self._invoke_prompt_skill_from_command(update, "debug", list(context.args or []))

    async def cmd_skill(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        if not self.skill_manager:
            await self._reply_text(update, "Skill system is not configured.")
            return

        args = list(context.args or [])
        if not args:
            await self._reply_text(update, "Skills", reply_markup=self._skill_keyboard())
            return

        sub = args[0].strip().lower()
        if sub == "help":
            grouped = self._skills_by_type()
            lines = ["Skills", ""]
            for skill_type in ("action", "toggle", "prompt"):
                entries = grouped.get(skill_type, [])
                if not entries:
                    continue
                lines.append(skill_type.upper())
                for skill in entries:
                    lines.append(f"- {skill.id}: {skill.description}")
                lines.append("")
            await self._reply_text(update, "\n".join(lines).strip())
            return

        skill = self.skill_manager.get_skill(sub)
        if skill is None:
            await self._reply_text(update, f"Unknown skill: {sub}")
            return

        rest = " ".join(args[1:]).strip()
        if skill.id in {"cron", "heartbeat"} and not rest:
            await self._render_skill_jobs(update, skill.id)
            return

        if skill.type == "toggle":
            if rest.lower() in {"on", "off"}:
                _, message = self.skill_manager.set_toggle_state(
                    self.workspace_dir,
                    skill.id,
                    enabled=(rest.lower() == "on"),
                )
                await self._reply_text(update, message, reply_markup=self._skill_action_keyboard(skill))
                return
            await self._reply_text(
                update,
                self.skill_manager.describe_skill(skill, self.workspace_dir),
                reply_markup=self._skill_action_keyboard(skill),
            )
            return

        if skill.type == "action":
            _, message = await self.skill_manager.run_action_skill(skill, self.workspace_dir, args=rest)
            await self.send_long_message(
                chat_id=update.effective_chat.id,
                text=message,
                request_id=f"skill-{skill.id}",
                purpose="skill-action",
            )
            return

        if not rest:
            await self._reply_text(
                update,
                self.skill_manager.describe_skill(skill, self.workspace_dir),
                reply_markup=self._skill_action_keyboard(skill),
            )
            return

        if skill.backend:
            allowed = [b["engine"] for b in self.config.allowed_backends]
            if skill.backend not in allowed:
                await self._reply_text(
                    update,
                    f"Skill '{skill.id}' targets {skill.backend}, which is not allowed for this flex agent.",
                )
                return
            if self.config.active_backend != skill.backend:
                await self._reply_text(update, f"Switching backend to {skill.backend} for skill {skill.id}...")
                success, message = await self._switch_backend_mode(
                    update.effective_chat.id,
                    skill.backend,
                    with_context=bool(self._get_active_skill_sections()),
                )
                if not success:
                    await self._send_text(update.effective_chat.id, message)
                    return
        prompt = self.skill_manager.build_prompt_for_skill(skill, rest)
        await self._reply_text(update, f"Running skill {skill.id}...")
        await self.enqueue_request(
            update.effective_chat.id,
            prompt,
            f"skill:{skill.id}",
            f"Skill {skill.id}",
        )

    async def callback_skill(self, update: Update, context: Any):
        query = update.callback_query
        if not self._is_authorized_user(query.from_user.id):
            return
        data = query.data or ""
        if data == "skill:noop:none":
            await query.answer()
            return
        if data.startswith("skilljob:"):
            _, kind, action, task_id, value = data.split(":", 4)
            if action == "toggle":
                ok, message = self.skill_manager.set_job_enabled(kind, task_id, enabled=(value == "on"))
                await query.answer(message, show_alert=not ok)
                await self._render_skill_jobs(query, kind)
                return
            if action == "delete":
                ok, message = self.skill_manager.delete_job(kind, task_id)
                await query.answer(message, show_alert=not ok)
                await self._render_skill_jobs(query, kind)
                return
            if action == "run":
                job = self.skill_manager.get_job(kind, task_id)
                if not job:
                    await query.answer("Unknown job", show_alert=True)
                    return
                await query.answer("Running job now")
                await self._run_job_now(job)
                return
        if data.startswith("skill:"):
            _, action, skill_id, *rest = data.split(":")
            skill = self.skill_manager.get_skill(skill_id)
            if skill is None:
                await query.answer("Unknown skill", show_alert=True)
                return
            if action == "show":
                if skill.id in {"cron", "heartbeat"}:
                    await self._render_skill_jobs(query, skill.id)
                else:
                    await query.edit_message_text(
                        self.skill_manager.describe_skill(skill, self.workspace_dir),
                        reply_markup=self._skill_action_keyboard(skill),
                    )
                await query.answer()
                return
            if action == "toggle" and rest:
                enabled = rest[0] == "on"
                _, message = self.skill_manager.set_toggle_state(self.workspace_dir, skill.id, enabled=enabled)
                await query.edit_message_text(message, reply_markup=self._skill_action_keyboard(skill))
                await query.answer()
                return
            if action == "run":
                ok, message = await self.skill_manager.run_action_skill(skill, self.workspace_dir)
                await query.answer("Skill executed" if ok else "Skill failed", show_alert=not ok)
                await self.send_long_message(
                    chat_id=query.message.chat_id,
                    text=message,
                    request_id=f"skill-{skill.id}",
                    purpose="skill-action",
                )
                return
            if action == "jobs":
                await self._render_skill_jobs(query, skill.id)
                await query.answer()
                return
        await query.answer()

    async def _run_job_now(self, job: dict[str, Any]):
        action = job.get("action", "enqueue_prompt")
        if action == "export_transcript":
            await self.send_long_message(
                chat_id=self._primary_chat_id(),
                text="Transcript export is only implemented for fixed agents.",
                request_id=f"job-{job.get('id')}",
                purpose="skill-job-run",
            )
            return
        if action.startswith("skill:"):
            await self.invoke_scheduler_skill(
                skill_id=action.split(":", 1)[1],
                args=job.get("args", "") or job.get("prompt", ""),
                task_id=job.get("id", "manual"),
            )
            return
        prompt = job.get("prompt", "")
        if not prompt.strip():
            await self.send_long_message(
                chat_id=self._primary_chat_id(),
                text=f"Job {job.get('id')} has no prompt.",
                request_id=f"job-{job.get('id')}",
                purpose="skill-job-run",
            )
            return
        summary_prefix = "Heartbeat Task" if "interval_seconds" in job else "Cron Task"
        await self.enqueue_request(
            chat_id=self._primary_chat_id(),
            prompt=prompt,
            source="scheduler",
            summary=f"{summary_prefix} [{job.get('id')}]",
        )

    async def cmd_status(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        detailed = bool(context.args and context.args[0].strip().lower() in {"full", "all", "more"})
        await self._reply_text(update, self._build_status_text(detailed=detailed))

    async def cmd_verbose(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = [a.strip().lower() for a in (context.args or []) if a.strip()]
        if args and args[0] in {"on", "true", "1"}:
            self._verbose = True
        elif args and args[0] in {"off", "false", "0"}:
            self._verbose = False
        else:
            self._verbose = not self._verbose
        # Persist so it survives restarts
        _verbose_file = self.workspace_dir / ".verbose"
        if self._verbose:
            _verbose_file.touch()
        else:
            _verbose_file.unlink(missing_ok=True)
        state = "ON 🔍" if self._verbose else "OFF"
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ ON" if self._verbose else "ON", callback_data="tgl:verbose:on"),
            InlineKeyboardButton("✅ OFF" if not self._verbose else "OFF", callback_data="tgl:verbose:off"),
        ]])
        await self._reply_text(
            update,
            f"Verbose mode: {state}\n"
            f"{'Long-task placeholders will show engine, elapsed, idle time and output events.' if self._verbose else 'Placeholders will show concise status only.'}",
            reply_markup=markup,
        )


    async def cmd_think(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = [a.strip().lower() for a in (context.args or []) if a.strip()]
        if args and args[0] in {"on", "true", "1"}:
            self._think = True
        elif args and args[0] in {"off", "false", "0"}:
            self._think = False
        else:
            self._think = not self._think
        _think_file = self.workspace_dir / ".think"
        if self._think:
            _think_file.touch()
        else:
            _think_file.unlink(missing_ok=True)
        state = "ON 💭" if self._think else "OFF"
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ ON" if self._think else "ON", callback_data="tgl:think:on"),
            InlineKeyboardButton("✅ OFF" if not self._think else "OFF", callback_data="tgl:think:off"),
        ]])
        await self._reply_text(
            update,
            f"Thinking display: {state}\n"
            f"{'Thinking traces will be sent as permanent italic messages every ~60s during generation.' if self._think else 'Thinking traces will not be displayed.'}",
            reply_markup=markup,
        )

    async def cmd_jobs(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        from orchestrator.agent_runtime import _build_jobs_with_buttons
        text, markup = _build_jobs_with_buttons(self.name, self.skill_manager)
        await self._reply_text(update, text, parse_mode="HTML", reply_markup=markup)

    async def cmd_logo(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        import asyncio
        from orchestrator.agent_runtime import _show_logo_animation
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _show_logo_animation)
        await self._reply_text(update, "Logo displayed in console.")

    async def cmd_backend(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        if self.backend_manager.agent_mode == "fixed":
            await self._reply_text(
                update,
                "Backend switching is disabled in **fixed** mode.\nUse `/mode flex` to re-enable.",
                parse_mode="Markdown",
            )
            return

        args = context.args
        allowed_engines = [b["engine"] for b in self.config.allowed_backends]

        if not args:
            await self._reply_text(update, self._build_backend_menu_text(), reply_markup=self._backend_keyboard())
            return

        target_engine = args[0].lower()
        with_context = False
        if len(args) > 1:
            flag = args[1].strip().lower()
            with_context = flag in {"+", "context", "handoff", "with-context"}

        if target_engine not in allowed_engines:
            await self._reply_text(update, f"Backend not allowed: {target_engine}")
            return

        await self._reply_text(
            update,
            self._build_backend_model_prompt(target_engine, with_context),
            reply_markup=self._backend_model_keyboard(target_engine, with_context),
        )

    async def cmd_handoff(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        if self._backend_busy():
            await self._reply_text(update, "Handoff is blocked while a request is running or queued.")
            return

        await self._reply_text(update, "Starting a fresh session with recent bridge history...")
        self.handoff_builder.refresh_recent_context()
        self.handoff_builder.build_handoff()
        prompt, exchange_count, word_count = self.handoff_builder.build_session_restore_prompt(
            max_rounds=10,
            max_words=6000,
        )
        if exchange_count <= 0:
            await self._send_text(update.effective_chat.id, "No recent bridge transcript was available for handoff.")
            return

        self._arm_session_primer(
            "This is a bridge-managed handoff restore. Review AGENT FYI, then use the recent transcript as continuity context."
        )
        if self.backend_manager.current_backend and getattr(self.backend_manager.current_backend.capabilities, "supports_sessions", False):
            await self.backend_manager.current_backend.handle_new_session()
            await self.enqueue_startup_bootstrap(update.effective_chat.id)

        await self._send_text(
            update.effective_chat.id,
            f"Handoff prepared from {exchange_count} recent exchanges ({word_count} words). Restoring continuity now...",
        )
        await self.enqueue_request(
            update.effective_chat.id,
            prompt,
            "handoff",
            f"Handoff restore [{exchange_count} exchanges]",
        )

    async def cmd_park(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = [a.strip() for a in (context.args or []) if a.strip()]
        if not args:
            await self._reply_text(update, self._format_parked_topics_text())
            return

        action = args[0].lower()
        if action == "delete":
            if len(args) < 2 or not args[1].isdigit():
                await self._reply_text(update, "Usage: /park delete <slot>")
                return
            slot_id = int(args[1])
            removed = self.parked_topics.delete_topic(slot_id)
            if not removed:
                await self._reply_text(update, f"Parked topic [{slot_id}] was not found.")
                return
            await self._reply_text(update, f"Deleted parked topic [{slot_id}] {removed.get('title') or ''}".strip())
            return

        if action != "chat":
            await self._reply_text(
                update,
                "Usage:\n"
                "/park - list parked topics\n"
                "/park chat [optional title] - park the current topic\n"
                "/park delete <slot> - delete a parked topic",
            )
            return

        if self._backend_busy():
            await self._reply_text(update, "Parking is blocked while a request is running or queued.")
            return

        title_override = " ".join(args[1:]).strip() or None
        await self._reply_text(update, "Parking the current topic and writing a resume summary...")
        summary = await self._summarize_current_topic_for_parking(title_override=title_override)
        if not summary:
            await self._reply_text(update, "No recent bridge transcript was available to park.")
            return

        topic = self.parked_topics.create_topic(
            title=summary["title"],
            summary_short=summary["summary_short"],
            summary_long=summary["summary_long"],
            recent_context=summary["recent_context"],
            last_user_text=summary["last_user_text"],
            last_assistant_text=summary["last_assistant_text"],
            last_exchange_text=summary["last_exchange_text"],
            source_session=self.session_id_dt,
            title_user_override=title_override,
        )
        slot_id = int(topic["slot_id"])
        await self._reply_text(
            update,
            f"Parked as [{slot_id}] {topic['title']}\n"
            f"{topic['summary_short']}\n\n"
            f"Follow-up reminders are scheduled for this parked topic (up to 3 attempts).\n"
            f"Use /load {slot_id} to resume or /park delete {slot_id} to remove it.",
        )

    async def cmd_load(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = [a.strip() for a in (context.args or []) if a.strip()]
        if len(args) != 1 or not args[0].isdigit():
            await self._reply_text(update, "Usage: /load <slot>")
            return
        if self._backend_busy():
            await self._reply_text(update, "Load is blocked while a request is running or queued.")
            return

        slot_id = int(args[0])
        topic = self.parked_topics.get_topic(slot_id)
        if not topic:
            await self._reply_text(update, f"Parked topic [{slot_id}] was not found.")
            return

        self.parked_topics.mark_loaded(slot_id)
        title = topic.get("title") or f"Topic {slot_id}"
        summary_short = topic.get("summary_short") or ""
        summary_long = topic.get("summary_long") or ""
        recent_context = topic.get("recent_context") or ""
        last_exchange = topic.get("last_exchange_text") or ""
        self._pending_auto_recall_context = (
            "Restore the parked topic below as active continuity context. "
            "Use it as current working context for this session.\n\n"
            f"--- PARKED TOPIC [{slot_id}] ---\n"
            f"Title: {title}\n"
            f"Short Summary: {summary_short}\n\n"
            f"Long Summary:\n{summary_long}\n\n"
            f"Last Exchange:\n{last_exchange or '(none)'}\n\n"
            f"{recent_context}"
        )
        self._arm_session_primer(
            f"Loading parked topic [{slot_id}] {title}. Resume it as the active working context."
        )
        await self._reply_text(update, f"Loading parked topic [{slot_id}] {title} and restoring continuity...")
        await self.enqueue_request(
            update.effective_chat.id,
            (
                "SYSTEM: Resume the parked topic that was just restored into context. "
                "Continue naturally from the most relevant unfinished point. "
                "Do not explain the restore process at length.\n\n"
                "Resume the topic now."
            ),
            "park-load",
            f"Parked topic load [{slot_id}]",
        )

    async def cmd_active(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        if not self.skill_manager:
            await self._reply_text(update, "Active mode is unavailable because the skill manager is not configured.")
            return

        args = [a.strip().lower() for a in (context.args or []) if a.strip()]
        if not args:
            status = self.skill_manager.describe_active_heartbeat(self.name)
            markup = self._active_keyboard()
            await self._reply_text(update, status, reply_markup=markup)
            return

        mode = args[0]
        if mode == "off":
            _, message = self.skill_manager.set_active_heartbeat(self.name, enabled=False)
            await self._reply_text(update, message)
            return
        if mode != "on":
            await self._reply_text(update, "Usage: /active on [minutes] | /active off")
            return

        minutes = self.skill_manager.ACTIVE_HEARTBEAT_DEFAULT_MINUTES
        if len(args) > 1:
            try:
                minutes = max(1, int(args[1]))
            except ValueError:
                await self._reply_text(update, "Minutes must be a positive integer. Usage: /active on [minutes]")
                return

        _, message = self.skill_manager.set_active_heartbeat(self.name, enabled=True, minutes=minutes)
        await self._reply_text(update, message)


    def _get_available_models(self) -> list[str]:
        return get_available_models(self.config.active_backend)

    def _get_available_models_for(self, engine: str) -> list[str]:
        return get_available_models(engine)

    def _get_available_efforts(self) -> list[str]:
        return get_available_efforts(self.config.active_backend)

    def _get_available_efforts_for(self, engine: str) -> list[str]:
        return get_available_efforts(engine)

    def _get_backend_cfg(self, engine: str) -> dict | None:
        return next((b for b in self.config.allowed_backends if b["engine"] == engine), None)

    def _get_current_effort(self) -> Optional[str]:
        if self.backend_manager.current_backend:
            effort = getattr(self.backend_manager.current_backend, "effort", None)
            if effort:
                return effort
        backend_cfg = next(
            (b for b in self.config.allowed_backends if b["engine"] == self.config.active_backend),
            None,
        )
        if backend_cfg:
            return backend_cfg.get("effort")
        return None

    def _set_backend_model(self, engine: str, requested: str):
        normalized = normalize_model(engine, requested)
        if not normalized:
            return
        backend_cfg = self._get_backend_cfg(engine)
        if backend_cfg is not None:
            backend_cfg["model"] = normalized
        if engine == self.config.active_backend and self.backend_manager.current_backend:
            self.backend_manager.current_backend.config.model = normalized
        self.backend_manager.persist_state()

    def _set_active_effort(self, requested: str):
        normalized = normalize_effort(self.config.active_backend, requested)
        if not normalized:
            return
        if self.backend_manager.current_backend and hasattr(self.backend_manager.current_backend, "effort"):
            self.backend_manager.current_backend.effort = normalized
        backend_cfg = self._get_backend_cfg(self.config.active_backend)
        if backend_cfg is not None:
            backend_cfg["effort"] = normalized
        self.backend_manager.persist_state()

    def _backend_keyboard(self) -> InlineKeyboardMarkup:
        current = self.config.active_backend
        buttons = []
        for backend in self.config.allowed_backends:
            engine = backend["engine"]
            base = get_backend_label(engine)
            plain_label = f">> {base}" if engine == current else base
            context_label = f"{base} +"
            buttons.append(
                [
                    InlineKeyboardButton(plain_label, callback_data=f"backend:{engine}:plain"),
                    InlineKeyboardButton(context_label, callback_data=f"backend:{engine}:context"),
                ]
            )
        return InlineKeyboardMarkup(buttons)

    def _model_keyboard(self, current_model: Optional[str] = None, engine: Optional[str] = None) -> InlineKeyboardMarkup:
        active_engine = engine or self.config.active_backend
        active = current_model or self.get_current_model()
        buttons = []
        for model in self._get_available_models_for(active_engine):
            label = f">> {model}" if model == active else model
            buttons.append([InlineKeyboardButton(label, callback_data=f"model:{model}")])
        return InlineKeyboardMarkup(buttons)

    def _effort_keyboard(self, current_effort: Optional[str] = None) -> InlineKeyboardMarkup:
        active = current_effort or self._get_current_effort()
        buttons = []
        for effort in self._get_available_efforts():
            label = f">> {effort}" if effort == active else effort
            buttons.append([InlineKeyboardButton(label, callback_data=f"effort:{effort}")])
        return InlineKeyboardMarkup(buttons)

    def _backend_model_keyboard(
        self,
        target_engine: str,
        with_context: bool,
        current_model: Optional[str] = None,
    ) -> InlineKeyboardMarkup:
        active_model = current_model or normalize_model(
            target_engine,
            (self._get_backend_cfg(target_engine) or {}).get("model"),
        )
        mode_flag = "c" if with_context else "p"
        buttons = []
        for model in self._get_available_models_for(target_engine):
            label = f">> {model}" if model == active_model else model
            buttons.append([InlineKeyboardButton(label, callback_data=f"bmodel:{target_engine}:{mode_flag}:{model}")])
        buttons.append([InlineKeyboardButton("Back", callback_data="backend_menu")])
        return InlineKeyboardMarkup(buttons)

    def _build_backend_menu_text(self) -> str:
        return (
            f"Flex Backend Status\n"
            f"Active: {self.config.active_backend}\n"
            f"Tap a backend to choose a model and switch.\n"
            f"Tap a backend + to rebuild handoff, choose a model, and switch."
        )

    def _build_backend_model_prompt(self, target_engine: str, with_context: bool) -> str:
        mode_text = "with handoff context" if with_context else "without handoff context"
        current_model = normalize_model(
            target_engine,
            (self._get_backend_cfg(target_engine) or {}).get("model"),
        )
        return (
            f"Switch backend to: {target_engine}\n"
            f"Mode: {mode_text}\n"
            f"Select a model before committing the switch.\n"
            f"Current choice: {current_model or 'auto'}"
        )

    def _clear_handoff_state(self):
        with suppress(Exception):
            if self.handoff_path.exists():
                self.handoff_path.unlink()

    async def _switch_backend_mode(
        self,
        chat_id: int,
        target_engine: str,
        target_model: str | None = None,
        with_context: bool = False,
    ) -> tuple[bool, str]:
        allowed_engines = [b["engine"] for b in self.config.allowed_backends]
        if target_engine not in allowed_engines:
            return False, f"Backend not allowed: {target_engine}"

        if self._backend_busy():
            return False, "Backend switch blocked while a request is running or queued."

        switch_ok = await self.backend_manager.switch_backend(
            target_engine,
            target_model=target_model,
        )
        if not switch_ok:
            return False, f"Failed to switch backend to: {target_engine}"

        if with_context:
            with suppress(Exception):
                self.handoff_builder.refresh_recent_context()
                self.handoff_builder.build_handoff()
        else:
            self._clear_handoff_state()
        self.last_backend_switch_at = datetime.now()

        primer_note = (
            f"Backend switched to {target_engine} with continuity handoff available."
            if with_context
            else f"Backend switched to {target_engine}. Review AGENT FYI before the next task."
        )
        self._arm_session_primer(primer_note)

        model = self.get_current_model()
        effort = self._get_current_effort()
        mode_text = "with handoff context" if with_context else "without handoff context"
        message = f"Backend switched to: {target_engine}\nModel: {model}\nMode: {mode_text}"
        if effort:
            message += f"\nEffort: {effort}"
        return True, message

    async def cmd_model(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        if not self.backend_manager.current_backend:
            return

        current_model = self.backend_manager.current_backend.config.model
        args = context.args
        if args:
            requested = args[0].strip()
            if self.config.active_backend == "claude-cli":
                requested = CLAUDE_MODEL_ALIASES.get(requested.lower(), requested)
            available = self._get_available_models()
            if available and requested not in available:
                await self._reply_text(update, f"Unknown model: {requested}\nUse /model to see available options.")
                return

            self._set_backend_model(self.config.active_backend, requested)

            await self._reply_text(update, f"Model switched to: {requested}")
            return

        available = self._get_available_models()
        if not available:
            await self._reply_text(update, f"Current model: {current_model}\nUse /model <name> to switch.")
            return

        await self._reply_text(
            update,
            f"Current model: {current_model}\nSelect:",
            reply_markup=self._model_keyboard(current_model),
        )

    async def cmd_effort(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        if not self.backend_manager.current_backend:
            return

        available = self._get_available_efforts()
        if not available:
            await self._reply_text(update, "Effort control is only available when the active backend is Claude or Codex.")
            return

        args = context.args
        if args:
            requested = args[0].strip().lower()
            if requested == "extra":
                requested = "extra_high"
            if requested not in available:
                await self._reply_text(update, f"Unknown effort level: {requested}\nAvailable: {', '.join(available)}")
                return
            self._set_active_effort(requested)
            await self._reply_text(update, f"Effort switched to: {requested}")
            return

        current_effort = self._get_current_effort() or available[0]
        await self._reply_text(
            update,
            f"Current effort: {current_effort}\nSelect:",
            reply_markup=self._effort_keyboard(current_effort),
        )

    async def callback_model(self, update: Update, context: Any):
        query = update.callback_query
        if not self._is_authorized_user(query.from_user.id):
            return
        data = query.data
        try:
            if data.startswith("model:"):
                model = data.split(":", 1)[1]
                available = self._get_available_models()
                if not available or model in available:
                    self._set_backend_model(self.config.active_backend, model)
                    await query.edit_message_text(
                        f"Model switched to: {model}",
                        reply_markup=self._model_keyboard(model),
                    )
            elif data == "backend_menu":
                await query.edit_message_text(
                    self._build_backend_menu_text(),
                    reply_markup=self._backend_keyboard(),
                )
            elif data.startswith("backend:"):
                parts = data.split(":", 2)
                if len(parts) != 3:
                    await query.answer("Invalid callback data", show_alert=True)
                    return
                _, target_engine, mode = parts
                with_context = mode == "context"
                await query.edit_message_text(
                    self._build_backend_model_prompt(target_engine, with_context),
                    reply_markup=self._backend_model_keyboard(target_engine, with_context),
                )
            elif data.startswith("bmodel:"):
                parts = data.split(":", 3)
                if len(parts) != 4:
                    await query.answer("Invalid callback data", show_alert=True)
                    return
                _, target_engine, mode_flag, model = parts
                with_context = mode_flag == "c"
                success, message = await self._switch_backend_mode(
                    query.message.chat_id,
                    target_engine,
                    target_model=model,
                    with_context=with_context,
                )
                if not success and "busy" in message.lower():
                    await query.answer(message, show_alert=True)
                    return
                await query.edit_message_text(
                    message,
                    reply_markup=self._backend_keyboard() if success else self._backend_model_keyboard(target_engine, with_context, model),
                )
            elif data.startswith("effort:"):
                requested = data.split(":", 1)[1]
                if requested in self._get_available_efforts():
                    self._set_active_effort(requested)
                    await query.edit_message_text(
                        f"Effort switched to: {requested}",
                        reply_markup=self._effort_keyboard(requested),
                    )
        except Exception as e:
            logger.error(f"callback_model error: {e}", exc_info=True)
            await query.answer(f"Error: {e}", show_alert=True)
            return
        await query.answer()

    async def cmd_mode(self, update: Update, context: Any):
        """Switch between fixed (continuous CLI session) and flex (multi-backend) modes.

        Usage: /mode [fixed|flex]
        """
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = (context.args[0].lower() if context.args else "").strip()
        current = self.backend_manager.agent_mode

        if not args or args not in ("fixed", "flex"):
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Fixed" if current == "fixed" else "Fixed", callback_data="tgl:mode:fixed"),
                InlineKeyboardButton("✅ Flex" if current == "flex" else "Flex", callback_data="tgl:mode:flex"),
            ]])
            await self._reply_text(
                update,
                f"Current mode: <b>{current}</b>\n\n"
                f"• <b>fixed</b> — continuous CLI session, incremental prompts\n"
                f"• <b>flex</b> — multi-backend switching, full context injection",
                parse_mode="HTML",
                reply_markup=markup,
            )
            return

        if args == current:
            await self._reply_text(update, f"Already in **{current}** mode.", parse_mode="Markdown")
            return

        self.backend_manager.agent_mode = args
        self.backend_manager._save_state()

        backend = self.backend_manager.current_backend
        if args == "fixed":
            # Enable session persistence on compatible backends
            if hasattr(backend, "set_session_mode"):
                backend.set_session_mode(True)
            await self._reply_text(
                update,
                "Switched to **fixed** mode.\n"
                "• CLI session will persist across messages\n"
                "• Bridge sends incremental prompts (no history re-injection)\n"
                "• `/backend` is disabled; use `/mode flex` to re-enable\n"
                "• `/new` will terminate the current session and start fresh",
                parse_mode="Markdown",
            )
        else:
            # Disable session persistence
            if hasattr(backend, "set_session_mode"):
                backend.set_session_mode(False)
            await self._reply_text(
                update,
                "Switched to **flex** mode.\n"
                "• Full context injection per request\n"
                "• `/backend` switching re-enabled",
                parse_mode="Markdown",
            )

    async def cmd_new(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        if not self.backend_manager.current_backend:
            return
        # /new semantics (author intent): start stateless and ONLY rely on the agent's own agent.md
        # - No Bridge FYI injection
        # - No README/doc auto-reading claims
        # - No continuity restore
        self._pending_auto_recall_context = None

        backend = self.backend_manager.current_backend

        # In fixed mode: terminate CLI session and clear session_id for a truly fresh start
        if self.backend_manager.agent_mode == "fixed":
            if hasattr(backend, "handle_new_session"):
                await backend.handle_new_session()
            # Kill the running process if any
            if hasattr(backend, "current_proc") and backend.current_proc:
                await backend.force_kill_process_tree(
                    backend.current_proc, logger=self.logger, reason="cmd_new_fixed_mode"
                )
                backend.current_proc = None
            await self._reply_text(update, "Fixed mode: session terminated. Starting fresh...")
        elif getattr(backend.capabilities, "supports_sessions", False):
            await backend.handle_new_session()
            await self._reply_text(update, "Starting a fresh session...")
        else:
            await self._reply_text(update, "Starting a fresh stateless session...")

        prompt = (
            "SYSTEM: Fresh session started. Do not reference any previous chat. "
            "Follow ONLY your agent.md instructions. Ask the user what they want to do next."
        )
        await self.enqueue_request(update.effective_chat.id, prompt, "system", "New session")

    async def cmd_memory(self, update: Update, context: Any):
        """Control long-term memory injection into the agent's context.

        Usage:
          /memory          -> show current status
          /memory on       -> enable memory injection (default)
          /memory pause    -> disable injection without deleting any data
          /memory wipe     -> permanently delete all stored memories and turns
        """
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = " ".join(context.args).strip().lower() if context.args else ""
        assembler = getattr(self, "context_assembler", None)

        if args in ("", "status"):
            if assembler:
                state = "ON ✅" if assembler.memory_injection_enabled else "PAUSED ⏸️"
            else:
                state = "unknown (assembler not ready)"
            stats = self.memory_store.get_stats() if hasattr(self, "memory_store") else {}
            turns = stats.get("turns", "?")
            memories = stats.get("memories", "?")
            await self._reply_text(update,
                f"Memory injection: {state}\n"
                f"Stored: {turns} turns, {memories} memories\n\n"
                f"Commands: /memory on | pause | wipe"
            )

        elif args == "on":
            if assembler:
                assembler.memory_injection_enabled = True
            await self._reply_text(update,
                "✅ Memory injection ON. Long-term memories will be included in context."
            )

        elif args == "pause":
            if assembler:
                assembler.memory_injection_enabled = False
            await self._reply_text(update,
                "⏸️ Memory injection PAUSED. Memories are preserved but not injected into context.\n"
                "Use /memory on to resume."
            )

        elif args == "wipe":
            if hasattr(self, "memory_store"):
                result = self.memory_store.clear_all()
                turns = result.get("deleted_turns", 0)
                mems = result.get("deleted_memories", 0)
                state = "ON ✅" if (assembler and assembler.memory_injection_enabled) else "PAUSED ⏸️"
                await self._reply_text(update,
                    f"🗑️ Memory wiped: {turns} turns and {mems} memories deleted.\n"
                    f"Database structure preserved. Injection is still {state}."
                )
            else:
                await self._reply_text(update, "❌ Memory store not available.")

        else:
            await self._reply_text(update,
                "Usage: /memory [on | pause | wipe | status]"
            )

    async def cmd_wipe(self, update: Update, context: Any):
        """Dangerous: wipe the agent's persisted workspace state.

        Goal: after /wipe, the only thing remaining in the workspace should be instructions
        from agent.md (and optionally AGENT.md).

        Usage:
          /wipe            -> shows warning
          /wipe CONFIRM    -> executes wipe
        """
        if not self._is_authorized_user(update.effective_user.id):
            return
        if self._backend_busy():
            await self._reply_text(update, "Wipe is blocked while a request is running or queued. Use /stop first.")
            return

        args = [a.strip() for a in (context.args or []) if a.strip()]
        if not args or args[0].upper() != "CONFIRM":
            await self._reply_text(
                update,
                "⚠️ /wipe will permanently delete this agent's persisted workspace state (memory, transcript, handoff, backend_state, etc.).\n"
                "Only agent instructions (agent.md / AGENT.md) will remain.\n\n"
                "To proceed: /wipe CONFIRM",
            )
            return

        keep_names = {"agent.md", "AGENT.md"}
        removed_files = 0
        removed_dirs = 0

        # Stop any backend process just in case.
        if self.backend_manager.current_backend:
            with suppress(Exception):
                await self.backend_manager.current_backend.shutdown()

        # Wipe workspace contents (keep agent instructions only)
        for child in list(self.workspace_dir.iterdir()):
            if child.name in keep_names:
                continue
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                    removed_dirs += 1
                else:
                    child.unlink(missing_ok=True)
                    removed_files += 1
            except Exception:
                # continue best-effort
                pass

        # Re-create essential dirs
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.memory_dir = self.workspace_dir / "memory"
        self.backend_state_dir = self.workspace_dir / "backend_state"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.backend_state_dir.mkdir(parents=True, exist_ok=True)

        # Re-init memory/handoff subsystems (fresh)
        self.memory_index = MemoryIndex(self.workspace_dir / "memory_index.sqlite")
        self.handoff_builder = HandoffBuilder(self.workspace_dir)
        self.memory_store = BridgeMemoryStore(self.workspace_dir)
        self.context_assembler = BridgeContextAssembler(
            self.memory_store,
            self.config.system_md,
            active_skill_provider=self._get_active_skill_sections,
            sys_prompt_manager=self.sys_prompt_manager,
        )

        # Reset any pending continuity
        self._pending_auto_recall_context = None
        self._pending_session_primer = None

        # Start a fresh backend session if supported
        if self.backend_manager.current_backend and getattr(self.backend_manager.current_backend.capabilities, "supports_sessions", False):
            with suppress(Exception):
                await self.backend_manager.current_backend.handle_new_session()

        await self._reply_text(
            update,
            f"✅ Wiped workspace for {self.name}. Removed {removed_dirs} dirs and {removed_files} files.\n"
            "Only agent.md instructions remain. Start fresh with /new.",
        )

    async def cmd_reset(self, update: Update, context: Any):
        """Soft reset: wipe workspace state but preserve agent identity and /sys prompts.

        Goal: after /reset, agent.md, AGENT.md, and sys_prompts.json are kept intact —
        personality and /sys slots survive. Everything else (memory, transcripts,
        handoff, backend_state, etc.) is cleared.

        Usage:
          /reset           -> shows warning
          /reset CONFIRM   -> executes reset
        """
        if not self._is_authorized_user(update.effective_user.id):
            return
        if self._backend_busy():
            await self._reply_text(update, "Reset is blocked while a request is running or queued. Use /stop first.")
            return

        args = [a.strip() for a in (context.args or []) if a.strip()]
        if not args or args[0].upper() != "CONFIRM":
            await self._reply_text(
                update,
                "⚠️ /reset will clear this agent's memory, transcripts, and session state.\n"
                "agent.md and /sys prompt slots will be preserved — the agent's identity stays intact.\n\n"
                "To proceed: /reset CONFIRM",
            )
            return

        keep_names = {"agent.md", "AGENT.md", "sys_prompts.json"}
        removed_files = 0
        removed_dirs = 0

        # Stop any backend process just in case.
        if self.backend_manager.current_backend:
            with suppress(Exception):
                await self.backend_manager.current_backend.shutdown()

        # Wipe workspace contents (keep identity + sys prompts)
        for child in list(self.workspace_dir.iterdir()):
            if child.name in keep_names:
                continue
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                    removed_dirs += 1
                else:
                    child.unlink(missing_ok=True)
                    removed_files += 1
            except Exception:
                pass

        # Re-create essential dirs
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.memory_dir = self.workspace_dir / "memory"
        self.backend_state_dir = self.workspace_dir / "backend_state"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.backend_state_dir.mkdir(parents=True, exist_ok=True)

        # Re-init memory/handoff subsystems (fresh), reuse existing sys_prompt_manager
        self.memory_index = MemoryIndex(self.workspace_dir / "memory_index.sqlite")
        self.handoff_builder = HandoffBuilder(self.workspace_dir)
        self.memory_store = BridgeMemoryStore(self.workspace_dir)
        self.context_assembler = BridgeContextAssembler(
            self.memory_store,
            self.config.system_md,
            active_skill_provider=self._get_active_skill_sections,
            sys_prompt_manager=self.sys_prompt_manager,
        )

        # Reset any pending continuity
        self._pending_auto_recall_context = None
        self._pending_session_primer = None

        # Start a fresh backend session if supported
        if self.backend_manager.current_backend and getattr(self.backend_manager.current_backend.capabilities, "supports_sessions", False):
            with suppress(Exception):
                await self.backend_manager.current_backend.handle_new_session()

        await self._reply_text(
            update,
            f"✅ Reset workspace for {self.name}. Removed {removed_dirs} dirs and {removed_files} files.\n"
            "Agent identity and /sys slots are intact. Start fresh with /new.",
        )

    async def cmd_clear(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return

        cleared = 0
        if self.media_dir.exists():
            for file_path in self.media_dir.iterdir():
                if file_path.is_file():
                    try:
                        file_path.unlink()
                        cleared += 1
                    except Exception:
                        pass

        if self.backend_manager.current_backend:
            await self.backend_manager.current_backend.handle_new_session()
        await self._reply_text(update, f"Cleared {cleared} media files and reset session state for current backend.")

    async def cmd_stop(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return

        self.logger.warning(
            f"Manual stop requested for flex agent {self.name} "
            f"(queue_size={self.queue.qsize()}, backend={self.config.active_backend})"
        )
        if self.backend_manager.current_backend:
            await self.backend_manager.current_backend.shutdown()

        dropped = 0
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
                dropped += 1
            except asyncio.QueueEmpty:
                break

        await self._reply_text(
            update,
            f"Stopped execution. Cleared {dropped} queued messages and killed active backend process tree.",
        )

    def _load_last_text_from_transcript(self, role: str) -> str | None:
        """Read the last message of the given role from transcript.jsonl."""
        try:
            if not self.transcript_log_path.exists():
                return None
            last_text = None
            with open(self.transcript_log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("role") == role and entry.get("text"):
                            last_text = entry["text"]
                    except Exception:
                        pass
            return last_text
        except Exception:
            return None

    async def cmd_retry(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = [a.strip().lower() for a in (context.args or []) if a.strip()]
        mode = args[0] if args else "response"
        chat_id = update.effective_chat.id
        if mode in {"response", "resp"}:
            if not self.last_response:
                # Try to restore last response from transcript (survives reboot)
                transcript_text = self._load_last_text_from_transcript("assistant")
                if transcript_text:
                    await self._reply_text(update, "Restoring last response from transcript...")
                    await self.send_long_message(
                        chat_id=chat_id,
                        text=transcript_text,
                        purpose="retry-response",
                    )
                    return
                # Fallback: re-run the last prompt
                if self.last_prompt:
                    await self._reply_text(update, "No cached response — retrying last prompt...")
                    await self.enqueue_request(
                        self.last_prompt.chat_id,
                        self.last_prompt.prompt,
                        "retry",
                        "Retry request",
                    )
                else:
                    await self._reply_text(update, "Nothing to retry — no previous response or prompt.")
                return
            await self._reply_text(update, "Resending last response...")
            await self.send_long_message(
                chat_id=self.last_response["chat_id"],
                text=self.last_response["text"],
                request_id=self.last_response.get("request_id"),
                purpose="retry-response",
            )
            return
        if mode in {"prompt", "req", "request"}:
            if not self.last_prompt:
                # Try to restore last user prompt from transcript
                transcript_text = self._load_last_text_from_transcript("user")
                if transcript_text:
                    await self._reply_text(update, "Restoring last prompt from transcript...")
                    await self.enqueue_request(chat_id, transcript_text, "retry", "Retry request")
                else:
                    await self._reply_text(update, "No previous prompt to rerun.")
                return
            await self._reply_text(update, "Retrying last prompt...")
            await self.enqueue_request(
                self.last_prompt.chat_id,
                self.last_prompt.prompt,
                "retry",
                "Retry request",
            )
            return
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("重发回复", callback_data="tgl:retry:response"),
            InlineKeyboardButton("重跑 Prompt", callback_data="tgl:retry:prompt"),
        ]])
        await self._reply_text(update, "Retry — choose action:", reply_markup=markup)

    async def handle_message(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            self.logger.warning(f"Ignored message from unauthorized user ID: {update.effective_user.id}")
            return
        text = update.message.text
        _print_user_message(self.name, text)
        await self.enqueue_request(update.effective_chat.id, text, "text", _safe_excerpt(text))

    # ------------------------------------------------------------------
    # Media handlers (photo, voice, audio, document, video, sticker)
    # ------------------------------------------------------------------

    async def download_media(self, file_id: str, filename: str) -> Path:
        tg_file = await self.app.bot.get_file(file_id)
        local_path = self.media_dir / filename
        await tg_file.download_to_drive(local_path)
        self.logger.info(f"Downloaded media: {local_path}")
        return local_path

    async def _handle_media_message(self, update, media_kind: str, filename: str, file_id: str, prompt: str, summary: str):
        backend = getattr(self.backend_manager, "current_backend", None)
        if backend and not backend.capabilities.supports_files:
            await self._reply_text(update, f"Current backend does not support {media_kind.lower()} attachments yet.")
            return
        _print_user_message(self.name, summary, media_tag=media_kind)
        try:
            local_path = await self.download_media(file_id, filename)
            rendered_prompt = prompt.replace("{local_path}", str(local_path))
            await self.enqueue_request(update.effective_chat.id, rendered_prompt, media_kind.lower(), summary)
        except Exception as e:
            self.error_logger.exception(f"{media_kind} handler failed for '{filename}': {e}")
            try:
                await self._reply_text(update, f"Failed to process {media_kind.lower()} message.")
            except Exception:
                pass

    async def handle_document(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        doc = update.message.document
        original_name = doc.file_name or f"file_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        caption = update.message.caption or ""
        ext = Path(original_name).suffix.lower()
        if ext == ".pdf":
            prompt = f'User sent a PDF document "{original_name}" (saved at {{local_path}}). Extract the text, analyze the contents thoroughly, and respond.'
        elif ext in [".txt", ".md", ".csv", ".json", ".py", ".js", ".html"]:
            prompt = f'User sent a text/code file "{original_name}" (saved at {{local_path}}). Read the raw contents carefully and respond.'
        else:
            prompt = f'User sent a document "{original_name}" (saved at {{local_path}}). Attempt to read the file and respond.'
        if caption:
            prompt += f' Caption: "{caption}"'
        await self._handle_media_message(update, "Document", original_name, doc.file_id, prompt, original_name)

    async def handle_photo(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        photo = update.message.photo[-1]
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        caption = update.message.caption or ""
        prompt = "User sent a photo (saved at {local_path})."
        if caption:
            prompt += f' Caption: "{caption}"'
        prompt += " View the image and respond."
        await self._handle_media_message(update, "Photo", f"photo_{ts}.jpg", photo.file_id, prompt, caption or f"photo_{ts}.jpg")

    async def handle_voice(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        voice = update.message.voice
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"voice_{ts}.ogg"
        await self._handle_voice_or_audio(update, "Voice", filename, voice.file_id)

    async def handle_audio(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        audio = update.message.audio
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        original_name = audio.file_name or f"audio_{ts}"
        caption = update.message.caption or ""
        await self._handle_voice_or_audio(update, "Audio", original_name, audio.file_id, caption=caption)

    async def _handle_voice_or_audio(self, update: Update, media_kind: str, filename: str, file_id: str, caption: str = ""):
        """Download voice/audio, transcribe locally, and dispatch as text."""
        from orchestrator.voice_transcriber import get_transcriber
        _print_user_message(self.name, f"Transcribing {filename}...", media_tag=media_kind)
        try:
            local_path = await self.download_media(file_id, filename)
            transcriber = get_transcriber()
            transcript = await transcriber.transcribe(local_path)

            if transcript.startswith("[Transcription error]"):
                self.error_logger.error(f"Voice transcription failed for {filename}: {transcript}")
                # Fall back to sending as media if backend supports files
                backend = getattr(self.backend_manager, "current_backend", None)
                if backend and backend.capabilities.supports_files:
                    prompt = f"User sent a voice message (saved at {local_path}). Listen to the audio, transcribe it, and respond."
                    await self.enqueue_request(update.effective_chat.id, prompt, media_kind.lower(), filename)
                else:
                    await self._reply_text(update, f"Failed to transcribe {media_kind.lower()} message.")
                return

            # Build prompt from transcript
            _print_user_message(self.name, transcript, media_tag="Transcription")
            prompt = f"[Voice message transcription] {transcript}"
            if caption:
                prompt += f'\nCaption: "{caption}"'

            self.telegram_logger.info(
                f"Transcribed {media_kind.lower()} ({filename}): {len(transcript)} chars"
            )
            await self.enqueue_request(update.effective_chat.id, prompt, "voice_transcript", f"{media_kind}: {filename}")
        except Exception as e:
            self.error_logger.exception(f"{media_kind} voice handler failed for '{filename}': {e}")
            try:
                await self._reply_text(update, f"Failed to process {media_kind.lower()} message.")
            except Exception:
                pass

    async def handle_video(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        video = update.message.video
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        original_name = video.file_name or f"video_{ts}.mp4"
        caption = update.message.caption or ""
        prompt = f'User sent a video "{original_name}" (saved at {{local_path}}).'
        if caption:
            prompt += f' Caption: "{caption}"'
        prompt += " Watch the video and respond."
        await self._handle_media_message(update, "Video", original_name, video.file_id, prompt, original_name)

    async def handle_sticker(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        sticker = update.message.sticker
        emoji = sticker.emoji or ""
        _print_user_message(self.name, emoji or "sticker", media_tag="Sticker")
        await self.enqueue_request(update.effective_chat.id, f"User sent a sticker (emoji: {emoji}). React warmly.", "sticker", emoji or "sticker")

    async def send_long_message(
        self,
        chat_id: int,
        text: str,
        request_id: Optional[str] = None,
        purpose: str = "response",
    ):
        """Send a message to Telegram with safe chunking.

        IMPORTANT: For backend errors, we must avoid spamming the user.
        - Errors can be extremely long (e.g., CLI streaming logs).
        - HTML chunking can break tags and trigger Telegram parse failures.

        Policy:
        - purpose=="error": send a single **plain-text** summary (truncated)
          + pointer to the local errors log.
        """
        # Guard: skip Telegram send if not connected
        if not self.telegram_connected:
            self.logger.info(
                f"Telegram disconnected — skipping send for {request_id or 'unknown'} "
                f"(purpose={purpose}, text_len={len(text)})"
            )
            return 0.0, 0

        send_started = datetime.now()
        tg_max_len = 4096
        chunk_count = 0

        # --- Error: never spam. Send one short, plain-text message only. ---
        if purpose == "error":
            errors_path = str(getattr(self, "session_dir", self.workspace_dir) / "errors.log")
            header = f"❌ Backend error ({self.config.active_backend})"
            if request_id:
                header += f" | {request_id}"

            # Keep a readable excerpt (head + tail) and stay under Telegram limits.
            max_excerpt = 2400
            s = (text or "").strip()
            if len(s) > max_excerpt:
                head = s[:1200]
                tail = s[-800:]
                excerpt = head + "\n... (truncated) ...\n" + tail
            else:
                excerpt = s

            msg = (
                f"{header}\n\n"
                f"{excerpt}\n\n"
                f"Full log (local): {errors_path}\n"
                f"Tip: use /verbose off to reduce progress message noise."
            )
            if len(msg) > tg_max_len:
                msg = msg[: tg_max_len - 20] + "\n... (truncated)"

            await self.app.bot.send_message(chat_id=chat_id, text=msg)
            self.telegram_logger.info(
                f"Sent Telegram message for request_id={request_id or '<none>'} "
                f"(purpose=error, chunks=1, text_len={len(msg)})"
            )
            return (datetime.now() - send_started).total_seconds(), 1

        # --- Normal responses: markdown→HTML + chunking ---
        html = _md_to_html(text)

        async def _send_chunk(chunk_raw: str, chunk_html: str, chunk_index: int):
            try:
                await self.app.bot.send_message(
                    chat_id=chat_id,
                    text=chunk_html,
                    parse_mode=constants.ParseMode.HTML,
                )
            except Exception as e:
                self.telegram_logger.warning(
                    f"Send failed for request_id={request_id or '<none>'} "
                    f"(purpose={purpose}, chunk={chunk_index}, mode=html): {e}. Fallback to raw text."
                )
                await self.app.bot.send_message(chat_id=chat_id, text=chunk_raw)

        if len(html) <= tg_max_len:
            chunk_count = 1
            await _send_chunk(text, html, chunk_count)
            self.telegram_logger.info(
                f"Sent Telegram message for request_id={request_id or '<none>'} "
                f"(purpose={purpose}, chunks={chunk_count}, text_len={len(text)})"
            )
            return (datetime.now() - send_started).total_seconds(), chunk_count

        raw_chunks, html_chunks = [], []
        raw_remain, html_remain = text, html
        while raw_remain:
            if len(html_remain) <= tg_max_len:
                raw_chunks.append(raw_remain)
                html_chunks.append(html_remain)
                break
            split_at = html_remain.rfind("\n", 0, tg_max_len)
            if split_at == -1:
                split_at = tg_max_len
            raw_split = raw_remain.rfind("\n", 0, split_at + 500)
            if raw_split == -1:
                raw_split = min(split_at, len(raw_remain))

            raw_chunks.append(raw_remain[:raw_split])
            html_chunks.append(html_remain[:split_at])
            raw_remain = raw_remain[raw_split:].lstrip("\n")
            html_remain = html_remain[split_at:].lstrip("\n")

        for chunk_count, (rc, hc) in enumerate(zip(raw_chunks, html_chunks), start=1):
            await _send_chunk(rc, hc, chunk_count)
        self.telegram_logger.info(
            f"Sent Telegram message for request_id={request_id or '<none>'} "
            f"(purpose={purpose}, chunks={chunk_count}, text_len={len(text)})"
        )
        return (datetime.now() - send_started).total_seconds(), chunk_count

    async def typing_loop(self, chat_id: int, stop_event: asyncio.Event):
        # Guard: skip if Telegram not connected
        if not self.telegram_connected:
            return
        while not stop_event.is_set():
            try:
                await self.app.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass

    async def _escalating_placeholder_loop(
        self,
        chat_id: int,
        placeholder,          # Telegram Message object or None
        request_id: str,
        stop_event: asyncio.Event,
        backend=None,         # BaseBackend instance for last_activity_at inspection
    ):
        """
        Runs alongside typing_loop. Edits the placeholder message at escalating
        time thresholds so the user knows the agent is still alive, and surfaces
        whether the backend has gone silent (e.g. waiting on a sub-agent/process).

        Thresholds (seconds): 30 → 60 → 90 → 150
        Exits immediately if stop_event is set (task completed) or placeholder is None.
        """
        if placeholder is None or not self.telegram_connected:
            return

        # Read per-agent config from extra (with safe fallbacks).
        # In agent config JSON, set e.g.:
        #   "escalation_thresholds": [30, 60, 90, 150]
        #   "escalation_idle_warn_after": 45
        _extra = (self.config.extra or {}) if self.config else {}
        THRESHOLDS: list[int] = _extra.get("escalation_thresholds", [30, 60, 90, 150])
        IDLE_WARN_AFTER: int = _extra.get("escalation_idle_warn_after", 45)

        elapsed = 0
        for threshold in THRESHOLDS:
            wait_s = threshold - elapsed
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait_s)
                return  # task finished before this threshold
            except asyncio.TimeoutError:
                elapsed = threshold

            # --- Build status text ---
            idle_s: Optional[int] = None
            events: Optional[int] = None
            if backend is not None:
                if getattr(backend, "last_activity_at", 0) > 0:
                    idle_s = int(time.time() - backend.last_activity_at)
                line_count = getattr(backend, "output_line_count", 0)
                if line_count > 0:
                    events = line_count

            is_stuck = idle_s is not None and idle_s > IDLE_WARN_AFTER

            if self._verbose:
                # Verbose: structured detail block
                engine = self.config.active_backend
                lines = [f"🔍 <b>{self.name}</b> | {engine}"]
                lines.append(f"⏱ Elapsed: {elapsed}s")
                if idle_s is not None:
                    if is_stuck:
                        lines.append(f"⚠️ No output for {idle_s}s — may be supervising a sub-process")
                    else:
                        lines.append(f"📡 Last output: {idle_s}s ago")
                if events is not None:
                    lines.append(f"📊 Output events: {events}")
                text = "\n".join(lines)
                parse_mode = "HTML"
            else:
                # Concise: single-line summary
                parse_mode = None
                if is_stuck:
                    text = (
                        f"⚠️ No backend output for {idle_s}s "
                        f"({elapsed}s total) — may be running a sub-process or stuck."
                    )
                elif elapsed <= 60:
                    activity = f", last output {idle_s}s ago" if idle_s is not None else ""
                    text = f"Still working... ⏳ ({elapsed}s elapsed{activity})"
                elif elapsed <= 90:
                    activity = f", last output {idle_s}s ago" if idle_s is not None else ""
                    text = f"This is taking a while 🔄 ({elapsed}s elapsed{activity})"
                else:
                    text = "Still running — I'll message you when done! 📬"

            try:
                await self.app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=placeholder.message_id,
                    text=text,
                    parse_mode=parse_mode,
                )
                self.telegram_logger.info(
                    f"Escalated placeholder for {request_id} at {elapsed}s "
                    f"(idle_s={idle_s}, events={events}, verbose={self._verbose})"
                )
            except Exception as exc:
                self.telegram_logger.warning(
                    f"Failed to escalate placeholder for {request_id} at {elapsed}s: {exc}"
                )

        # Past all thresholds — just wait quietly for stop_event
        await stop_event.wait()

    # ------------------------------------------------------------------
    # Stage 3b: Streaming display loop (verbose ON with stream events)
    # ------------------------------------------------------------------

    async def _streaming_display_loop(
        self,
        chat_id: int,
        placeholder,
        request_id: str,
        stop_event: asyncio.Event,
        event_queue: asyncio.Queue,
        backend=None,
    ):
        """
        Real-time streaming display for verbose mode.  Consumes StreamEvent
        objects from event_queue and edits the placeholder message with a
        rolling activity buffer.  Rate-limited to ~1 edit per 2.5s.
        """
        if placeholder is None or not self.telegram_connected:
            return

        from adapters.stream_events import KIND_THINKING, KIND_TEXT_DELTA, KIND_TOOL_START

        buffer: list[str] = []
        MAX_LINES = 10
        MAX_MSG_LEN = 3800
        MIN_EDIT_INTERVAL = 2.5
        last_edit_at = 0.0
        started = time.time()
        dirty = False
        engine = getattr(self.config, "active_backend", "unknown")

        ICONS = {
            "thinking": "💭",
            "tool_start": "🔧",
            "tool_end": "  →",
            "file_read": "📂",
            "file_edit": "📝",
            "shell_exec": "🔧",
            "text_delta": "✏️",
            "progress": "📊",
            "error": "❌",
        }

        def _build_display() -> str:
            import html as _html
            elapsed = int(time.time() - started)
            header = f"🔍 <b>{_html.escape(str(self.name))}</b> | {_html.escape(str(engine))} | {elapsed}s\n"
            body = "\n".join(_html.escape(line) for line in buffer[-MAX_LINES:])
            text = header + "\n" + body
            if len(text) > MAX_MSG_LEN:
                text = text[:MAX_MSG_LEN] + "\n..."
            return text

        async def _edit_placeholder():
            nonlocal last_edit_at, dirty
            text = _build_display()
            try:
                await self.app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=placeholder.message_id,
                    text=text,
                    parse_mode="HTML",
                )
                last_edit_at = time.time()
                dirty = False
            except Exception as exc:
                if "429" in str(exc) or "RetryAfter" in str(exc):
                    await asyncio.sleep(3)
                elif "message to edit not found" in str(exc).lower() or "message is not modified" in str(exc).lower():
                    pass
                else:
                    self.telegram_logger.warning(
                        f"Streaming display edit failed for {request_id}: {exc}"
                    )

        while not stop_event.is_set():
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=MIN_EDIT_INTERVAL)
                icon = ICONS.get(event.kind, "•")
                if event.kind == KIND_TEXT_DELTA:
                    summary = event.summary[:80]
                    if buffer and buffer[-1].startswith("✏️"):
                        buffer[-1] = f"{icon} {summary}"
                    else:
                        buffer.append(f"{icon} {summary}")
                elif event.kind == KIND_THINKING:
                    if buffer and buffer[-1].startswith("💭"):
                        buffer[-1] = f"{icon} {event.summary[:80]}"
                    else:
                        buffer.append(f"{icon} {event.summary[:80]}")
                elif event.kind == KIND_TOOL_START:
                    # Replace last tool_start line if consecutive (avoids fragmented JSON input spam)
                    if buffer and buffer[-1].startswith("🔧"):
                        buffer[-1] = f"{icon} {event.summary[:100]}"
                    else:
                        buffer.append(f"{icon} {event.summary[:100]}")
                else:
                    buffer.append(f"{icon} {event.summary[:100]}")

                if len(buffer) > MAX_LINES * 2:
                    buffer[:] = buffer[-MAX_LINES:]

                dirty = True
            except asyncio.TimeoutError:
                pass

            now = time.time()
            if dirty and (now - last_edit_at) >= MIN_EDIT_INTERVAL:
                await _edit_placeholder()

        if buffer:
            elapsed = int(time.time() - started)
            buffer.append(f"✅ Done ({elapsed}s)")
            await _edit_placeholder()

    def _make_stream_callback(self, event_queue: asyncio.Queue | None = None,
                              think_buffer: list | None = None):
        """Create an async callback that puts StreamEvents into the queue
        and/or appends all events to the think buffer."""
        from adapters.stream_events import KIND_THINKING
        _engine = self.config.active_backend
        _chunk_target = 100
        _chunk_hard_limit = 150
        _chunk_endings = ("。", "！", "？", "\n")
        async def _callback(event):
            if event_queue is not None:
                try:
                    event_queue.put_nowait(event)
                except asyncio.QueueFull:
                    self.logger.debug(f"Stream event queue full, dropping: {event.summary[:40]!r}")
            if think_buffer is not None:
                if event.kind != KIND_THINKING:
                    return
                if _engine == "openrouter-api":
                    snippet = (event.summary or "")[:200].strip()
                    if not snippet or snippet == self._last_openrouter_think_snippet:
                        return
                    self._last_openrouter_think_snippet = snippet
                    self._openrouter_think_chunk += snippet
                    if (
                        len(self._openrouter_think_chunk) >= _chunk_hard_limit
                        or (
                            len(self._openrouter_think_chunk) >= _chunk_target
                            and self._openrouter_think_chunk.endswith(_chunk_endings)
                        )
                    ):
                        think_buffer.append(self._openrouter_think_chunk)
                        self._openrouter_think_chunk = ""
                    return
                # Non-openrouter: accumulate thinking chunks before appending
                snippet = (event.summary or "")[:200].strip()
                if snippet.startswith("Thinking: "):
                    snippet = snippet[len("Thinking: "):]
                elif snippet == "Thinking...":
                    return
                if snippet:
                    self._openrouter_think_chunk += (" " if self._openrouter_think_chunk else "") + snippet
                    if (
                        len(self._openrouter_think_chunk) >= _chunk_hard_limit
                        or (
                            len(self._openrouter_think_chunk) >= _chunk_target
                            and self._openrouter_think_chunk.endswith(_chunk_endings)
                        )
                    ):
                        think_buffer.append(self._openrouter_think_chunk)
                        self._openrouter_think_chunk = ""
        return _callback

    # ------------------------------------------------------------------
    # Think mode: periodic flushing of thinking traces as permanent messages
    # ------------------------------------------------------------------

    async def _flush_thinking(self, chat_id: int):
        """Send accumulated thinking events to Telegram, console, and transcript."""
        if self._openrouter_think_chunk:
            self._think_buffer.append(self._openrouter_think_chunk)
            self._openrouter_think_chunk = ""
        if not self._think_buffer:
            return
        import html as _html
        lines = self._think_buffer[:]
        self._think_buffer.clear()
        text = "\n".join(lines)
        if len(text) > 3800:
            text = text[:3800] + "\n..."
        # Console
        _print_thinking(self.name, text)
        # Transcript (for workbench polling) — always write, even if Telegram disconnected
        self.handoff_builder.append_transcript("thinking", f"💭 {text}", "think")
        # Telegram — skip if not connected
        if not self.telegram_connected:
            return
        _think_msg = f"💭 <i>{_html.escape(text)}</i>"
        try:
            await self.app.bot.send_message(chat_id=chat_id, text=_think_msg, parse_mode="HTML")
        except Exception as e:
            if "ConnectError" in type(e).__name__ or "ConnectError" in str(e):
                await asyncio.sleep(2)
                try:
                    await self.app.bot.send_message(chat_id=chat_id, text=_think_msg, parse_mode="HTML")
                except Exception as e2:
                    self.telegram_logger.warning(f"Failed to send thinking message (retry): {e2}")
            else:
                self.telegram_logger.warning(f"Failed to send thinking message: {e}")

    async def _thinking_flush_loop(self, chat_id: int, stop_event: asyncio.Event):
        """Periodically flush accumulated thinking traces every 6 seconds."""
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=6)
                break  # stop_event was set
            except asyncio.TimeoutError:
                pass  # 6s elapsed — flush
            await self._flush_thinking(chat_id)

    # ------------------------------------------------------------------
    # Stage 4: Background-mode helpers
    # ------------------------------------------------------------------

    def _register_background_task(self, gen_task: asyncio.Task, item: QueuedRequest) -> None:
        """Track a detached generation task and wire up its completion callback."""
        self._background_tasks.add(gen_task)

        def _on_done(task: asyncio.Task) -> None:
            self._background_tasks.discard(task)
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(self._on_background_complete(task, item))
            except RuntimeError:
                pass  # loop closed during shutdown

        gen_task.add_done_callback(_on_done)

    async def _on_background_complete(self, task: asyncio.Task, item: QueuedRequest) -> None:
        """Called when a background generate_response task finishes."""
        if self.is_shutting_down:
            return

        try:
            if task.cancelled():
                self._mark_error(f"Background task cancelled: {item.summary}")
                self.logger.warning(f"Background task {item.request_id} was cancelled.")
                await self.send_long_message(
                    item.chat_id,
                    f"⚠️ Background task [{item.summary}] was cancelled before completing.",
                    request_id=item.request_id,
                    purpose="bg-cancelled",
                )
                return

            exc = task.exception()
            if exc:
                self._mark_error(str(exc))
                self.error_logger.error(f"Background task {item.request_id} raised: {exc}")
                await self.send_long_message(
                    item.chat_id,
                    f"⚠️ Background task error ({self.config.active_backend}): {exc}",
                    request_id=item.request_id,
                    purpose="bg-error",
                )
                return

            response = task.result()

            if response.is_success and response.text:
                self._mark_success()
                self.last_response = {
                    "chat_id": item.chat_id,
                    "text": response.text,
                    "request_id": item.request_id,
                }
                memory_user_text = item.prompt
                if item.source.lower() in {"document", "photo", "voice", "audio", "video", "sticker"}:
                    memory_user_text = f"[{item.source}] {item.summary}"
                if item.source not in {"startup", "system"}:
                    self.memory_store.record_turn("user", item.source, memory_user_text)
                    self.memory_store.record_turn("assistant", self.config.active_backend, response.text)
                    self.memory_store.record_exchange(memory_user_text, response.text, item.source)
                self.handoff_builder.append_transcript("user", item.prompt, item.source)
                self.handoff_builder.append_transcript("assistant", response.text)
                self.handoff_builder.refresh_recent_context()
                _print_final_response(self.name, response.text)
                total_s = (datetime.now() - datetime.fromisoformat(item.created_at)).total_seconds()
                send_elapsed_s, chunk_count = await self.send_long_message(
                    chat_id=item.chat_id,
                    text=response.text,
                    request_id=item.request_id,
                    purpose="bg-response",
                )
                await self._send_voice_reply(item.chat_id, response.text, item.request_id)
                self.logger.info(
                    f"Background task {item.request_id} delivered "
                    f"(total_s={total_s:.2f}, chunks={chunk_count}, send_s={send_elapsed_s:.2f})"
                )
            else:
                err_msg = response.error or "Unknown error"
                self._mark_error(err_msg)
                self.error_logger.error(f"Background task {item.request_id} failed: {err_msg}")
                clipped = err_msg if len(err_msg) <= 3000 else err_msg[:2800].rstrip() + "\n\n[truncated]"
                await self.send_long_message(
                    item.chat_id,
                    f"⚠️ Background task error ({self.config.active_backend}): {clipped}",
                    request_id=item.request_id,
                    purpose="bg-error",
                )

        except Exception as e:
            self._mark_error(str(e))
            self.error_logger.exception(
                f"Unhandled error in _on_background_complete for {item.request_id}: {e}"
            )

    async def process_queue(self):
        self.logger.info("Flex queue processor started.")
        while True:
            item = None
            try:
                item = await self.queue.get()
                if not item.silent:
                    self.last_prompt = item
                is_bridge_request = item.source.startswith("bridge:")
                queued_at = datetime.fromisoformat(item.created_at)
                queue_wait_s = (datetime.now() - queued_at).total_seconds()
                self.logger.info(
                    f"Processing {item.request_id} via {self.config.active_backend} "
                    f"(source={item.source}, silent={item.silent}, prompt_len={len(item.prompt)}, "
                    f"queue_wait_s={queue_wait_s:.2f})"
                )
                self.current_request_meta = {
                    "request_id": item.request_id,
                    "source": item.source,
                    "summary": item.summary,
                    "started_at": datetime.now().isoformat(),
                }
                self._mark_activity()
                self._log_maintenance(
                    item,
                    "processing",
                    engine=self.config.active_backend,
                    silent=item.silent,
                    prompt_len=len(item.prompt),
                    queue_wait_s=f"{queue_wait_s:.2f}",
                )
                self.is_generating = True

                effective_prompt = self._consume_session_primer(item)
                # In fixed mode with an active session, use incremental prompts
                _incremental = (
                    self.backend_manager.agent_mode == "fixed"
                    and hasattr(self.backend_manager.current_backend, "_session_id")
                    and self.backend_manager.current_backend._session_id is not None
                )
                final_prompt = self.context_assembler.build_prompt(
                    effective_prompt, self.config.active_backend, incremental=_incremental
                )
                
                stop_typing = None
                typing_task = None
                escalation_task = None
                placeholder = None
                _stream_callback = None
                _think_flush_task = None
                if not item.silent and item.deliver_to_telegram:
                    placeholder_text, placeholder_parse_mode = self.get_typing_placeholder()
                    stop_typing = asyncio.Event()
                    typing_task = asyncio.create_task(self.typing_loop(item.chat_id, stop_typing))
                    try:
                        placeholder_started = datetime.now()
                        placeholder = await self.app.bot.send_message(
                            chat_id=item.chat_id,
                            text=placeholder_text,
                            parse_mode=placeholder_parse_mode,
                        )
                        placeholder_elapsed_s = (datetime.now() - placeholder_started).total_seconds()
                        self.telegram_logger.info(
                            f"Sent placeholder for {item.request_id} "
                            f"(elapsed_s={placeholder_elapsed_s:.2f})"
                        )
                    except Exception as e:
                        self.telegram_logger.warning(f"Failed to send placeholder: {e}")

                    # Verbose ON → streaming display loop; OFF → escalating placeholder.
                    # Think ON → start thinking flush loop (independent of verbose).
                    _stream_queue = None
                    _think_flush_task = None
                    _use_stream = self._verbose or self._think
                    if _use_stream:
                        if self._verbose:
                            _stream_queue = asyncio.Queue(maxsize=200)
                        _stream_callback = self._make_stream_callback(
                            event_queue=_stream_queue,
                            think_buffer=self._think_buffer if self._think else None,
                        )
                    if self._verbose:
                        escalation_task = asyncio.create_task(
                            self._streaming_display_loop(
                                item.chat_id,
                                placeholder,
                                item.request_id,
                                stop_typing,
                                _stream_queue,
                                backend=self.backend_manager.current_backend,
                            )
                        )
                    else:
                        escalation_task = asyncio.create_task(
                            self._escalating_placeholder_loop(
                                item.chat_id,
                                placeholder,
                                item.request_id,
                                stop_typing,
                                backend=self.backend_manager.current_backend,
                            )
                        )
                    if self._think:
                        self._think_buffer.clear()
                        self._openrouter_think_chunk = ""
                        self._last_openrouter_think_snippet = None
                        _think_flush_task = asyncio.create_task(
                            self._thinking_flush_loop(item.chat_id, stop_typing)
                        )

                # Resolve stream callback (may be None if silent or stream not needed)
                _on_stream = _stream_callback if not item.silent else None

                # --- Stage 4: background-mode detach ---
                _extra = (self.config.extra or {})
                _bg_mode = _extra.get("background_mode", False) and not item.silent and item.deliver_to_telegram
                _detach_after: float = float(
                    _extra.get("background_detach_after")
                    or (_extra.get("escalation_thresholds") or [30, 60, 90, 150])[-1]
                )

                backend_started = datetime.now()
                current_backend = getattr(self.backend_manager, "current_backend", None)
                if self.config.active_backend == "openrouter-api" and hasattr(current_backend, "set_reasoning_enabled"):
                    current_backend.set_reasoning_enabled(self._think)
                detached = False
                if _bg_mode:
                    _gen_task = asyncio.create_task(
                        self.backend_manager.generate_response(
                            final_prompt, item.request_id,
                            is_retry=item.is_retry, silent=item.silent,
                            on_stream_event=_on_stream,
                        )
                    )
                    try:
                        response = await asyncio.wait_for(
                            asyncio.shield(_gen_task), timeout=_detach_after
                        )
                    except asyncio.TimeoutError:
                        detached = True
                    except asyncio.CancelledError:
                        _gen_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await _gen_task
                        raise
                    finally:
                        self.is_generating = False
                else:
                    try:
                        response = await self.backend_manager.generate_response(
                            final_prompt, item.request_id,
                            is_retry=item.is_retry, silent=item.silent,
                            on_stream_event=_on_stream,
                        )
                    finally:
                        self.is_generating = False

                if detached:
                    if stop_typing and typing_task:
                        stop_typing.set()
                        await typing_task
                        if escalation_task is not None:
                            with suppress(asyncio.CancelledError):
                                await escalation_task
                    if placeholder:
                        with suppress(Exception):
                            await self.app.bot.edit_message_text(
                                chat_id=item.chat_id,
                                message_id=placeholder.message_id,
                                text="⏳ Still running in the background — I'll notify you here when done! 📬",
                            )
                    self._register_background_task(_gen_task, item)
                    self.logger.info(
                        f"Detached {item.request_id} to background "
                        f"(threshold={_detach_after}s, backend={self.config.active_backend})"
                    )
                    self._log_maintenance(item, "bg_detached", detach_after_s=_detach_after)
                    continue  # release queue slot; task runs in background

                backend_elapsed = (datetime.now() - backend_started).total_seconds()
                self.logger.info(
                    f"Backend finished {item.request_id} via {self.config.active_backend} "
                    f"(success={response.is_success}, elapsed_s={backend_elapsed:.2f}, "
                    f"text_len={len(response.text or '')}, error_len={len(response.error or '')}, "
                    f"final_prompt_len={len(final_prompt)})"
                )
                self._log_maintenance(
                    item,
                    "backend_finished",
                    engine=self.config.active_backend,
                    success=response.is_success,
                    elapsed_s=f"{backend_elapsed:.2f}",
                    text_len=len(response.text or ""),
                    error_len=len(response.error or ""),
                    final_prompt_len=len(final_prompt),
                    result_excerpt=_safe_excerpt(response.text or response.error or "", 200),
                )

                if stop_typing and typing_task:
                    stop_typing.set()
                    await typing_task
                    if escalation_task is not None:
                        with suppress(asyncio.CancelledError):
                            await escalation_task

                # Flush remaining thinking traces and cancel the flush loop
                if _think_flush_task is not None:
                    _think_flush_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await _think_flush_task
                    await self._flush_thinking(item.chat_id)

                if placeholder:
                    try:
                        delete_started = datetime.now()
                        await self.app.bot.delete_message(chat_id=item.chat_id, message_id=placeholder.message_id)
                        delete_elapsed_s = (datetime.now() - delete_started).total_seconds()
                        self.telegram_logger.info(
                            f"Deleted placeholder for {item.request_id} "
                            f"(elapsed_s={delete_elapsed_s:.2f})"
                        )
                    except Exception:
                        pass

                # 3. Update transcript and refresh context
                if response.is_success and not response.text:
                    # Backend succeeded but returned empty text (e.g. tool call returned
                    # an error and the model gave up without producing a reply).
                    # Surface a clear message rather than falling through to "Unknown error".
                    err_msg = "I wasn't able to complete that — a tool I tried to use didn't return a result. Please check that all required API keys (e.g. brave_api_key for web search) are configured in secrets.json."
                    self.logger.warning(
                        f"Backend {self.config.active_backend} returned success with empty text for "
                        f"{item.request_id} — treating as recoverable tool failure"
                    )
                    self._mark_error(err_msg)
                    if not item.silent:
                        await self.send_long_message(
                            chat_id=item.chat_id,
                            text=err_msg,
                            request_id=item.request_id,
                            purpose="error",
                        )
                    await self._notify_request_listeners(
                        item.request_id,
                        {
                            "request_id": item.request_id,
                            "success": False,
                            "text": None,
                            "error": err_msg,
                            "source": item.source,
                            "summary": item.summary,
                        },
                    )
                elif response.is_success and response.text:
                    self._mark_success()
                    await self._notify_request_listeners(
                        item.request_id,
                        {
                            "request_id": item.request_id,
                            "success": True,
                            "text": response.text,
                            "error": None,
                            "source": item.source,
                            "summary": item.summary,
                        },
                    )
                    if not item.silent:
                        self.last_response = {
                            "chat_id": item.chat_id,
                            "text": response.text,
                            "request_id": item.request_id,
                        }
                        memory_user_text = item.prompt
                        if item.source.lower() in {"document", "photo", "voice", "audio", "video", "sticker"}:
                            memory_user_text = f"[{item.source}] {item.summary}"
                        if item.source not in {"startup", "system"} and not is_bridge_request:
                            self.memory_store.record_turn("user", item.source, memory_user_text)
                            self.memory_store.record_turn("assistant", self.config.active_backend, response.text)
                            self.memory_store.record_exchange(memory_user_text, response.text, item.source)
                        if not is_bridge_request:
                            self.handoff_builder.append_transcript("user", item.prompt, item.source)
                            self.handoff_builder.append_transcript("assistant", response.text)
                            self.handoff_builder.refresh_recent_context()
                        if not item.deliver_to_telegram:
                            continue
                        _print_final_response(self.name, response.text)
                        send_elapsed_s, chunk_count = await self.send_long_message(
                            chat_id=item.chat_id,
                            text=response.text,
                            request_id=item.request_id,
                            purpose="response",
                        )
                        await self._send_voice_reply(item.chat_id, response.text, item.request_id)
                        total_elapsed_s = (datetime.now() - queued_at).total_seconds()
                        self.logger.info(
                            f"Completed {item.request_id} delivery via {self.config.active_backend} "
                            f"(queue_wait_s={queue_wait_s:.2f}, backend_s={backend_elapsed:.2f}, "
                            f"telegram_send_s={send_elapsed_s:.2f}, total_s={total_elapsed_s:.2f}, "
                            f"chunks={chunk_count})"
                        )
                        self._log_maintenance(item, "send_success", text_len=len(response.text or ""))
                else:
                    err_msg = response.error or "Unknown error"
                    self._mark_error(err_msg)
                    await self._notify_request_listeners(
                        item.request_id,
                        {
                            "request_id": item.request_id,
                            "success": False,
                            "text": None,
                            "error": err_msg,
                            "source": item.source,
                            "summary": item.summary,
                        },
                    )
                    if not item.silent:
                        self.error_logger.error(
                            f"Flex Backend error for {item.request_id} "
                            f"({self.config.active_backend}, source={item.source}): {err_msg}"
                        )
                        if self._should_retry_codex_scheduler_failure(item, err_msg):
                            self._schedule_codex_scheduler_retry(item)
                        if not item.deliver_to_telegram:
                            continue
                        send_elapsed_s, chunk_count = await self.send_long_message(
                            chat_id=item.chat_id,
                            text=f"Flex Backend Error ({self.config.active_backend}): {err_msg}",
                            request_id=item.request_id,
                            purpose="error",
                        )
                        total_elapsed_s = (datetime.now() - queued_at).total_seconds()
                        self.logger.info(
                            f"Completed {item.request_id} error delivery via {self.config.active_backend} "
                            f"(queue_wait_s={queue_wait_s:.2f}, backend_s={backend_elapsed:.2f}, "
                            f"telegram_send_s={send_elapsed_s:.2f}, total_s={total_elapsed_s:.2f}, "
                            f"chunks={chunk_count})"
                        )
                        self._log_maintenance(item, "send_error", error_excerpt=_safe_excerpt(err_msg, 200))

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._mark_error(str(e))
                self.error_logger.exception(f"Error in flex queue processing: {e}")
                self.is_generating = False
            finally:
                self.current_request_meta = None
                if item is not None:
                    self.queue.task_done()

    def get_bot_commands(self) -> list[BotCommand]:
        return [
            BotCommand("help", "Show help menu"),
            BotCommand("start", "Start another stopped agent"),
            BotCommand("status", "View agent status"),
            BotCommand("voice", "Toggle native voice replies"),
            BotCommand("whisper", "Set Whisper model size [small|medium|large]"),
            BotCommand("active", "Toggle proactive heartbeat"),
            BotCommand("fyi", "Refresh bridge environment awareness"),
            BotCommand("debug", "Run in strict debug mode"),
            BotCommand("skill", "Browse and run skills"),
            BotCommand("backend", "Show backend buttons (+ means context)"),
            BotCommand("handoff", "Fresh session with recent continuity"),
            BotCommand("park", "List or save parked topics"),
            BotCommand("load", "Restore a parked topic"),
            BotCommand("mode", "Switch fixed/flex mode"),
            BotCommand("model", "View or change model"),
            BotCommand("effort", "View or change effort"),
            BotCommand("new", "Start a fresh session"),
            BotCommand("clear", "Clear media/history"),
            BotCommand("stop", "Stop execution"),
            BotCommand("reboot", "Hot restart agents"),
            BotCommand("terminate", "Shut down this agent"),
            BotCommand("retry", "Resend response or rerun prompt"),
            BotCommand("verbose", "Toggle verbose long-task status [on|off]"),
            BotCommand("think", "Toggle thinking trace display [on|off]"),
            BotCommand("jobs", "Show cron and heartbeat jobs"),
            BotCommand("logo", "Play startup animation"),
            BotCommand("wa_on", "Start WhatsApp transport"),
            BotCommand("wa_off", "Stop WhatsApp transport"),
            BotCommand("wa_send", "Send a WhatsApp message"),
            BotCommand("sys", "Manage system prompt slots"),
            BotCommand("credit", "Check API credit/usage"),
        ]

    async def shutdown(self):
        self.logger.info(f"Shutting down flex agent '{self.name}'...")
        self.is_shutting_down = True
        for task in list(self._scheduled_retry_tasks):
            task.cancel()
        for task in list(self._scheduled_retry_tasks):
            with suppress(asyncio.CancelledError):
                await task
        # Cancel in-flight background tasks (is_shutting_down suppresses notifications)
        for task in list(self._background_tasks):
            task.cancel()
        for task in list(self._background_tasks):
            with suppress(asyncio.CancelledError):
                await task
        if self.process_task:
            self.process_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.process_task
            self.process_task = None
        await self.backend_manager.shutdown()
        self._mark_runtime_shutdown(clean=True)
        
        if self.startup_success:
            for action in (self.app.updater.stop, self.app.stop, self.app.shutdown):
                try:
                    await action()
                except Exception as e:
                    self.error_logger.warning(f"Shutdown warning: {e}")
            self.logger.info("Telegram app shut down cleanly.")
