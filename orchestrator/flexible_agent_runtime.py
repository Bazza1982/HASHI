from __future__ import annotations
import html
import re
import sys
import time
import asyncio
import inspect
import logging
import shutil
import sqlite3
from uuid import uuid4
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Optional, Any
import json

import aiohttp
import yaml
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
    is_cli_backend,
    normalize_effort,
    normalize_model,
)
from orchestrator.memory_index import MemoryIndex
from orchestrator.handoff_builder import HandoffBuilder
from orchestrator.habits import HabitStore
from orchestrator.media_utils import is_image_file, normalize_image_file
from orchestrator.parked_topics import ParkedTopicStore
from orchestrator.usecomputer_mode import (
    build_usecomputer_task_prompt,
    get_usecomputer_examples_text,
    get_usecomputer_status,
    set_usecomputer_mode,
)
from orchestrator.skill_manager import SkillDefinition, SkillManager
from orchestrator.voice_manager import VoiceManager
from orchestrator.private_wol import describe_wol_targets, private_wol_available, run_private_wol
from orchestrator.workzone import access_root_for_workzone, build_workzone_prompt, clear_workzone, load_workzone, resolve_workzone_input, save_workzone
from orchestrator.wrapper_mode import load_wrapper_config

HABIT_BROWSER_PAGE_SIZE = 5

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
        self._transfer_state: dict | None = None
        self._suppressed_transfer_results: list[dict[str, Any]] = []
        # /long ... /end buffering
        self._long_buffer: list[str] = []
        self._long_buffer_active: bool = False
        self._long_buffer_chat_id: int | None = None
        self._long_buffer_timeout_task: asyncio.Task | None = None
        # Hashi Remote subprocess
        self._remote_process: asyncio.subprocess.Process | None = None
        self.skill_manager = skill_manager
        self.agent_fyi_path = self.global_config.project_root / "docs" / "AGENT_FYI.md"
        self._pending_session_primer: str | None = None
        self._pending_auto_recall_context: str | None = None

        self.app = ApplicationBuilder().token(self.token).get_updates_connection_pool_size(8).build()

        # Workspace structure
        self.workspace_dir = config.workspace_dir
        # Load persisted verbose preference (.verbose_off file presence = OFF, absence = ON)
        self._verbose: bool = not (self.workspace_dir / ".verbose_off").exists()
        # Load persisted think preference (.think_off file presence = OFF, absence = ON)
        self._think: bool = not (self.workspace_dir / ".think_off").exists()
        self._think_buffer: list[str] = []
        self._openrouter_think_chunk: str = ""
        self._last_openrouter_think_snippet: str | None = None
        self._thinking_chars_this_req: int = 0   # CLI thinking token estimation
        self._last_full_prompt_tokens: int = 0   # set before each request for bg-mode usage
        self._last_prompt_audit: dict = {}        # prompt section breakdown for token audit
        self.memory_dir = self.workspace_dir / "memory"
        self.sys_prompt_manager = SysPromptManager(self.workspace_dir)
        self.backend_state_dir = self.workspace_dir / "backend_state"
        self.transcript_log_path = self.workspace_dir / "transcript.jsonl"
        self.recent_context_path = self.workspace_dir / "recent_context.jsonl"
        self.handoff_path = self.workspace_dir / "handoff.md"
        self.state_path = self.workspace_dir / "state.json"
        from orchestrator.project_chat_logger import ProjectChatLogger
        self.project_chat_logger = ProjectChatLogger(self.workspace_dir)
        self.runtime_session_path = self.workspace_dir / ".runtime_session.json"
        self.transfer_state_path = self.workspace_dir / "active_transfer.json"
        self._cos_enabled: bool = (self.workspace_dir / ".cos_on").exists()
        self._workzone_dir: Path | None = load_workzone(self.workspace_dir)
        self._sync_workzone_to_backend_config()
        self.voice_manager = VoiceManager(self.workspace_dir, self.media_dir, ffmpeg_cmd="ffmpeg", secrets=self.secrets)
        self._authorized_telegram_ids = resolve_authorized_telegram_ids(self.config.extra, self.global_config.authorized_id)
        self._active_chat_ids: dict[int, int] = {}  # user_id -> chat_id, populated on first message

        # Safe voice confirmation layer
        self._safevoice_enabled: bool = self._get_skill_state().get("safevoice", True)
        self._pending_voice: dict = {}  # chat_id -> {prompt, summary, media_kind, timestamp}

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
        if self.transfer_state_path.exists():
            try:
                self._transfer_state = json.loads(self.transfer_state_path.read_text(encoding="utf-8"))
            except Exception:
                self._transfer_state = None

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
        self.habit_store = HabitStore(
            self.workspace_dir,
            self.global_config.project_root,
            self.name,
            self._get_agent_class(),
        )

        # Initialize FlexibleBackendManager
        self.backend_manager = FlexibleBackendManager(config, global_config, secrets)

    def _record_active_chat(self, update) -> None:
        """Track the chat_id for each authorized user who messages this bot."""
        user = getattr(update, "effective_user", None)
        chat = getattr(update, "effective_chat", None)
        if user and chat and self._is_authorized_user(user.id):
            self._active_chat_ids[user.id] = chat.id

    def _primary_chat_id(self) -> int:
        """Return the best chat_id for proactive messages (forks, notifications).

        Prefer a chat_id from an authorized user who has actually messaged
        this bot (i.e. has an established conversation).  Fall back to the
        first authorized ID only if no active chat has been recorded yet.
        """
        # Return the first active chat we find, in config order
        for uid in self._authorized_telegram_ids:
            if uid in self._active_chat_ids:
                return self._active_chat_ids[uid]
        # Fallback: original behaviour
        if self._authorized_telegram_ids:
            return self._authorized_telegram_ids[0]
        return self.global_config.authorized_id

    def _get_agent_class(self) -> str:
        extra = self.config.extra or {}
        direct = getattr(self.config, "agent_class", None)
        return (direct or extra.get("agent_class") or "general").strip().lower()

    def _build_habit_sections(self, item: QueuedRequest, prompt: str) -> tuple[list[tuple[str, str]], list[str]]:
        habits = self.habit_store.retrieve(prompt, source=item.source, summary=item.summary)
        if not habits:
            item.active_habits = []
            return [], []
        self.habit_store.mark_triggered(habits)
        item.active_habits = self.habit_store.serialize_habits(habits)
        habit_ids = [habit.habit_id for habit in habits]
        section = self.habit_store.render_prompt_section(habits)
        self.logger.info(
            f"Habit retrieval for {item.request_id}: {len(habit_ids)} matched ({', '.join(habit_ids)})"
        )
        self._log_maintenance(item, "habit_retrieval", habit_ids=",".join(habit_ids), habit_count=len(habit_ids))
        return ([section] if section else []), habit_ids

    def _record_habit_outcome(
        self,
        item: QueuedRequest,
        *,
        success: bool,
        response_text: str | None = None,
        error_text: str | None = None,
    ) -> None:
        active_habits = item.active_habits or []
        if not active_habits:
            return
        try:
            self.habit_store.record_execution_outcome(
                request_id=item.request_id,
                prompt=item.prompt,
                source=item.source,
                summary=item.summary,
                active_habits=active_habits,
                response_text=response_text,
                error_text=error_text,
                success=success,
            )
        except Exception as exc:
            self.error_logger.warning(
                f"Failed to record habit outcome for {item.request_id}: {exc}"
            )

    def _capture_followup_habit_feedback(self, text: str) -> None:
        last_response = self.last_response or {}
        request_id = last_response.get("request_id")
        responded_at = last_response.get("responded_at")
        if not request_id:
            return
        try:
            result = self.habit_store.apply_user_feedback(
                request_id=request_id,
                feedback_text=text,
                responded_at=responded_at,
            )
        except Exception as exc:
            self.error_logger.warning(
                f"Failed to capture habit feedback for {request_id}: {exc}"
            )
            return
        if not result:
            return
        self.maintenance_logger.info(
            f"Habit follow-up feedback for {request_id}: "
            f"sentiment={result.sentiment} updated_events={result.updated_events} "
            f"habits={','.join(result.updated_habits)}"
        )

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

        # help/status/new/fresh/wipe/clear/model/effort/mode should always be available
        self._enabled_commands.update({"help", "status", "new", "fresh", "wipe", "reset", "clear", "memory", "model", "effort", "mode", "wrapper", "core", "wrap", "jobs", "verbose", "think", "voice", "whisper", "transfer", "fork", "cos", "long", "end", "oll"})

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
            self._record_active_chat(update)
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
        skip_memory_injection: bool = False,
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
            skip_memory_injection=skip_memory_injection,
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

    def _sync_workzone_to_backend_config(self) -> None:
        if self.config.extra is None:
            self.config.extra = {}
        if self._workzone_dir is not None:
            self.config.extra["workzone_dir"] = str(self._workzone_dir)
        else:
            self.config.extra.pop("workzone_dir", None)
        backend = getattr(getattr(self, "backend_manager", None), "current_backend", None)
        if backend is not None and getattr(backend, "config", None) is not None:
            if backend.config.extra is None:
                backend.config.extra = {}
            if self._workzone_dir is not None:
                backend.config.extra["workzone_dir"] = str(self._workzone_dir)
            else:
                backend.config.extra.pop("workzone_dir", None)
            registry = getattr(backend, "tool_registry", None)
            if registry is not None:
                if self._workzone_dir is not None:
                    registry.workspace_dir = self._workzone_dir
                    registry.access_root = access_root_for_workzone(backend.config.resolve_access_root(), self._workzone_dir)
                else:
                    registry.workspace_dir = self.workspace_dir
                    registry.access_root = backend.config.resolve_access_root()

    def _workzone_prompt_section(self) -> list[tuple[str, str]]:
        self._workzone_dir = load_workzone(self.workspace_dir)
        self._sync_workzone_to_backend_config()
        backend = getattr(self.backend_manager, "current_backend", None)
        can_access_files = bool(
            backend
            and (
                getattr(getattr(backend, "capabilities", None), "supports_files", False)
                or getattr(backend, "tool_registry", None) is not None
            )
        )
        section = build_workzone_prompt(self._workzone_dir, self.workspace_dir, can_access_files=can_access_files)
        return [section] if section else []

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

    def _get_system_prompt_text(self) -> str:
        """Return combined system prompt text for token estimation (CLI backends)."""
        parts = []
        try:
            md_path = getattr(self.config, "system_md", None)
            if md_path and Path(md_path).exists():
                parts.append(Path(md_path).read_text(encoding="utf-8"))
        except Exception:
            pass
        try:
            for text in self.sys_prompt_manager.get_active_texts():
                parts.append(text)
        except Exception:
            pass
        return "\n".join(parts)

    def get_runtime_metadata(self) -> dict:
        return {
            "id": self.name,
            "name": self.name,
            "display_name": self.get_display_name(),
            "emoji": self.get_agent_emoji(),
            "engine": self.config.active_backend,
            "active_backend": self.config.active_backend,
            "model": self.get_current_model(),
            "allowed_backends": [dict(backend) for backend in self.config.allowed_backends],
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

    def _detect_instance_name(self) -> str:
        from orchestrator.ticket_manager import detect_instance

        return detect_instance(self.global_config.project_root)

    def _normalize_instance_name(self, value: str | None) -> str:
        raw = str(value or "").strip()
        if not raw:
            return self._detect_instance_name()
        match = re.search(r"(\d+)$", raw, flags=re.IGNORECASE)
        if match:
            return f"HASHI{match.group(1)}"
        if raw.lower() == "usb":
            return "USB"
        return raw.upper()

    def _parse_request_seq(self, request_id: str | None) -> int | None:
        match = re.match(r"^req-(\d+)$", str(request_id or "").strip())
        if not match:
            return None
        return int(match.group(1))

    def _persist_transfer_state(self) -> None:
        if self._transfer_state is None:
            self.transfer_state_path.unlink(missing_ok=True)
            return
        self.transfer_state_path.write_text(
            json.dumps(self._transfer_state, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )

    def _clear_transfer_state(self) -> None:
        self._transfer_state = None
        self._suppressed_transfer_results.clear()
        self._persist_transfer_state()

    def has_active_transfer(self) -> bool:
        return bool(self._transfer_state and self._transfer_state.get("status") in {"pending", "accepted"})

    def _transfer_redirect_text(self) -> str:
        state = self._transfer_state or {}
        target_agent = state.get("target_agent") or "target"
        target_instance = state.get("target_instance") or "unknown"
        transfer_id = state.get("transfer_id") or "unknown"
        return (
            f"This session has been transferred to {target_agent}@{target_instance}.\n"
            f"Continue there. Transfer ID: {transfer_id}"
        )

    def _should_redirect_after_transfer(self) -> bool:
        return bool(self._transfer_state and self._transfer_state.get("status") == "accepted")

    def _should_buffer_during_transfer(self, request_id: str | None) -> bool:
        if not self._transfer_state:
            return False
        status = self._transfer_state.get("status")
        if status not in {"pending", "accepted"}:
            return False
        cutoff_seq = self._transfer_state.get("cutoff_seq")
        req_seq = self._parse_request_seq(request_id)
        return cutoff_seq is not None and req_seq is not None and req_seq <= cutoff_seq

    def _record_suppressed_transfer_result(
        self,
        item: QueuedRequest,
        *,
        success: bool,
        text: str | None = None,
        error: str | None = None,
    ) -> None:
        self._suppressed_transfer_results.append(
            {
                "request_id": item.request_id,
                "chat_id": item.chat_id,
                "success": success,
                "text": text,
                "error": error,
                "summary": item.summary,
                "source": item.source,
            }
        )

    async def _flush_suppressed_transfer_results(self) -> None:
        buffered = list(self._suppressed_transfer_results)
        self._suppressed_transfer_results.clear()
        for entry in buffered:
            text = entry.get("text") if entry.get("success") else f"Flex Backend Error ({self.config.active_backend}): {entry.get('error')}"
            if not text:
                continue
            await self.send_long_message(
                chat_id=entry["chat_id"],
                text=text,
                request_id=entry.get("request_id"),
                purpose="transfer-release",
            )

    def _strip_transfer_accept_prefix(self, item: QueuedRequest, text: str) -> str:
        if not item.source.startswith("bridge-transfer:"):
            return text
        prefix = f"TRANSFER_ACCEPTED {item.source.split(':', 1)[1]}"
        if not text.startswith(prefix):
            return text
        stripped = text[len(prefix):].lstrip()
        if stripped.startswith("\n"):
            stripped = stripped.lstrip()
        return stripped

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
        if item.source.startswith("scheduler") or item.source.startswith("bridge:") or item.source.startswith("bridge-transfer:"):
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

    def _source_requires_manual_permission(self, source: str) -> bool:
        normalized = (source or "").strip().lower()
        if not normalized:
            return True
        automated_prefixes = (
            "scheduler",
            "bridge:",
            "bridge-transfer:",
            "hchat-reply:",
            "cos-query:",
            "ticket:",
            "loop_skill",
            "startup",
        )
        return normalized.startswith(automated_prefixes)

    def _remote_backend_block_reason(self, source: str) -> str | None:
        engine = (self.config.active_backend or "").strip().lower()
        if engine not in {"openrouter-api", "deepseek-api"}:
            return None
        if not self._source_requires_manual_permission(source):
            return None
        return (
            f"Blocked {engine} for source '{source}'. Remote API backends are reserved for user-initiated requests only; "
            "automated/agent-originated flows must not use them."
        )

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

    async def _send_voice_reply(self, chat_id: int, text: str, request_id: str, force: bool = False) -> bool:
        # Guard: skip if Telegram not connected
        if not self.telegram_connected:
            return False
        try:
            asset = await self.voice_manager.synthesize_reply(self.name, request_id, text, force=force)
            if asset is None:
                return False
            max_attempts = 3
            last_error = None
            for attempt in range(1, max_attempts + 1):
                try:
                    with asset.ogg_path.open("rb") as f:
                        await self.app.bot.send_voice(
                            chat_id=chat_id, voice=f,
                            read_timeout=30, write_timeout=30, connect_timeout=15,
                        )
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
            try:
                from tools.token_tracker import get_summary, format_status_line
                _usage_summary = get_summary(self.workspace_dir, session_id=self.session_id_dt)
                lines.append(f"💰 Tokens: {format_status_line(_usage_summary)}")
            except Exception:
                pass
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

    def _habit_db_path(self) -> Path:
        return self.workspace_dir / "habits.sqlite"

    def _load_local_habit_counts(self) -> dict[str, int]:
        db_path = self._habit_db_path()
        counts = {"total": 0, "active": 0, "candidate": 0, "paused": 0, "disabled": 0}
        if not db_path.exists():
            return counts
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END), 0) AS active,
                    COALESCE(SUM(CASE WHEN status = 'candidate' THEN 1 ELSE 0 END), 0) AS candidate,
                    COALESCE(SUM(CASE WHEN status = 'paused' THEN 1 ELSE 0 END), 0) AS paused,
                    COALESCE(SUM(CASE WHEN status = 'disabled' THEN 1 ELSE 0 END), 0) AS disabled
                FROM habits
                WHERE agent_id = ?
                """,
                (self.name,),
            ).fetchone()
        if row:
            counts = {key: int(row[key] or 0) for key in counts}
        return counts

    def _load_local_habit_rows(
        self,
        *,
        offset: int = 0,
        limit: int = HABIT_BROWSER_PAGE_SIZE,
    ) -> tuple[int, list[sqlite3.Row]]:
        db_path = self._habit_db_path()
        if not db_path.exists():
            return 0, []
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            total = int(
                conn.execute("SELECT COUNT(*) FROM habits WHERE agent_id = ?", (self.name,)).fetchone()[0] or 0
            )
            rows = conn.execute(
                """
                SELECT habit_id, status, enabled, habit_type, title, instruction, task_type, confidence
                FROM habits
                WHERE agent_id = ?
                ORDER BY
                    CASE status WHEN 'active' THEN 0 WHEN 'candidate' THEN 1 WHEN 'paused' THEN 2 ELSE 3 END,
                    confidence DESC,
                    updated_at DESC
                LIMIT ? OFFSET ?
                """,
                (self.name, limit, max(offset, 0)),
            ).fetchall()
        return total, rows

    def _habit_status_button_label(self, current: str, target: str) -> str:
        return {
            "active": "✅ Active" if current == "active" else "Active",
            "paused": "⏸ Pause" if current == "paused" else "Pause",
            "disabled": "❌ Disable" if current == "disabled" else "Disable",
        }[target]

    def _build_habit_browser_view(
        self,
        *,
        offset: int = 0,
        selected_habit_id: str | None = None,
        notice: str | None = None,
    ) -> tuple[str, InlineKeyboardMarkup]:
        import html as _html

        counts = self._load_local_habit_counts()
        total, rows = self._load_local_habit_rows(offset=offset)
        lines = [
            "<b>🧠 Local Habits</b>",
            f"Agent: <code>{_html.escape(self.name)}</code>",
            "",
            (
                f"📊 Total <b>{counts['total']}</b> • "
                f"🟢 Active <b>{counts['active']}</b> • "
                f"🟡 Candidate <b>{counts['candidate']}</b> • "
                f"⏸ Paused <b>{counts['paused']}</b> • "
                f"🔴 Disabled <b>{counts['disabled']}</b>"
            ),
        ]
        if notice:
            lines.extend(["", f"✨ {_html.escape(notice)}"])
        lines.append("")
        buttons: list[list[InlineKeyboardButton]] = []
        if not rows:
            lines.append("No local habits yet.")
        for idx, row in enumerate(rows, start=offset + 1):
            title = str(row["title"] or "").strip()
            instruction = str(row["instruction"] or "").strip()
            label = title or instruction or "(untitled)"
            task_type = str(row["task_type"] or "general")
            status = str(row["status"] or "active")
            habit_type = str(row["habit_type"] or "do")
            confidence = float(row["confidence"] or 0.0)
            icon = {"active": "🟢", "candidate": "🟡", "paused": "⏸", "disabled": "🔴"}.get(status, "⚪")
            type_icon = {"do": "✅", "avoid": "🚫"}.get(habit_type, "•")
            lines.append(f"{idx}. {icon} <b>{_html.escape(label[:80])}</b>")
            lines.append(
                f"   {type_icon} <code>{_html.escape(habit_type)}</code> • "
                f"<code>{_html.escape(task_type)}</code> • conf <b>{confidence:.2f}</b>"
            )
            if selected_habit_id == row["habit_id"]:
                lines.append(f"   💡 {_html.escape(instruction[:280])}")
                lines.append(f"   🆔 <code>{_html.escape(str(row['habit_id']))}</code>")
            lines.append("")
            habit_id = str(row["habit_id"])
            buttons.append([
                InlineKeyboardButton("🔍 Detail", callback_data=f"skill:habits:view:{habit_id}:{offset}"),
                InlineKeyboardButton(
                    self._habit_status_button_label(status, "active"),
                    callback_data=f"skill:habits:set:{habit_id}:active:{offset}",
                ),
            ])
            buttons.append([
                InlineKeyboardButton(
                    self._habit_status_button_label(status, "paused"),
                    callback_data=f"skill:habits:set:{habit_id}:paused:{offset}",
                ),
                InlineKeyboardButton(
                    self._habit_status_button_label(status, "disabled"),
                    callback_data=f"skill:habits:set:{habit_id}:disabled:{offset}",
                ),
            ])
        nav: list[InlineKeyboardButton] = []
        prev_offset = max(offset - HABIT_BROWSER_PAGE_SIZE, 0)
        next_offset = offset + HABIT_BROWSER_PAGE_SIZE
        if offset > 0:
            nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"skill:habits:list:{prev_offset}"))
        nav.append(InlineKeyboardButton("🔄 Refresh", callback_data=f"skill:habits:list:{offset}"))
        if next_offset < total:
            nav.append(InlineKeyboardButton("Next ▶", callback_data=f"skill:habits:list:{next_offset}"))
        if nav:
            buttons.append(nav)
        buttons.append([InlineKeyboardButton("📋 Governance Queue", callback_data="skill:habits:queue:0")])
        return "\n".join(lines).strip(), InlineKeyboardMarkup(buttons)

    def _set_local_habit_status(self, habit_id: str, target_status: str) -> tuple[bool, str]:
        db_path = self._habit_db_path()
        if not db_path.exists():
            return False, "Habit store not found."
        enabled = 1 if target_status == "active" else 0
        now = datetime.now().isoformat(timespec="seconds")
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT habit_id, status FROM habits WHERE habit_id = ? AND agent_id = ?",
                (habit_id, self.name),
            ).fetchone()
            if row is None:
                return False, "Habit not found."
            old_status = str(row["status"] or "")
            conn.execute(
                "UPDATE habits SET status = ?, enabled = ?, updated_at = ? WHERE habit_id = ? AND agent_id = ?",
                (target_status, enabled, now, habit_id, self.name),
            )
            conn.execute(
                """
                INSERT INTO habit_state_changes (habit_id, change_type, old_value, new_value, reason, changed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (habit_id, "telegram_status", old_status, target_status, f"telegram:{self.name}", now),
            )
        return True, f"Habit set to {target_status}."

    def _build_habit_governance_view(self) -> str:
        import html as _html

        project_root = self.workspace_dir.parent.parent
        rows = HabitStore.list_copy_recommendations(project_root=project_root, limit=100)
        shared_rows = HabitStore.list_shared_patterns(project_root=project_root, limit=100)
        counts: dict[str, int] = {status: 0 for status in ("pending", "approved", "applied", "rejected", "obsolete")}
        for row in rows:
            counts[row.status] = counts.get(row.status, 0) + 1
        lines = [
            "<b>📋 Habit Governance Queue</b>",
            f"Agent: <code>{_html.escape(self.name)}</code>",
            "",
            (
                f"Pending <b>{counts['pending']}</b> • Approved <b>{counts['approved']}</b> • "
                f"Applied <b>{counts['applied']}</b> • Rejected <b>{counts['rejected']}</b> • "
                f"Obsolete <b>{counts['obsolete']}</b>"
            ),
            f"🤝 Active shared patterns <b>{len(shared_rows)}</b>",
        ]
        pending = [row for row in rows if row.status == "pending"][:5]
        lines.append("")
        lines.append("<b>Recent governance items</b>")
        if not pending:
            lines.append("• No copy recommendations right now.")
        else:
            for row in pending:
                lines.append(
                    "• "
                    f"<code>{_html.escape(row.source_agent)}</code> → "
                    f"<code>{_html.escape(row.target_agent)}</code> "
                    f"for <code>{_html.escape(row.habit_id)}</code>"
                )
        lines.append("")
        lines.append("Tip: use <code>/skill habits</code> to return to local habits.")
        return "\n".join(lines)

    async def _render_skill_jobs(self, update_or_query, kind: str):
        from orchestrator.agent_runtime import _build_jobs_with_buttons
        text, markup = _build_jobs_with_buttons(self.name, self.skill_manager, filter_agent=self.name)
        if hasattr(update_or_query, "edit_message_text"):
            await update_or_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        else:
            await self._reply_text(update_or_query, text, parse_mode="HTML", reply_markup=markup)

    async def invoke_scheduler_skill(self, skill_id: str, args: str, task_id: str) -> tuple[bool, str | None]:
        if not self.skill_manager:
            message = f"Scheduler skill invocation requested without skill manager: {skill_id}"
            self.error_logger.error(message)
            return False, message
        skill = self.skill_manager.get_skill(skill_id)
        if skill is None:
            message = f"Unknown scheduler skill: {skill_id}"
            self.error_logger.error(message)
            return False, message
        if skill.type == "toggle":
            message = f"Toggle skill cannot be scheduled: {skill_id}"
            self.error_logger.error(message)
            return False, message
        skill_env = {
            "BRIDGE_ACTIVE_BACKEND": self.config.active_backend,
            "BRIDGE_ACTIVE_MODEL": self.get_current_model(),
        }
        if skill.type == "action":
            ok, text = await self.skill_manager.run_action_skill(
                skill,
                self.workspace_dir,
                args=args,
                extra_env=skill_env,
            )
            if ok and text:
                await self.send_long_message(
                    chat_id=self._primary_chat_id(),
                    text=text,
                    request_id=f"skill-{task_id}",
                    purpose="scheduler-skill",
                )
            elif text:
                self.error_logger.error(text)
            return ok, text
        prompt = self.skill_manager.build_prompt_for_skill(skill, args or "")
        if skill.backend:
            allowed = [b["engine"] for b in self.config.allowed_backends]
            if skill.backend not in allowed:
                message = f"Scheduled prompt skill {skill.id} targets disallowed backend {skill.backend}."
                self.error_logger.error(message)
                return False, message
            if self.config.active_backend != skill.backend:
                self._workzone_dir = load_workzone(self.workspace_dir)
                self._sync_workzone_to_backend_config()
                switch_ok = await self.backend_manager.switch_backend(skill.backend)
                if not switch_ok:
                    message = f"Failed to switch backend for scheduled skill {skill.id}."
                    self.error_logger.error(message)
                    return False, message
                self._sync_workzone_to_backend_config()
                backend = self.backend_manager.current_backend
                if backend and getattr(backend.capabilities, "supports_sessions", False):
                    await backend.handle_new_session()
        await self.enqueue_request(
            chat_id=self._primary_chat_id(),
            prompt=prompt,
            source="scheduler-skill",
            summary=f"Skill Task [{task_id}]",
            silent=False,
        )
        return True, f"Scheduled prompt skill queued: {skill.id}"

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
        if self._should_redirect_after_transfer() and not source.startswith(("bridge-transfer:", "bridge-fork:")):
            if deliver_to_telegram:
                await self.send_long_message(
                    self._primary_chat_id(),
                    self._transfer_redirect_text(),
                    request_id=f"transfer-redirect-{uuid4().hex[:8]}",
                    purpose="transfer-redirect",
                )
            return None
        _print_user_message(self.name, text)
        return await self.enqueue_request(
            self._primary_chat_id(),
            text,
            source,
            _safe_excerpt(text),
            deliver_to_telegram=deliver_to_telegram,
        )

    async def _hchat_route_reply(self, item, response_text: str):
        """Route hchat reply back to the sender.

        Supports both [hchat from name] and [hchat from name@INSTANCE] header formats.
        Priority:
        1. Local runtime for local senders.
        2. Explicit cross-instance reply via send_hchat(name@INSTANCE).
        3. contacts.json entry for legacy external callers.
        4. Cross-instance delivery via send_hchat(name) when no instance is known.
        """
        try:
            from tools.hchat_send import parse_hchat_message, parse_return_address
            sender = parse_return_address(item.prompt)
        except Exception:
            sender = None
        if not sender:
            return
        parsed_hchat = parse_hchat_message(item.prompt)
        body_text = str((parsed_hchat or {}).get("body") or "").lstrip().lower()
        if body_text.startswith("[hchat reply from "):
            self.logger.info("Hchat auto-reply suppressed for reply message to avoid loop")
            return
        sender_name = sender["agent"].lower()
        sender_instance = (sender.get("instance_id") or "").upper()
        try:
            from tools.hchat_send import _get_instance_id, _load_config
            local_instance = str(_get_instance_id(_load_config()) or "").upper()
        except Exception:
            local_instance = ""
        reply_text = f"[hchat reply from {self.name}] {response_text}"

        # ── 1. Try local runtime only when sender is local/unspecified ────────
        if not sender_instance or sender_instance == local_instance:
            orchestrator = getattr(self, "orchestrator", None)
            if orchestrator:
                for rt in getattr(orchestrator, "runtimes", []):
                    if getattr(rt, "name", "") == sender_name and hasattr(rt, "enqueue_api_text"):
                        try:
                            await rt.enqueue_api_text(
                                reply_text,
                                source=f"hchat-reply:{self.name}",
                                deliver_to_telegram=True,
                            )
                            self.logger.info(f"Hchat reply routed to local runtime '{sender_name}'")
                        except Exception as e:
                            self.logger.warning(f"Failed to route hchat reply to '{sender_name}': {e}")
                        return

        # ── 2. Explicit cross-instance reply when sender instance is known ────
        if sender_instance and sender_instance != local_instance:
            try:
                from tools.hchat_send import send_hchat
                import functools
                loop = asyncio.get_event_loop()
                ok = await loop.run_in_executor(
                    None,
                    functools.partial(send_hchat, sender_name, self.name, reply_text, target_instance=sender_instance),
                )
                if ok:
                    self.logger.info(f"Hchat reply cross-instance delivered to '{sender_name}@{sender_instance}'")
                    return
            except Exception as e:
                self.logger.warning(f"Hchat reply: cross-instance delivery to '{sender_name}@{sender_instance}' failed: {e}")

        # ── 3. Fall back to contacts.json (legacy external callers) ───────────
        try:
            from tools.hchat_send import _get_cached_route
            contact = _get_cached_route(sender_name)
            if contact:
                host = contact.get("host") or "127.0.0.1"
                if host in ("0.0.0.0", "::", ""):
                    host = "127.0.0.1"
                wb_port = contact.get("wb_port") or contact.get("port")
                if wb_port:
                    url = f"http://{host}:{wb_port}/api/chat"
                    payload = {
                        "agent": sender_name,
                        "text": reply_text,
                    }
                    async with aiohttp.ClientSession() as session:
                        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status < 300:
                                self.logger.info(f"Hchat reply delivered to external '{sender_name}' via {url}")
                            else:
                                body = await resp.text()
                                self.logger.warning(f"Hchat reply to '{sender_name}' got HTTP {resp.status}: {body[:200]}")
                    return
        except Exception as e:
            self.logger.warning(f"Hchat reply: contacts fallback for '{sender_name}' failed: {e}")

        # ── 4. Last resort: cross-instance delivery without instance hint ─────
        try:
            from tools.hchat_send import send_hchat
            import functools
            loop = asyncio.get_event_loop()
            ok = await loop.run_in_executor(None, functools.partial(send_hchat, sender_name, self.name, reply_text))
            if ok:
                self.logger.info(f"Hchat reply cross-instance delivered to '{sender_name}'")
                return
        except Exception as e:
            self.logger.warning(f"Hchat reply: cross-instance delivery to '{sender_name}' failed: {e}")

        self.logger.warning(
            f"Hchat reply: sender '{sender_name}' not found locally, in contacts, or cross-instance"
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
        if self._should_redirect_after_transfer():
            if deliver_to_telegram:
                await self.send_long_message(
                    self._primary_chat_id(),
                    self._transfer_redirect_text(),
                    request_id=f"transfer-redirect-{uuid4().hex[:8]}",
                    purpose="transfer-redirect",
                )
            return None
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
        self.app.add_handler(CommandHandler("safevoice", self._wrap_cmd("safevoice", self.cmd_safevoice)))
        self.app.add_handler(CommandHandler("say", self._wrap_cmd("say", self.cmd_say)))
        self.app.add_handler(CommandHandler("loop", self._wrap_cmd("loop", self.cmd_loop)))
        self.app.add_handler(CommandHandler("whisper", self._wrap_cmd("whisper", self.cmd_whisper)))
        self.app.add_handler(CommandHandler("active", self._wrap_cmd("active", self.cmd_active)))
        self.app.add_handler(CommandHandler("fyi", self._wrap_cmd("fyi", self.cmd_fyi)))
        self.app.add_handler(CommandHandler("debug", self._wrap_cmd("debug", self.cmd_debug)))
        self.app.add_handler(CommandHandler("skill", self._wrap_cmd("skill", self.cmd_skill)))
        self.app.add_handler(CommandHandler("backend", self._wrap_cmd("backend", self.cmd_backend)))
        self.app.add_handler(CommandHandler("handoff", self._wrap_cmd("handoff", self.cmd_handoff)))
        self.app.add_handler(CommandHandler("ticket", self._wrap_cmd("ticket", self.cmd_ticket)))
        self.app.add_handler(CommandHandler("park", self._wrap_cmd("park", self.cmd_park)))
        self.app.add_handler(CommandHandler("load", self._wrap_cmd("load", self.cmd_load)))
        self.app.add_handler(CommandHandler("transfer", self._wrap_cmd("transfer", self.cmd_transfer)))
        self.app.add_handler(CommandHandler("fork", self._wrap_cmd("fork", self.cmd_fork)))
        self.app.add_handler(CommandHandler("cos", self._wrap_cmd("cos", self.cmd_cos)))
        self.app.add_handler(CommandHandler("model", self._wrap_cmd("model", self.cmd_model)))
        self.app.add_handler(CommandHandler("effort", self._wrap_cmd("effort", self.cmd_effort)))
        self.app.add_handler(CallbackQueryHandler(self.callback_model, pattern=r"^(model|backend|bmodel|effort|backend_menu)"))
        self.app.add_handler(CallbackQueryHandler(self.callback_voice, pattern=r"^voice:"))
        self.app.add_handler(CallbackQueryHandler(self.callback_safevoice, pattern=r"^safevoice:"))
        self.app.add_handler(CallbackQueryHandler(self.callback_start_agent, pattern=r"^startagent:"))
        self.app.add_handler(CommandHandler("agents", self._wrap_cmd("agents", self.cmd_agents)))
        self.app.add_handler(CallbackQueryHandler(self.callback_agents, pattern=r"^agents:"))
        self.app.add_handler(CallbackQueryHandler(self.callback_skill, pattern=r"^(skill|skilljob):"))
        self.app.add_handler(CallbackQueryHandler(self.callback_toggle, pattern=r"^tgl:"))
        self.app.add_handler(CommandHandler("mode", self._wrap_cmd("mode", self.cmd_mode)))
        self.app.add_handler(CommandHandler("wrapper", self._wrap_cmd("wrapper", self.cmd_wrapper)))
        self.app.add_handler(CommandHandler("core", self._wrap_cmd("core", self.cmd_core)))
        self.app.add_handler(CommandHandler("wrap", self._wrap_cmd("wrap", self.cmd_wrap)))
        self.app.add_handler(CommandHandler("workzone", self._wrap_cmd("workzone", self.cmd_workzone)))
        self.app.add_handler(CommandHandler("worzone", self._wrap_cmd("worzone", self.cmd_workzone)))
        self.app.add_handler(CommandHandler("new", self._wrap_cmd("new", self.cmd_new)))
        self.app.add_handler(CommandHandler("fresh", self._wrap_cmd("fresh", self.cmd_fresh)))
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
        self.app.add_handler(CommandHandler("cron", self._wrap_cmd("cron", self.cmd_cron)))
        self.app.add_handler(CommandHandler("heartbeat", self._wrap_cmd("heartbeat", self.cmd_heartbeat)))
        self.app.add_handler(CommandHandler("timeout", self._wrap_cmd("timeout", self.cmd_timeout)))
        self.app.add_handler(CommandHandler("hchat", self._wrap_cmd("hchat", self.cmd_hchat)))
        self.app.add_handler(CommandHandler("group", self._wrap_cmd("group", self.cmd_group)))
        self.app.add_handler(CallbackQueryHandler(self.callback_group, pattern=r"^group:"))
        self.app.add_handler(CommandHandler("token", self._wrap_cmd("token", self.cmd_token)))
        self.app.add_handler(CommandHandler("usage", self._wrap_cmd("usage", self.cmd_usage)))
        self.app.add_handler(CommandHandler("logo", self._wrap_cmd("logo", self.cmd_logo)))
        self.app.add_handler(CommandHandler("move", self._wrap_cmd("move", self.cmd_move)))
        self.app.add_handler(CallbackQueryHandler(self.callback_move, pattern=r"^move:"))
        self.app.add_handler(CommandHandler("wa_on", self._wrap_cmd("wa_on", self.cmd_wa_on)))
        self.app.add_handler(CommandHandler("wa_off", self._wrap_cmd("wa_off", self.cmd_wa_off)))
        self.app.add_handler(CommandHandler("wa_send", self._wrap_cmd("wa_send", self.cmd_wa_send)))
        self.app.add_handler(CommandHandler("usecomputer", self._wrap_cmd("usecomputer", self.cmd_usecomputer)))
        self.app.add_handler(CommandHandler("usercomputer", self._wrap_cmd("usercomputer", self.cmd_usercomputer)))
        self.app.add_handler(CommandHandler("long", self._wrap_cmd("long", self.cmd_long)))
        self.app.add_handler(CommandHandler("end", self._wrap_cmd("end", self.cmd_end)))
        self.app.add_handler(CommandHandler("remote", self._wrap_cmd("remote", self.cmd_remote)))
        self.app.add_handler(CommandHandler("oll", self._wrap_cmd("oll", self.cmd_oll)))
        self.app.add_handler(CommandHandler("wol", self._wrap_cmd("wol", self.cmd_wol)))
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
        import time
        from telegram.error import Conflict, NetworkError, TimedOut
        err_text = str(error) or "<no error message>"
        now = time.monotonic()
        if isinstance(error, (NetworkError, TimedOut)) and not isinstance(error, Conflict):
            self._last_network_error_ts = now
        if isinstance(error, Conflict):
            last_net_err = getattr(self, "_last_network_error_ts", 0)
            if now - last_net_err < 120:
                self.telegram_logger.warning(
                    f"Telegram polling self-conflict for '{self.name}': network recovered and new poll "
                    f"displaced the stale one. This is harmless and auto-recovers. ({err_text})"
                )
            else:
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

    # ── /agents ────────────────────────────────────────────────────────────────

    def _build_agents_view(self, orchestrator) -> tuple[str, "InlineKeyboardMarkup"]:
        import re as _re
        all_agents = orchestrator.get_all_agents_raw()
        running_names = set(orchestrator._runtime_map().keys())
        starting_names = set(orchestrator._startup_tasks.keys())

        lines = ["<b>📋 HASHI Agents</b>"]
        rows = []

        for agent in all_agents:
            name = agent.get("name", "?")
            display = agent.get("display_name", name)
            emoji = agent.get("emoji", "🤖")
            is_active = agent.get("is_active", True)

            if name in starting_names:
                status_icon, status_text = "⏳", "starting"
            elif name in running_names:
                status_icon, status_text = "🟢", "running"
            elif is_active:
                status_icon, status_text = "⚪", "stopped"
            else:
                status_icon, status_text = "🔴", "inactive"

            lines.append(f"{status_icon} <b>{name}</b> — {display} [{status_text}]")

            btn_row = []
            if is_active:
                btn_row.append(InlineKeyboardButton(f"❌ {name}", callback_data=f"agents:deactivate:{name}"))
            else:
                btn_row.append(InlineKeyboardButton(f"✅ {name}", callback_data=f"agents:activate:{name}"))

            if name in starting_names:
                btn_row.append(InlineKeyboardButton("⏳", callback_data="agents:noop"))
            elif name in running_names:
                btn_row.append(InlineKeyboardButton("⏹ Stop", callback_data=f"agents:stop:{name}"))
            elif is_active:
                btn_row.append(InlineKeyboardButton("▶ Start", callback_data=f"agents:start:{name}"))

            btn_row.append(InlineKeyboardButton("🗑", callback_data=f"agents:delete:{name}"))
            rows.append(btn_row)

        rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="agents:refresh")])
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    async def cmd_agents(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = context.args or []
        if args and args[0] == "add":
            await self._cmd_agents_add(update, context)
            return
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            await self._reply_text(update, "Agent management unavailable.")
            return
        text, markup = self._build_agents_view(orchestrator)
        await self._reply_text(update, text, reply_markup=markup, parse_mode="HTML")

    async def _cmd_agents_add(self, update: Update, context: Any):
        import re as _re
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            await self._reply_text(update, "Agent management unavailable.")
            return
        args = context.args or []
        # args: ["add", "<id>", "<display_name_parts...>", "[token]"]
        if len(args) < 3:
            await self._reply_text(update, "Usage: /agents add <id> <display_name> [telegram_token]")
            return
        new_id = args[1]
        if not _re.match(r'^[a-zA-Z0-9_]+$', new_id):
            await self._reply_text(update, "Agent ID must be alphanumeric with underscores only.")
            return
        # If last arg looks like a Telegram token (digits:letters) treat as token
        if len(args) >= 4 and _re.match(r'^\d+:[A-Za-z0-9_-]+$', args[-1]):
            token = args[-1]
            display_name = " ".join(args[2:-1])
        else:
            token = None
            display_name = " ".join(args[2:])
        if not display_name:
            display_name = new_id
        ok, msg = orchestrator.add_agent_to_config(new_id, display_name, token)
        await self._reply_text(update, msg)

    async def callback_agents(self, update: Update, context: Any):
        query = update.callback_query
        if not self._is_authorized_user(query.from_user.id):
            return
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            await query.answer("Agent management unavailable.", show_alert=True)
            return
        data = query.data or ""
        parts = data.split(":", 2)
        action = parts[1] if len(parts) > 1 else ""
        name = parts[2] if len(parts) > 2 else ""

        if action in ("refresh", "noop"):
            await query.answer()
            text, markup = self._build_agents_view(orchestrator)
            await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
            return

        if action == "activate":
            await query.answer(f"Activating {name}…")
            orchestrator.set_agent_active(name, True)
        elif action == "deactivate":
            if name in orchestrator._runtime_map():
                await query.answer(f"Stop {name} first.", show_alert=True)
                return
            await query.answer(f"Deactivating {name}…")
            orchestrator.set_agent_active(name, False)
        elif action == "start":
            await query.answer(f"Starting {name}…")
            ok, msg = await orchestrator.start_agent(name)
            if not ok:
                await query.answer(msg, show_alert=True)
                return
        elif action == "stop":
            await query.answer(f"Stopping {name}…")
            ok, msg = await orchestrator.stop_agent(name)
            if not ok:
                await query.answer(msg, show_alert=True)
                return
        elif action == "delete":
            await query.answer()
            confirm_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"⚠️ Confirm delete {name}", callback_data=f"agents:confirmdelete:{name}"),
                InlineKeyboardButton("Cancel", callback_data="agents:refresh"),
            ]])
            await query.edit_message_text(
                f"⚠️ <b>Delete '{name}'?</b>\n\nRemoves from config only — workspace files are kept.",
                reply_markup=confirm_markup,
                parse_mode="HTML",
            )
            return
        elif action == "confirmdelete":
            if name in orchestrator._runtime_map():
                await query.answer(f"Stop {name} first.", show_alert=True)
                return
            await query.answer(f"Deleted {name}.")
            orchestrator.delete_agent_from_config(name)

        text, markup = self._build_agents_view(orchestrator)
        await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")

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
            _f = self.workspace_dir / ".verbose_off"
            if self._verbose:
                _f.unlink(missing_ok=True)
            else:
                _f.touch()
            state = "ON 🔍" if self._verbose else "OFF"
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ ON" if self._verbose else "ON", callback_data="tgl:verbose:on"),
                InlineKeyboardButton("✅ OFF" if not self._verbose else "OFF", callback_data="tgl:verbose:off"),
            ]])
            await query.edit_message_text(f"Verbose mode: {state}", reply_markup=markup)
            await query.answer(f"Verbose {state}")

        elif target == "think":
            self._think = value == "on"
            _f = self.workspace_dir / ".think_off"
            if self._think:
                _f.unlink(missing_ok=True)
            else:
                _f.touch()
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
            elif value == "wrapper":
                if hasattr(backend, "set_session_mode"):
                    backend.set_session_mode(False)
                detail = "Core/wrapper config mode · use /core and /wrap"
            else:
                if hasattr(backend, "set_session_mode"):
                    backend.set_session_mode(False)
                detail = "Full context injection · /backend enabled"
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Fixed" if value == "fixed" else "Fixed", callback_data="tgl:mode:fixed"),
                InlineKeyboardButton("✅ Flex" if value == "flex" else "Flex", callback_data="tgl:mode:flex"),
                InlineKeyboardButton("✅ Wrapper" if value == "wrapper" else "Wrapper", callback_data="tgl:mode:wrapper"),
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

    # ── /move command ────────────────────────────────────────────────────────
    def _load_instances(self) -> dict:
        """Load instances.json from the project root or ~/.hashi/instances.json."""
        candidates = [
            Path(__file__).parent.parent / "instances.json",
            Path.home() / ".hashi" / "instances.json",
        ]
        for p in candidates:
            if p.exists():
                import json as _json
                with open(p) as f:
                    data = _json.load(f)
                return data.get("instances", {})
        return {}

    async def cmd_move(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return

        instances = self._load_instances()
        if not instances:
            await self._reply_text(update, "⚠️ No instances.json found. Create one at the project root.")
            return

        args = context.args or []

        # /move list
        if args and args[0].lower() == "list":
            lines = ["<b>Known HASHI Instances:</b>"]
            for name, inst in instances.items():
                root = inst.get("root") or "(auto)"
                lines.append(f"  • <code>{name}</code> — {inst.get('display_name', '')}  <i>{root}</i>")
            await self._reply_text(update, "\n".join(lines), parse_mode="HTML")
            return

        # /move <agent> <target> [--keep-source] [--sync] [--dry-run]
        if len(args) >= 2:
            agent_id = args[0]
            target = args[1]
            keep = "--keep-source" in args
            sync = "--sync" in args
            dry = "--dry-run" in args
            await self._do_move(update, agent_id, target, instances, keep_source=keep, sync=sync, dry_run=dry)
            return

        # /move <agent> — show target picker
        if len(args) == 1:
            agent_id = args[0]
            await self._move_show_target_picker(update, agent_id, instances)
            return

        # /move — show agent picker first, then target
        await self._move_show_agent_picker(update, instances)

    async def _move_show_agent_picker(self, update: Update, instances: dict):
        """Step 1: pick which agent to move (from current instance)."""
        import json as _json
        root = Path(__file__).parent.parent
        try:
            with open(root / "agents.json") as f:
                data = _json.load(f)
            agents = data if isinstance(data, list) else data.get("agents", [])
            agent_names = [ag.get("name") or ag.get("id", "?") for ag in agents if ag.get("name")]
        except Exception:
            agent_names = []

        if not agent_names:
            await self._reply_text(update, "No agents found in this instance.")
            return

        rows = [[InlineKeyboardButton(f"🤖 {name}", callback_data=f"move:agent:{name}")]
                for name in agent_names]
        markup = InlineKeyboardMarkup(rows)
        await self._reply_text(update, "<b>Move Agent</b> — select agent to move:", parse_mode="HTML", reply_markup=markup)

    async def _move_show_target_picker(self, update: Update, agent_id: str, instances: dict):
        """Step 2: pick target instance."""
        rows = []
        for name, inst in instances.items():
            label = inst.get("display_name", name)
            rows.append([InlineKeyboardButton(f"📦 {label}", callback_data=f"move:target:{agent_id}:{name}")])
        markup = InlineKeyboardMarkup(rows)
        await self._reply_text(
            update,
            f"<b>Move <code>{agent_id}</code></b> — select target instance:",
            parse_mode="HTML",
            reply_markup=markup,
        )

    async def _move_show_options(self, update, agent_id: str, target: str):
        """Step 3: show move options (keep source / sync / encrypt)."""
        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔒 Move + Encrypt", callback_data=f"move:exec:{agent_id}:{target}:enc"),
                InlineKeyboardButton("📋 Move Plain", callback_data=f"move:exec:{agent_id}:{target}:plain"),
            ],
            [
                InlineKeyboardButton("📂 Copy (keep source)", callback_data=f"move:exec:{agent_id}:{target}:keep"),
                InlineKeyboardButton("🔄 Sync memories", callback_data=f"move:exec:{agent_id}:{target}:sync"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="move:cancel")],
        ])
        await update.callback_query.edit_message_text(
            f"<b>Move <code>{agent_id}</code> → {target}</b>\n\nChoose move mode:",
            parse_mode="HTML",
            reply_markup=markup,
        )

    async def _do_move(self, update, agent_id: str, target: str, instances: dict,
                       keep_source: bool = False, sync: bool = False, dry_run: bool = False):
        """Execute the actual migration."""
        import asyncio as _asyncio
        import subprocess as _subprocess

        # Called from callback query — update.message is None, use chat_id + _send_text
        chat_id = update.effective_chat.id

        await self._send_text(chat_id, f"⏳ Moving <code>{agent_id}</code> → <b>{target}</b>…", parse_mode="HTML")

        script = Path(__file__).parent.parent / "scripts" / "move_agent.py"
        if not script.exists():
            await self._send_text(chat_id, "Error: move_agent.py not found.")
            return

        cmd = [
            "python", str(script),
            agent_id, target,
            "--source-instance", "hashi2",
        ]
        if keep_source:
            cmd.append("--keep-source")
        if sync:
            cmd.append("--sync")
        if dry_run:
            cmd.append("--dry-run")

        try:
            result = await _asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _subprocess.run(cmd, capture_output=True, text=True,
                                        cwd=str(Path(__file__).parent.parent))
            )
            output = (result.stdout + result.stderr).strip()
            # Trim for Telegram
            if len(output) > 3000:
                output = output[:3000] + "\n…[truncated]"
            status = "✅" if result.returncode == 0 else "❌"
            await self._send_text(
                chat_id,
                f"{status} <b>Migration result:</b>\n<pre>{output}</pre>",
                parse_mode="HTML",
            )
        except Exception as e:
            await self._send_text(chat_id, f"Error running migration: {e}")

    async def callback_move(self, update: Update, context: Any):
        """Handle move: callback queries (multi-step picker)."""
        query = update.callback_query
        if not self._is_authorized_user(query.from_user.id):
            await query.answer()
            return
        await query.answer()

        data = query.data or ""
        parts = data.split(":", 3)

        if len(parts) < 2:
            return

        action = parts[1] if len(parts) > 1 else ""

        if action == "cancel":
            await query.edit_message_text("Move cancelled.")
            return

        if action == "agent" and len(parts) >= 3:
            agent_id = parts[2]
            instances = self._load_instances()
            rows = []
            for name, inst in instances.items():
                label = inst.get("display_name", name)
                rows.append([InlineKeyboardButton(f"📦 {label}", callback_data=f"move:target:{agent_id}:{name}")])
            rows.append([InlineKeyboardButton("❌ Cancel", callback_data="move:cancel")])
            markup = InlineKeyboardMarkup(rows)
            await query.edit_message_text(
                f"<b>Move <code>{agent_id}</code></b> — select target:",
                parse_mode="HTML",
                reply_markup=markup,
            )
            return

        if action == "target" and len(parts) >= 4:
            agent_id = parts[2]
            target = parts[3]
            markup = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📋 Move (plain)", callback_data=f"move:exec:{agent_id}:{target}:plain"),
                    InlineKeyboardButton("📂 Copy (keep source)", callback_data=f"move:exec:{agent_id}:{target}:keep"),
                ],
                [
                    InlineKeyboardButton("🔄 Sync memories back", callback_data=f"move:exec:{agent_id}:{target}:sync"),
                    InlineKeyboardButton("🔍 Dry run preview", callback_data=f"move:exec:{agent_id}:{target}:dry"),
                ],
                [InlineKeyboardButton("❌ Cancel", callback_data="move:cancel")],
            ])
            await query.edit_message_text(
                f"<b>Move <code>{agent_id}</code> → {target}</b>\n\nChoose mode:",
                parse_mode="HTML",
                reply_markup=markup,
            )
            return

        if action == "exec" and len(parts) >= 4:
            # parts: move:exec:<agent>:<target>:<mode>
            sub = parts[3].split(":", 1)
            agent_id = sub[0]
            rest = sub[1] if len(sub) > 1 else "plain"
            target_mode = rest.split(":", 1)
            target = target_mode[0]
            mode = target_mode[1] if len(target_mode) > 1 else "plain"

            keep = mode == "keep"
            sync = mode == "sync"
            dry = mode == "dry"
            instances = self._load_instances()
            await self._do_move(update, agent_id, target, instances,
                                keep_source=keep, sync=sync, dry_run=dry)

    def _resolve_bridge_handoff_endpoint(self, target_instance: str, mode: str) -> tuple[str, str]:
        action = "fork" if str(mode or "").strip().lower() == "fork" else "transfer"
        normalized_target = self._normalize_instance_name(target_instance)
        current_instance = self._normalize_instance_name(self._detect_instance_name())
        if normalized_target == current_instance:
            return current_instance, f"http://127.0.0.1:{self.global_config.workbench_port}/api/bridge/{action}"

        instances = self._load_instances()
        for name, inst in instances.items():
            if self._normalize_instance_name(name) != normalized_target:
                continue
            host = str(inst.get("api_host") or "127.0.0.1").strip() or "127.0.0.1"
            port = inst.get("workbench_port")
            if not port:
                raise ValueError(f"instance {normalized_target} has no workbench_port configured")
            return normalized_target, f"http://{host}:{int(port)}/api/bridge/{action}"
        raise ValueError(f"unknown instance: {target_instance}")

    def _build_handoff_payload(self, target_agent: str, target_instance: str, mode: str) -> dict[str, Any]:
        action = "fork" if str(mode or "").strip().lower() == "fork" else "transfer"
        transfer_id = f"{'frk' if action == 'fork' else 'trf'}-{uuid4().hex}"
        source_instance = self._normalize_instance_name(self._detect_instance_name())
        package = self.handoff_builder.build_transfer_package(
            transfer_id=transfer_id,
            source_agent=self.name,
            source_instance=source_instance,
            target_agent=target_agent,
            target_instance=target_instance,
            created_at=datetime.now().isoformat(),
            max_rounds=30,
            max_words=18000,
        )
        package["mode"] = action
        package["source_runtime"] = self.get_runtime_metadata()
        package["source_workspace_dir"] = str(self.workspace_dir)
        package["source_transcript_path"] = str(self.transcript_log_path)
        return package

    async def _cmd_bridge_handoff(self, update: Update, context: Any, *, mode: str) -> None:
        action = "fork" if str(mode or "").strip().lower() == "fork" else "transfer"
        label = "Fork" if action == "fork" else "Transfer"
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = [a.strip() for a in (context.args or []) if a.strip()]
        if len(args) not in {1, 2}:
            await self._reply_text(update, f"Usage: /{action} <target_agent> [HASHI instance]")
            return
        if action == "transfer" and self.has_active_transfer():
            await self._reply_text(update, f"Transfer already active.\n{self._transfer_redirect_text()}")
            return

        target_agent = args[0]
        target_instance = self._normalize_instance_name(args[1] if len(args) > 1 else self._detect_instance_name())
        current_instance = self._normalize_instance_name(self._detect_instance_name())
        if target_agent == self.name and target_instance == current_instance:
            await self._reply_text(update, f"Cannot {action} to the same agent on the same instance.")
            return

        package = self._build_handoff_payload(target_agent, target_instance, action)
        if int(package.get("exchange_count") or 0) <= 0:
            await self._reply_text(update, f"No recent bridge transcript was available to {action}.")
            return

        if action == "transfer":
            self._transfer_state = {
                "transfer_id": package["transfer_id"],
                "status": "pending",
                "source_agent": self.name,
                "source_instance": current_instance,
                "target_agent": target_agent,
                "target_instance": target_instance,
                "cutoff_seq": self.request_seq,
                "initiated_at": package["created_at"],
            }
            self._persist_transfer_state()
        await self._reply_text(
            update,
            f"Preparing {action} to {target_agent}@{target_instance}.\n{label} ID: {package['transfer_id']}",
        )

        try:
            _, endpoint = self._resolve_bridge_handoff_endpoint(target_instance, action)
            timeout = aiohttp.ClientTimeout(total=100)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(endpoint, json=package) as response:
                    body = await response.json()
                    if response.status >= 400 or not body.get("ok"):
                        raise RuntimeError(str(body.get("error") or f"HTTP {response.status}"))
        except Exception as e:
            self.logger.warning(f"{label} failed for {package['transfer_id']}: {e}")
            if action == "transfer":
                self._transfer_state["status"] = "failed"
                self._transfer_state["error"] = str(e)
                self._persist_transfer_state()
                await self._flush_suppressed_transfer_results()
                self._clear_transfer_state()
                await self._send_text(
                    update.effective_chat.id,
                    f"Transfer failed: {e}\nSession remains here.",
                )
                return
            await self._send_text(
                update.effective_chat.id,
                f"Fork failed: {e}",
            )
            return

        final_status = str(body.get("status") or "accepted")
        if action == "transfer":
            self._transfer_state["status"] = "accepted"
            self._transfer_state["target_status"] = final_status
            self._persist_transfer_state()
            self._suppressed_transfer_results.clear()
            if final_status == "accepted_but_chat_offline":
                target_status = body.get("target_chat_status") or "offline"
                message = (
                    f"Transfer accepted by {target_agent}@{target_instance}, but the target chat is {target_status}.\n"
                    f"Continue there once it reconnects. Transfer ID: {package['transfer_id']}"
                )
            else:
                message = (
                    f"Transfer accepted by {target_agent}@{target_instance}.\n"
                    f"Continue there. Transfer ID: {package['transfer_id']}"
                )
            await self._send_text(
                update.effective_chat.id,
                message,
            )
            return

        if final_status == "accepted_but_chat_offline":
            target_status = body.get("target_chat_status") or "offline"
            message = (
                f"Fork accepted by {target_agent}@{target_instance}, but the target chat is {target_status}.\n"
                f"Both sessions remain usable. Fork ID: {package['transfer_id']}"
            )
        else:
            message = (
                f"Fork accepted by {target_agent}@{target_instance}.\n"
                f"Both sessions can continue from the same context. Fork ID: {package['transfer_id']}"
            )
        await self._send_text(
            update.effective_chat.id,
            message,
        )

    async def cmd_transfer(self, update: Update, context: Any):
        await self._cmd_bridge_handoff(update, context, mode="transfer")

    async def cmd_fork(self, update: Update, context: Any):
        await self._cmd_bridge_handoff(update, context, mode="fork")

    async def cmd_cos(self, update: Update, context: Any):
        """Chief of Staff: route human-in-the-loop decisions to Lily for precedent-based answers."""
        if not self._is_authorized_user(update.effective_user.id):
            return
        if self.name == "lily":
            await self._reply_text(update, "Lily cannot use /cos — that would go in circles 🌸")
            return
        args = [a.strip().lower() for a in (context.args or []) if a.strip()]
        if not args:
            status = "ON ✅" if self._cos_enabled else "OFF"
            await self._reply_text(update, f"Chief of Staff routing: {status}\nUse /cos on or /cos off to toggle.")
            return
        if args[0] == "on":
            self._cos_enabled = True
            (self.workspace_dir / ".cos_on").touch()
            await self._reply_text(update, "Chief of Staff routing enabled ✅\nHuman-in-the-loop decisions will be routed to Lily first.")
        elif args[0] == "off":
            self._cos_enabled = False
            (self.workspace_dir / ".cos_on").unlink(missing_ok=True)
            await self._reply_text(update, "Chief of Staff routing disabled.\nDecisions will go directly to the user.")
        else:
            await self._reply_text(update, "Usage: /cos [on|off]")

    async def cos_query(self, question: str, *, timeout_s: float = 30.0) -> dict[str, Any]:
        """Send a decision query to Lily (Chief of Staff) via hchat and wait for response.

        Returns: {"answered": True/False, "response": str or None, "reason": str}
        """
        if not self._cos_enabled:
            return {"answered": False, "response": None, "reason": "cos_disabled"}
        if self.name == "lily":
            return {"answered": False, "response": None, "reason": "self_referential"}

        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            return {"answered": False, "response": None, "reason": "no_orchestrator"}

        lily_runtime = None
        for rt in getattr(orchestrator, "runtimes", []):
            if getattr(rt, "name", "") == "lily" and hasattr(rt, "enqueue_api_text"):
                lily_runtime = rt
                break
        if lily_runtime is None or not getattr(lily_runtime, "startup_success", False):
            return {"answered": False, "response": None, "reason": "lily_offline"}

        cos_id = f"cos-{uuid4().hex[:12]}"
        cos_prompt = (
            f"[cos query from {self.name}] (ID: {cos_id})\n"
            f"An agent needs a decision. Search your memory for precedent.\n"
            f"If you find clear precedent, reply with: COS_APPROVED: <your recommendation>\n"
            f"If no clear precedent exists, reply with: COS_DECLINED: <reason>\n\n"
            f"Question: {question}"
        )

        loop = asyncio.get_running_loop()
        response_future = loop.create_future()
        original_hchat_route = lily_runtime._hchat_route_reply

        async def _cos_intercept(item, response_text: str):
            if not response_future.done() and cos_id in getattr(item, "prompt", ""):
                response_future.set_result(response_text)
            await original_hchat_route(item, response_text)

        lily_runtime._hchat_route_reply = _cos_intercept
        try:
            request_id = await lily_runtime.enqueue_api_text(
                cos_prompt,
                source=f"cos-query:{self.name}",
                deliver_to_telegram=True,
            )
            if request_id is None:
                return {"answered": False, "response": None, "reason": "enqueue_failed"}

            try:
                response_text = await asyncio.wait_for(response_future, timeout=timeout_s)
            except asyncio.TimeoutError:
                return {"answered": False, "response": None, "reason": "timeout"}

            if response_text.strip().startswith("COS_DECLINED"):
                return {"answered": False, "response": response_text, "reason": "declined"}
            return {"answered": True, "response": response_text, "reason": "approved", "cos_id": cos_id}
        finally:
            lily_runtime._hchat_route_reply = original_hchat_route

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

    async def cmd_usecomputer(self, update, context):
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = [a.strip() for a in (context.args or []) if a.strip()]
        if not args:
            await self._reply_text(
                update,
                "Usage:\n"
                "/usecomputer on - enable managed GUI-aware mode\n"
                "/usecomputer off - disable it and clear the managed /sys slot\n"
                "/usecomputer status - show current state\n"
                "/usecomputer examples - show example prompts\n"
                "/usecomputer <task> - run a task with computer-use guidance loaded",
            )
            return

        sub = args[0].lower()
        if sub == "on":
            await self._reply_text(update, set_usecomputer_mode(self.sys_prompt_manager, True))
            return
        if sub == "off":
            await self._reply_text(update, set_usecomputer_mode(self.sys_prompt_manager, False))
            return
        if sub == "status":
            await self._reply_text(update, get_usecomputer_status(self.sys_prompt_manager))
            return
        if sub == "examples":
            await self._reply_text(update, get_usecomputer_examples_text())
            return

        task = " ".join(args).strip()
        set_usecomputer_mode(self.sys_prompt_manager, True)
        await self._reply_text(update, "Running in /usecomputer mode...")
        await self.enqueue_request(
            update.effective_chat.id,
            build_usecomputer_task_prompt(task),
            "usecomputer",
            "Computer-use task",
        )

    async def cmd_usercomputer(self, update, context):
        await self.cmd_usecomputer(update, context)

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

    # ── /safevoice command ─────────────────────────────────────────────────
    async def cmd_safevoice(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = [a.strip().lower() for a in (context.args or []) if a.strip()]
        if not args:
            status = "ON 🛡️" if self._safevoice_enabled else "OFF"
            await self._reply_text(update, f"Safe Voice: {status}\nUsage: /safevoice on | off")
            return
        if args[0] == "on":
            self._safevoice_enabled = True
            self._set_skill_state("safevoice", True)
            await self._reply_text(update, "🛡️ Safe Voice ON — voice messages will require confirmation before sending to agent.")
        elif args[0] == "off":
            self._safevoice_enabled = False
            self._set_skill_state("safevoice", False)
            self._pending_voice.clear()
            await self._reply_text(update, "Safe Voice OFF — voice messages go directly to agent.")
        else:
            await self._reply_text(update, "Usage: /safevoice on | off")

    async def callback_safevoice(self, update: Update, context: Any):
        query = update.callback_query
        if not self._is_authorized_user(query.from_user.id):
            return
        parts = (query.data or "").split(":", 2)
        action = parts[1] if len(parts) > 1 else ""
        chat_key = parts[2] if len(parts) > 2 else ""
        pending = self._pending_voice.pop(chat_key, None)
        if action == "yes" and pending:
            await query.edit_message_text(f"✅ Confirmed. Sending to agent:\n\n_{pending['transcript']}_", parse_mode="Markdown")
            await query.answer("Sending...")
            await self.enqueue_request(int(chat_key), pending["prompt"], "voice_transcript", pending["summary"])
        elif action == "no":
            await query.edit_message_text("❌ Voice message discarded.")
            await query.answer("Discarded")
        else:
            await query.edit_message_text("⏰ Voice confirmation expired.")
            await query.answer("Expired")

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

    async def cmd_say(self, update: Update, context: Any):
        """One-shot TTS: synthesize the last assistant message and send as voice."""
        if not self._is_authorized_user(update.effective_user.id):
            return
        text = self._load_last_text_from_transcript("assistant")
        if not text:
            await self._reply_text(update, "No recent message to read.")
            return
        chat_id = update.effective_chat.id
        request_id = f"say-{int(time.time())}"
        ok = await self._send_voice_reply(chat_id, text, request_id, force=True)
        if not ok:
            await self._reply_text(update, "Voice synthesis failed. Check /voice status for provider settings.")

    # ── /loop — recurring task management ──────────────────────────

    _LOOP_FREQ_PATTERNS: list[tuple] = []  # populated once at class level below

    async def cmd_loop(self, update: Update, context: Any):
        """Manage recurring loop tasks via skill injection.

        /loop <task description>     — create a new loop (agent comprehends & sets up)
        /loop list                   — list this agent's loops
        /loop stop [id]              — stop one or all loops
        """
        if not self._is_authorized_user(update.effective_user.id):
            return

        raw = (update.message.text or "").strip()
        parts = raw.split(None, 1)
        args_text = parts[1].strip() if len(parts) > 1 else ""

        if not args_text:
            await self._reply_text(
                update,
                "🔄 <b>Loop — Recurring Task Manager</b>\n\n"
                "<code>/loop &lt;task&gt;</code> — create a loop\n"
                "<code>/loop list</code> — list active loops\n"
                "<code>/loop stop [id]</code> — stop loop(s)",
                parse_mode="HTML",
            )
            return

        sub_lower = args_text.lower().strip()

        # --- /loop list ---
        if sub_lower == "list":
            if not self.skill_manager:
                await self._reply_text(update, "Skill manager not available.")
                return
            jobs = (
                [("heartbeat", j) for j in self.skill_manager.list_jobs("heartbeat", agent_name=self.name)] +
                [("cron", j) for j in self.skill_manager.list_jobs("cron", agent_name=self.name)]
            )
            loops = [(job_kind, j) for job_kind, j in jobs if j.get("loop_meta")]
            if not loops:
                await self._reply_text(update, "No active loops for this agent.")
                return
            lines = ["🔄 <b>Loops</b>\n"]
            for job_kind, j in loops:
                meta = j.get("loop_meta", {})
                status = "🟢 ON" if j.get("enabled") else "🔴 OFF"
                count = meta.get("count", 0)
                mx = meta.get("max", 100)
                reason = meta.get("stopped_reason", "")
                sched = (
                    f"every {j.get('interval_seconds')}s"
                    if job_kind == "heartbeat"
                    else j.get("schedule", "?")
                )
                summary = meta.get("task_summary", j.get("note", ""))[:60]
                lines.append(f"<code>{j['id']}</code> [{status}] [{job_kind}] {sched} ({count}/{mx})")
                if summary:
                    lines.append(f"  {summary}")
                if reason:
                    lines.append(f"  ⚠️ {reason}")
            await self._reply_text(update, "\n".join(lines), parse_mode="HTML")
            return

        # --- /loop stop [id] ---
        if sub_lower.startswith("stop"):
            stop_arg = sub_lower[4:].strip()
            if not self.skill_manager:
                await self._reply_text(update, "Skill manager not available.")
                return
            jobs = (
                [("heartbeat", j) for j in self.skill_manager.list_jobs("heartbeat", agent_name=self.name)] +
                [("cron", j) for j in self.skill_manager.list_jobs("cron", agent_name=self.name)]
            )
            loops = [(job_kind, j) for job_kind, j in jobs if j.get("loop_meta") and j.get("enabled")]
            if not loops:
                await self._reply_text(update, "No active loops to stop.")
                return
            stopped = []
            for job_kind, j in loops:
                if not stop_arg or stop_arg in j["id"]:
                    self.skill_manager.set_job_enabled(job_kind, j["id"], enabled=False)
                    stopped.append(j["id"])
            if stopped:
                await self._reply_text(update, f"⏹ Stopped: {', '.join(stopped)}")
            else:
                await self._reply_text(update, f"No loop matching '{stop_arg}' found.")
            return

        # --- /loop <task> — skill injection: let agent comprehend and set up ---
        tasks_path = str(self.skill_manager.tasks_path) if self.skill_manager else "tasks.json"
        loop_skill_prompt = (
            "--- SKILL CONTEXT [loop] ---\n"
            "The user wants to create a recurring loop task. Your job is to UNDERSTAND their request "
            "and set up the correct recurring job in tasks.json.\n\n"
            "## What you must figure out from the user's message:\n"
            "1. **WHAT** to do each iteration (the task)\n"
            "2. **HOW OFTEN** (the interval — e.g., every 10 min, every 30 min, hourly)\n"
            "3. **WHEN TO STOP** (the completion condition — e.g., after N times, when all items done, etc.)\n\n"
            "## Job type rule: read this carefully\n"
            "- Use a `heartbeat` for interval-based loops: every N minutes, every N hours, repeated polling, recurring progress checks, retries, watchdogs.\n"
            "- Use a `cron` only for fixed wall-clock times: e.g. every day at 08:00, every Monday at 09:30.\n"
            "- Do NOT use cron expressions like `*/15 * * * *` for loop-style interval jobs.\n"
            "- If the user's request sounds like 'check every 15 min' or 'run every hour until done', this needs `heartbeat`.\n\n"
            "## How to create the recurring job:\n"
            f"1. Read `{tasks_path}` to see the current `heartbeats` and `crons` arrays\n"
            f"2. Generate a unique ID: `{self.name}-loop-<6char_hash>`\n"
            "3. Choose the correct structure:\n"
            "For interval-based loops, append a heartbeat entry like this:\n"
            "```json\n"
            "{\n"
            f'  "id": "{self.name}-loop-XXXXXX",\n'
            f'  "agent": "{self.name}",\n'
            '  "enabled": true,\n'
            '  "interval_seconds": 600,\n'
            '  "action": "enqueue_prompt",\n'
            '  "prompt": "<clear instructions for each iteration — include the task, progress tracking method, and stop condition>",\n'
            f'  "note": "Loop: <brief summary>",\n'
            '  "loop_meta": {\n'
            '    "max": 100,\n'
            '    "count": 0,\n'
            f'    "created": "<current ISO datetime>",\n'
            '    "task_summary": "<user request summary>"\n'
            '  }\n'
            "}\n"
            "```\n"
            "For fixed-time schedules, append a cron entry like this:\n"
            "```json\n"
            "{\n"
            f'  "id": "{self.name}-loop-XXXXXX",\n'
            f'  "agent": "{self.name}",\n'
            '  "enabled": true,\n'
            '  "schedule": "<cron expression>",\n'
            '  "action": "enqueue_prompt",\n'
            '  "prompt": "<clear instructions for each iteration — include the task, progress tracking method, and stop condition>",\n'
            f'  "note": "Loop: <brief summary>",\n'
            '  "loop_meta": {\n'
            '    "max": 100,\n'
            '    "count": 0,\n'
            f'    "created": "<current ISO datetime>",\n'
            '    "task_summary": "<user request summary>"\n'
            '  }\n'
            "}\n"
            "```\n"
            f"4. Save `{tasks_path}`\n\n"
            "## Heartbeat interval examples:\n"
            "- Every 5 min: `interval_seconds = 300`\n"
            "- Every 10 min: `interval_seconds = 600`\n"
            "- Every 15 min: `interval_seconds = 900`\n"
            "- Every hour: `interval_seconds = 3600`\n\n"
            "## Cron examples for fixed clock times only:\n"
            "- Daily at midnight: `0 0 * * *`\n"
            "- Daily at 08:30: `30 8 * * *`\n"
            "- Every Monday at 09:00: `0 9 * * 1`\n\n"
            "## The prompt you write into the job entry must tell the future iteration:\n"
            "- What to do\n"
            "- How to track progress (use workspace files if needed)\n"
            f'- When done: read `{tasks_path}`, find the job by ID in the correct array, set `"enabled": false`, save\n'
            "- If unrecoverable error: disable the same job and report\n\n"
            "## Safety net:\n"
            "- `loop_meta.max` is a hard cap (default 100). The scheduler auto-disables when count exceeds max.\n"
            "- The agent should still stop EARLIER when the task is semantically complete.\n\n"
            "## IMPORTANT:\n"
            "- Do NOT ask the user for clarification. Infer reasonable defaults from their message.\n"
            "- If interval is unclear, default to 10 minutes.\n"
            "- For interval loops, this means a heartbeat unless the user explicitly asks for a fixed wall-clock time.\n"
            "- After creating the job, confirm to the user: the job ID, whether it is a heartbeat or cron, its schedule/interval, and what each iteration will do.\n\n"
            "--- USER REQUEST ---\n"
            f"{args_text}"
        )

        # Inject as a regular prompt for the agent to process
        await self.enqueue_request(
            chat_id=update.effective_chat.id,
            prompt=loop_skill_prompt,
            source="loop_skill",
            summary="Loop setup",
        )
        await self._reply_text(
            update,
            "🔄 收到！正在理解任务并设置循环…",
        )

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
        raw_args = list(context.args or [])
        args = [a.strip().lower() for a in raw_args if a.strip()]
        if args and args[0] in {"on", "off"}:
            enabled = args[0] == "on"
            if self.skill_manager:
                _, msg = self.skill_manager.set_toggle_state(self.workspace_dir, "debug", enabled=enabled)
                state_str = "ON 🔴" if enabled else "OFF"
                await self._reply_text(update, f"🐛 Debug mode: {state_str}\n{msg}")
            else:
                await self._reply_text(update, "Skill manager not available.")
            return
        if not self.skill_manager:
            await self._reply_text(update, "Skill system is not configured.")
            return
        skill = self.skill_manager.get_skill("debug")
        if skill is None:
            await self._reply_text(update, "Unknown skill: debug")
            return
        prompt_text = " ".join(raw_args).strip()
        if not prompt_text:
            await self._reply_text(update, "Usage: /debug <prompt> or /debug on|off")
            return
        prompt = self.skill_manager.build_prompt_for_skill(skill, prompt_text)
        await self._reply_text(update, f"Running skill {skill.id}...")
        await self.enqueue_request(
            update.effective_chat.id,
            prompt,
            f"skill:{skill.id}",
            f"Skill {skill.id}",
        )

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
        if skill.id == "habits" and not rest:
            text, markup = self._build_habit_browser_view()
            await self._reply_text(update, text, parse_mode="HTML", reply_markup=markup)
            return
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
            _, message = await self.skill_manager.run_action_skill(
                skill,
                self.workspace_dir,
                args=rest,
                extra_env={
                    "BRIDGE_ACTIVE_BACKEND": self.config.active_backend,
                    "BRIDGE_ACTIVE_MODEL": self.get_current_model(),
                },
            )
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
        if data.startswith("skill:habits:"):
            parts = data.split(":", 5)
            action = parts[2] if len(parts) > 2 else "list"
            if action == "list":
                offset = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
                text, markup = self._build_habit_browser_view(offset=offset)
                await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
                await query.answer()
                return
            if action == "view":
                habit_id = parts[3] if len(parts) > 3 else ""
                offset = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
                text, markup = self._build_habit_browser_view(offset=offset, selected_habit_id=habit_id)
                await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
                await query.answer()
                return
            if action == "set":
                habit_id = parts[3] if len(parts) > 3 else ""
                target = parts[4] if len(parts) > 4 else ""
                offset = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0
                ok, message = self._set_local_habit_status(habit_id, target)
                text, markup = self._build_habit_browser_view(
                    offset=offset,
                    selected_habit_id=habit_id if ok else None,
                    notice=message,
                )
                await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
                await query.answer(message, show_alert=not ok)
                return
            if action == "queue":
                await query.edit_message_text(self._build_habit_governance_view(), parse_mode="HTML")
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
            if action == "transfer":
                # Show agent selector (same instance + remote instances)
                markup = self._build_job_transfer_keyboard(kind, task_id)
                job = self.skill_manager.get_job(kind, task_id)
                job_label = (job.get("note") or task_id) if job else task_id
                await query.edit_message_text(
                    f"📤 <b>Transfer job</b>\n<code>{job_label[:60]}</code>\n\nSelect target agent:",
                    parse_mode="HTML",
                    reply_markup=markup,
                )
                await query.answer()
                return
            if action == "xfer_to":
                # Same-instance transfer: value = target_agent_name
                target_agent = value
                job = self.skill_manager.get_job(kind, task_id)
                if not job:
                    await query.answer("Job not found", show_alert=True)
                    return
                ok, message, _ = self.skill_manager.transfer_job(kind, task_id, target_agent)
                await query.answer(message, show_alert=not ok)
                if ok:
                    await query.edit_message_text(
                        f"✅ Job transferred to <b>{target_agent}</b> (disabled — review before enabling).",
                        parse_mode="HTML",
                    )
                return
            if action == "xfer_remote":
                # Cross-instance transfer: value = "{target_agent}:{instance_id}"
                parts = value.split(":", 1)
                if len(parts) != 2:
                    await query.answer("Invalid target", show_alert=True)
                    return
                target_agent, instance_id = parts
                job = self.skill_manager.get_job(kind, task_id)
                if not job:
                    await query.answer("Job not found", show_alert=True)
                    return
                await query.answer("Sending to remote instance…")
                ok, msg = await self._transfer_job_remote(kind, job, target_agent, instance_id)
                if ok:
                    # Disable original
                    self.skill_manager.set_job_enabled(kind, task_id, enabled=False)
                    await query.edit_message_text(
                        f"✅ Job transferred to <b>{target_agent}@{instance_id}</b> (original disabled).",
                        parse_mode="HTML",
                    )
                else:
                    await query.edit_message_text(f"❌ Transfer failed: {msg}")
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
                elif skill.id == "habits":
                    text, markup = self._build_habit_browser_view()
                    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
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
                ok, message = await self.skill_manager.run_action_skill(
                    skill,
                    self.workspace_dir,
                    extra_env={
                        "BRIDGE_ACTIVE_BACKEND": self.config.active_backend,
                        "BRIDGE_ACTIVE_MODEL": self.get_current_model(),
                    },
                )
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

    def _build_job_transfer_keyboard(self, kind: str, task_id: str):
        """Build inline keyboard for job transfer: same-instance agents + remote instances."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        buttons = []

        # Same-instance active agents (excluding self)
        orchestrator = getattr(self, "orchestrator", None)
        local_agents = []
        if orchestrator:
            for rt in getattr(orchestrator, "runtimes", []):
                name = getattr(rt, "name", "")
                if name and name != self.name:
                    local_agents.append(name)

        if local_agents:
            buttons.append([InlineKeyboardButton("── This instance ──", callback_data="noop")])
            row = []
            for agent in sorted(local_agents):
                row.append(InlineKeyboardButton(agent, callback_data=f"skilljob:{kind}:xfer_to:{task_id}:{agent}"))
                if len(row) == 3:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)

        # Remote instances from instances.json
        try:
            import json as _j
            from pathlib import Path as _P
            instances_path = self.global_config.project_root / "instances.json"
            if instances_path.exists():
                data = _j.loads(instances_path.read_text(encoding="utf-8"))
                local_id = self.global_config.project_root.name.upper()  # rough heuristic
                for inst_id, inst_info in data.get("instances", {}).items():
                    if not inst_info.get("active", False):
                        continue
                    # Skip self-instance
                    display = inst_info.get("display_name", inst_id)
                    platform = inst_info.get("platform", "")
                    if platform == "portable":
                        continue
                    # Load agents from remote agents.json
                    if platform == "windows":
                        wsl_root = inst_info.get("wsl_root")
                        agents_path = _P(wsl_root) / "agents.json" if wsl_root else None
                    else:
                        root = inst_info.get("root")
                        agents_path = _P(root) / "agents.json" if root else None

                    if not agents_path or not agents_path.exists():
                        continue
                    try:
                        adata = _j.loads(agents_path.read_text(encoding="utf-8-sig"))
                        remote_agents = [a["name"] for a in adata.get("agents", []) if a.get("is_active", True)]
                    except Exception:
                        continue

                    if not remote_agents:
                        continue

                    buttons.append([InlineKeyboardButton(f"── {display} ──", callback_data="noop")])
                    row = []
                    for agent in sorted(remote_agents):
                        cb = f"skilljob:{kind}:xfer_remote:{task_id}:{agent}:{inst_id}"
                        row.append(InlineKeyboardButton(f"{agent}", callback_data=cb))
                        if len(row) == 3:
                            buttons.append(row)
                            row = []
                    if row:
                        buttons.append(row)
        except Exception:
            pass

        buttons.append([InlineKeyboardButton("✖ Cancel", callback_data="noop")])
        return InlineKeyboardMarkup(buttons)

    async def _transfer_job_remote(self, kind: str, job: dict, target_agent: str,
                                   instance_id: str) -> tuple[bool, str]:
        """POST job to remote instance /api/jobs/import via Workbench API."""
        import json as _j
        from urllib import request as _req
        from urllib.error import URLError
        from pathlib import Path as _P

        try:
            instances_path = self.global_config.project_root / "instances.json"
            data = _j.loads(instances_path.read_text(encoding="utf-8"))
            inst = data.get("instances", {}).get(instance_id, {})
        except Exception as e:
            return False, f"Could not read instances.json: {e}"

        host = inst.get("lan_ip") or inst.get("api_host", "127.0.0.1")
        wb_port = inst.get("workbench_port")
        if not wb_port:
            return False, f"No workbench_port for {instance_id}"

        import copy
        from uuid import uuid4
        new_job = copy.deepcopy(job)
        new_job["agent"] = target_agent
        new_job["enabled"] = False
        new_job["id"] = f"{target_agent}-{uuid4().hex[:8]}"
        new_job["note"] = (job.get("note") or job["id"]) + f" [transferred from {self.name}@{self.global_config.project_root.name}]"

        payload = _j.dumps({
            "kind": kind,
            "job": new_job,
            "from_instance": str(self.global_config.project_root.name),
            "from_agent": self.name,
        }).encode("utf-8")

        url = f"http://{host}:{wb_port}/api/jobs/import"
        rq = _req.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with _req.urlopen(rq, timeout=10) as resp:
                result = _j.loads(resp.read().decode("utf-8"))
                if result.get("ok"):
                    # hchat notification to target agent
                    try:
                        from tools.hchat_send import send_hchat
                        send_hchat(
                            target_agent, self.name,
                            f"You have a new job transferred from {self.name}: [{new_job['id']}] {new_job.get('note', '')} — review with /jobs and enable when ready.",
                            target_instance=instance_id,
                        )
                    except Exception:
                        pass
                    return True, result.get("message", "ok")
                return False, result.get("message", "remote error")
        except URLError as e:
            return False, f"Connection failed ({host}:{wb_port}): {e}"

    async def _run_job_now(self, job: dict[str, Any]) -> tuple[bool, str]:
        action = job.get("action", "enqueue_prompt")
        if action == "export_transcript":
            await self.send_long_message(
                chat_id=self._primary_chat_id(),
                text="Transcript export is only implemented for fixed agents.",
                request_id=f"job-{job.get('id')}",
                purpose="skill-job-run",
            )
            return False, "Transcript export is only implemented for fixed agents."
        if action.startswith("skill:"):
            return await self.invoke_scheduler_skill(
                skill_id=action.split(":", 1)[1],
                args=job.get("args", "") or job.get("prompt", ""),
                task_id=job.get("id", "manual"),
            )
        prompt = job.get("prompt", "")
        if not prompt.strip():
            await self.send_long_message(
                chat_id=self._primary_chat_id(),
                text=f"Job {job.get('id')} has no prompt.",
                request_id=f"job-{job.get('id')}",
                purpose="skill-job-run",
            )
            return False, f"Job {job.get('id')} has no prompt."
        summary_prefix = "Heartbeat Task" if "interval_seconds" in job else "Cron Task"
        await self.enqueue_request(
            chat_id=self._primary_chat_id(),
            prompt=prompt,
            source="scheduler",
            summary=f"{summary_prefix} [{job.get('id')}]",
        )
        return True, f"Queued {summary_prefix.lower()} [{job.get('id')}]"

    async def _handle_job_command(self, update: Update, kind: str, args: list[str]):
        if not self._is_authorized_user(update.effective_user.id):
            return
        if not self.skill_manager:
            await self._reply_text(update, "No task scheduler configured.")
            return
        if not args or args[0].strip().lower() in {"list", "show"}:
            from orchestrator.agent_runtime import _build_jobs_with_buttons
            text, markup = _build_jobs_with_buttons(self.name, self.skill_manager, filter_agent=self.name)
            await self._reply_text(update, text, parse_mode="HTML", reply_markup=markup)
            return
        if args[0].strip().lower() != "run" or len(args) < 2:
            await self._reply_text(update, f"Usage: /{kind} [list] | /{kind} run <job_id>")
            return
        task_id = args[1].strip()
        job = self.skill_manager.get_job(kind, task_id)
        if not job or job.get("agent") != self.name:
            await self._reply_text(update, f"{kind} job not found for this agent: {task_id}")
            return
        await self._reply_text(update, f"Running {kind} job now: {task_id}")
        await self._run_job_now(job)

    async def cmd_cron(self, update: Update, context: Any):
        await self._handle_job_command(update, "cron", list(context.args or []))

    async def cmd_heartbeat(self, update: Update, context: Any):
        await self._handle_job_command(update, "heartbeat", list(context.args or []))

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
        _verbose_file = self.workspace_dir / ".verbose_off"
        if self._verbose:
            _verbose_file.unlink(missing_ok=True)
        else:
            _verbose_file.touch()
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
        _think_file = self.workspace_dir / ".think_off"
        if self._think:
            _think_file.unlink(missing_ok=True)
        else:
            _think_file.touch()
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
        arg = (context.args[0].strip().lower() if context.args else "")
        if arg == "all":
            filter_agent = None
        elif arg:
            filter_agent = arg
        else:
            filter_agent = self.name
        text, markup = _build_jobs_with_buttons(self.name, self.skill_manager, filter_agent=filter_agent)
        await self._reply_text(update, text, parse_mode="HTML", reply_markup=markup)

    async def cmd_timeout(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = [a.strip() for a in (context.args or []) if a.strip()]
        backend = getattr(self, "backend", None) or (
            self.backend_manager.current_backend if hasattr(self, "backend_manager") else None
        )
        extra = {}
        if backend and hasattr(backend, "config") and backend.config.extra:
            extra = backend.config.extra

        # Defaults (in seconds) from the backend class
        default_idle = getattr(type(backend), "DEFAULT_IDLE_TIMEOUT_SEC", 300) if backend else 300
        default_hard = getattr(type(backend), "DEFAULT_HARD_TIMEOUT_SEC", 1800) if backend else 1800

        if not args:
            # Show current values
            idle_s = extra.get("idle_timeout_sec") or extra.get("process_timeout") or default_idle
            hard_s = extra.get("hard_timeout_sec") or default_hard
            idle_min = int(idle_s) // 60
            hard_min = int(hard_s) // 60
            def_idle_min = default_idle // 60
            def_hard_min = default_hard // 60
            text = (
                f"<b>⏱ Timeout — {self.name}</b>\n\n"
                f"  Idle:  <b>{idle_min} min</b>  (default: {def_idle_min} min)\n"
                f"  Hard:  <b>{hard_min} min</b>  (default: {def_hard_min} min)\n\n"
                f"Usage:\n"
                f"  <code>/timeout 30</code>        — set idle to 30 min\n"
                f"  <code>/timeout 30 120</code>    — idle=30 min, hard=120 min\n"
                f"  <code>/timeout reset</code>     — restore defaults"
            )
            await self._reply_text(update, text, parse_mode="HTML")
            return

        if args[0].lower() == "reset":
            if backend and hasattr(backend, "config") and backend.config.extra:
                backend.config.extra.pop("idle_timeout_sec", None)
                backend.config.extra.pop("hard_timeout_sec", None)
                backend.config.extra.pop("process_timeout", None)
            def_idle_min = default_idle // 60
            def_hard_min = default_hard // 60
            await self._reply_text(
                update,
                f"⏱ Timeout reset to defaults: idle={def_idle_min} min, hard={def_hard_min} min",
            )
            return

        try:
            idle_min = int(args[0])
            if idle_min <= 0:
                raise ValueError
        except ValueError:
            await self._reply_text(update, "Usage: /timeout [minutes] [hard_minutes] | reset")
            return

        hard_min = None
        if len(args) >= 2:
            try:
                hard_min = int(args[1])
                if hard_min <= 0:
                    raise ValueError
            except ValueError:
                await self._reply_text(update, "Usage: /timeout [minutes] [hard_minutes] | reset")
                return

        if backend and hasattr(backend, "config"):
            if backend.config.extra is None:
                backend.config.extra = {}
            backend.config.extra["idle_timeout_sec"] = idle_min * 60
            backend.config.extra.pop("process_timeout", None)  # avoid legacy conflict
            if hard_min is not None:
                backend.config.extra["hard_timeout_sec"] = hard_min * 60

        hard_str = f", hard={hard_min} min" if hard_min is not None else ""
        await self._reply_text(update, f"⏱ Timeout updated: idle={idle_min} min{hard_str}")

    async def cmd_hchat(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = [a.strip() for a in (context.args or []) if a.strip()]
        if len(args) < 2:
            await self._reply_text(
                update,
                "<b>💬 Hchat — Ask this agent to compose &amp; send a message to another agent</b>\n\n"
                "Usage: <code>/hchat &lt;agent&gt; &lt;intent&gt;</code> — local instance only\n"
                "       <code>/hchat &lt;agent&gt;@&lt;INSTANCE&gt; &lt;intent&gt;</code> — cross-instance via HASHI1 exchange\n"
                "       <code>/hchat all &lt;intent&gt;</code> — broadcast to all local active agents (excludes temp)\n"
                "       <code>/hchat @&lt;group&gt; &lt;intent&gt;</code> — broadcast to a local group (use /group to manage)\n\n"
                "Example: <code>/hchat lily give her an update on what we've been doing</code>\n"
                "Example: <code>/hchat rika@HASHI2 ask her for the latest test result</code>\n"
                "Example: <code>/hchat hashiko@MSI tell her the route is fixed</code>\n"
                "Example: <code>/hchat arale 告诉她新功能已完成</code>\n"
                "Example: <code>/hchat all 告诉大家新功能上线了</code>\n"
                "Example: <code>/hchat @staff 告诉核心团队系统已重启</code>\n\n"
                "<i>Note: no @ means local only. Cross-instance targets must be written as agent@INSTANCE.</i>",
                parse_mode="HTML",
            )
            return
        target_name = args[0].lower()
        intent = " ".join(args[1:])

        # Resolve "all" or "@group_name" to a list of agent names
        broadcast_targets: list[str] | None = None
        broadcast_label: str = ""

        if target_name == "all":
            import json as _json
            try:
                _cfg = _json.loads(self.global_config.config_path.read_text(encoding="utf-8-sig"))
                broadcast_targets = [
                    a["name"] for a in _cfg.get("agents", [])
                    if a.get("is_active", True)
                    and a["name"].lower() != "temp"
                    and a["name"].lower() != self.name.lower()
                ]
            except Exception:
                broadcast_targets = []
            broadcast_label = "ALL active agents"

        elif target_name.startswith("@"):
            group_name = target_name[1:]
            directory = getattr(self, "agent_directory", None) or getattr(getattr(self, "orchestrator", None), "agent_directory", None)
            if directory is None:
                await self._reply_text(update, "❌ Agent directory unavailable for group resolution.")
                return
            if not directory.group_exists(group_name):
                await self._reply_text(update, f"❌ Group '{group_name}' not found. Use /group to list groups.")
                return
            broadcast_targets = directory.resolve_group(group_name, exclude_self=self.name)
            broadcast_label = f"group @{group_name}"

        if broadcast_targets is not None:
            if not broadcast_targets:
                await self._reply_text(update, f"❌ No agents found in {broadcast_label}.")
                return
            agent_list = ", ".join(broadcast_targets)
            send_cmds = "\n".join(
                f'   {sys.executable} {Path(__file__).resolve().parent.parent / "tools" / "hchat_send.py"} --to {a} --from {self.name} --text "<your composed message>"'
                for a in broadcast_targets
            )
            self_prompt = (
                f"[HCHAT BROADCAST] The user wants you to send a Hchat message to {broadcast_label}.\n\n"
                f"Target agents: {agent_list}\n"
                f"EXCLUDED: temp (always excluded from broadcasts), {self.name} (yourself)\n\n"
                f"Intent: {intent}\n\n"
                f"Instructions:\n"
                f"1. Think about what from our current conversation context is relevant to this intent.\n"
                f"2. Compose a complete, meaningful message FROM you ({self.name}). "
                f"Write it as yourself — the same message goes to all agents. Be concise.\n"
                f"3. Send the message to EACH agent by running these bash commands:\n"
                f"{send_cmds}\n"
                f"4. Report back to the user: what you sent, to whom, and how many succeeded.\n\n"
                f"Do NOT relay the user's words literally. Compose the message yourself.\n\n"
                f"IMPORTANT: When you later receive messages starting with '[hchat reply from ...]', "
                f"just report the reply content to the user. Do NOT send another hchat message back."
            )
            await self._reply_text(update, f"📢 Broadcasting Hchat to <b>{len(broadcast_targets)}</b> agents ({broadcast_label})...", parse_mode="HTML")
        else:
            # Single agent target
            self_prompt = (
                f"[HCHAT TASK] The user wants you to send a Hchat message to agent \"{target_name}\".\n\n"
                f"Intent: {intent}\n\n"
                f"Instructions:\n"
                f"1. Think about what from our current conversation context is relevant to this intent.\n"
                f"2. Compose a complete, meaningful message FROM you ({self.name}) TO {target_name}. "
                f"Write it as yourself — introduce yourself if appropriate, include relevant context, be concise.\n"
                f"3. Send the message by running this bash command:\n"
                f"   {sys.executable} {Path(__file__).resolve().parent.parent / 'tools' / 'hchat_send.py'} --to {target_name} --from {self.name} --text \"<your composed message>\"\n"
                f"4. Report back to the user: what you sent and a brief summary of why.\n\n"
                f"Do NOT relay the user's words literally. Compose the message yourself.\n\n"
                f"IMPORTANT: When you later receive a message starting with '[hchat reply from ...]', "
                f"just report the reply content to the user. Do NOT send another hchat message back — "
                f"the conversation ends there."
            )
            await self._reply_text(update, f"💬 Composing Hchat message to <b>{target_name}</b>...", parse_mode="HTML")

        await self.enqueue_api_text(
            self_prompt,
            source="bridge:hchat",
            deliver_to_telegram=True,
        )

    # ── /group ────────────────────────────────────────────────────────────────

    def _group_detail_view(self, directory, group_name: str) -> tuple[str, "InlineKeyboardMarkup"]:
        """Build the group detail message + inline keyboard."""
        groups = directory.list_groups()
        grp = groups.get(group_name, {})
        desc = grp.get("description", "")
        members = grp.get("members", [])
        is_dynamic = members == "@active"

        if is_dynamic:
            resolved = directory.resolve_group(group_name)
            member_display = "🔄 <i>Dynamic — all active agents</i>\n  " + ", ".join(resolved) if resolved else "🔄 <i>Dynamic — (none running)</i>"
        else:
            if members:
                rows_meta = []
                for m in members:
                    row = directory.get_agent_row(m)
                    emoji = row.get("emoji", "🤖") if row else "🤖"
                    display = row.get("display_name", m) if row else m
                    rows_meta.append(f"{emoji} {display}")
                member_display = "  " + "  ·  ".join(rows_meta)
            else:
                member_display = "  <i>(empty)</i>"

        text = (
            f"<b>📦 Group: {group_name}</b>\n"
            f"{desc}\n\n"
            f"Members ({len(directory.resolve_group(group_name))}):\n"
            f"{member_display}\n"
        )

        if is_dynamic:
            rows = [[InlineKeyboardButton("✕ Close", callback_data=f"group:back")]]
        else:
            rows = [
                [
                    InlineKeyboardButton("＋ Add", callback_data=f"group:add:{group_name}"),
                    InlineKeyboardButton("－ Remove", callback_data=f"group:remove:{group_name}"),
                    InlineKeyboardButton("✏️ Rename", callback_data=f"group:rename:{group_name}"),
                ],
                [
                    InlineKeyboardButton("🗑 Delete", callback_data=f"group:delete:{group_name}"),
                    InlineKeyboardButton("« Back", callback_data="group:back"),
                ],
                [
                    InlineKeyboardButton("▶ Start All", callback_data=f"group:start:{group_name}"),
                    InlineKeyboardButton("⏹ Stop All", callback_data=f"group:stop:{group_name}"),
                    InlineKeyboardButton("🔄 Reboot All", callback_data=f"group:reboot:{group_name}"),
                ],
                [InlineKeyboardButton("💬 Broadcast", callback_data=f"group:broadcast:{group_name}")],
            ]
        return text, InlineKeyboardMarkup(rows)

    def _group_list_view(self, directory) -> tuple[str, "InlineKeyboardMarkup"]:
        """Build the group overview message + inline keyboard."""
        groups = directory.list_groups()
        if groups:
            lines = ["<b>📦 Agent Groups</b>\n"]
            for name, grp in groups.items():
                members = grp.get("members", [])
                is_dynamic = members == "@active"
                count = len(directory.resolve_group(name)) if is_dynamic else len(members)
                desc = grp.get("description", "")
                tag = " 🔄" if is_dynamic else ""
                lines.append(f"• <b>{name}</b>{tag}  ({count} agents) — {desc}")
        else:
            lines = ["<b>📦 Agent Groups</b>\n", "<i>No groups defined yet.</i>"]

        rows = [[InlineKeyboardButton(f"📦 {name}", callback_data=f"group:view:{name}")] for name in groups]
        rows.append([InlineKeyboardButton("＋ New Group", callback_data="group:new")])
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    async def cmd_group(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        directory = getattr(self, "agent_directory", None)
        if directory is None:
            await self._reply_text(update, "❌ Agent directory unavailable.")
            return

        args = [a.strip() for a in (context.args or []) if a.strip()]

        # /group new <name>
        if args and args[0].lower() == "new":
            if len(args) < 2:
                await self._reply_text(update, "Usage: <code>/group new &lt;name&gt;</code>", parse_mode="HTML")
                return
            name = args[1].lower()
            desc = " ".join(args[2:]) if len(args) > 2 else ""
            ok, msg = directory.create_group(name, desc)
            if ok:
                text, markup = self._group_detail_view(directory, name)
                await self._reply_text(update, f"✅ {msg}\n\n" + text, parse_mode="HTML", reply_markup=markup)
            else:
                await self._reply_text(update, f"❌ {msg}")
            return

        # /group del <name>
        if args and args[0].lower() == "del":
            if len(args) < 2:
                await self._reply_text(update, "Usage: <code>/group del &lt;name&gt;</code>", parse_mode="HTML")
                return
            name = args[1].lower()
            rows = [[
                InlineKeyboardButton("✅ Confirm Delete", callback_data=f"group:delete_confirm:{name}"),
                InlineKeyboardButton("✕ Cancel", callback_data="group:back"),
            ]]
            await self._reply_text(
                update,
                f"⚠️ Delete group <b>{name}</b>?\nThis will NOT affect the agents themselves.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return

        # /group <name>  — view detail
        if args:
            name = args[0].lower()
            if not directory.group_exists(name):
                await self._reply_text(update, f"❌ Group '{name}' not found.")
                return
            text, markup = self._group_detail_view(directory, name)
            await self._reply_text(update, text, parse_mode="HTML", reply_markup=markup)
            return

        # /group  — overview
        text, markup = self._group_list_view(directory)
        await self._reply_text(update, text, parse_mode="HTML", reply_markup=markup)

    async def callback_group(self, update: Update, context: Any):
        query = update.callback_query
        if not self._is_authorized_user(query.from_user.id):
            return
        directory = getattr(self, "agent_directory", None)
        if directory is None:
            await query.answer("Agent directory unavailable", show_alert=True)
            return

        data = query.data or ""
        # data format: group:<action>[:<group_name>[:<agent_name>]]
        parts = data.split(":", 3)
        action = parts[1] if len(parts) > 1 else ""
        group_name = parts[2] if len(parts) > 2 else ""
        extra = parts[3] if len(parts) > 3 else ""

        await query.answer()

        # ── back → group list ──
        if action == "back":
            text, markup = self._group_list_view(directory)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            return

        # ── view group detail ──
        if action == "view":
            text, markup = self._group_detail_view(directory, group_name)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
            return

        # ── new group (prompt) ──
        if action == "new":
            await query.edit_message_text(
                "To create a new group, send:\n<code>/group new &lt;name&gt; [description]</code>",
                parse_mode="HTML",
            )
            return

        # ── delete (confirm prompt) ──
        if action == "delete":
            rows = [[
                InlineKeyboardButton("✅ Confirm Delete", callback_data=f"group:delete_confirm:{group_name}"),
                InlineKeyboardButton("✕ Cancel", callback_data=f"group:view:{group_name}"),
            ]]
            await query.edit_message_text(
                f"⚠️ Delete group <b>{group_name}</b>?\nThis will NOT affect the agents themselves.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return

        # ── delete confirmed ──
        if action == "delete_confirm":
            ok, msg = directory.delete_group(group_name)
            if ok:
                text, markup = self._group_list_view(directory)
                await query.edit_message_text(f"🗑 {msg}\n\n" + text, parse_mode="HTML", reply_markup=markup)
            else:
                await query.edit_message_text(f"❌ {msg}")
            return

        # ── add members ──
        if action == "add":
            groups = directory.list_groups()
            current = groups.get(group_name, {}).get("members", [])
            all_agents = list(directory._agent_rows.keys())
            available = [n for n in all_agents if n not in current]
            if not available:
                await query.edit_message_text(f"All active agents are already in <b>{group_name}</b>.", parse_mode="HTML")
                return
            rows = []
            for agent in available:
                row = directory.get_agent_row(agent)
                emoji = row.get("emoji", "🤖") if row else "🤖"
                rows.append([InlineKeyboardButton(f"{emoji} {agent}", callback_data=f"group:add_confirm:{group_name}:{agent}")])
            rows.append([InlineKeyboardButton("✕ Cancel", callback_data=f"group:view:{group_name}")])
            await query.edit_message_text(
                f"➕ Add to <b>{group_name}</b>\nSelect agents to add:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return

        # ── add confirmed ──
        if action == "add_confirm":
            agent_name = extra
            ok, msg = directory.group_add_member(group_name, agent_name)
            text, markup = self._group_detail_view(directory, group_name)
            prefix = "✅ " if ok else "❌ "
            await query.edit_message_text(prefix + msg + "\n\n" + text, parse_mode="HTML", reply_markup=markup)
            return

        # ── remove members ──
        if action == "remove":
            groups = directory.list_groups()
            current = groups.get(group_name, {}).get("members", [])
            if not current:
                await query.edit_message_text(f"Group <b>{group_name}</b> is empty.", parse_mode="HTML")
                return
            rows = []
            for agent in current:
                row = directory.get_agent_row(agent)
                emoji = row.get("emoji", "🤖") if row else "🤖"
                rows.append([InlineKeyboardButton(f"{emoji} {agent}", callback_data=f"group:remove_confirm:{group_name}:{agent}")])
            rows.append([InlineKeyboardButton("✕ Cancel", callback_data=f"group:view:{group_name}")])
            await query.edit_message_text(
                f"➖ Remove from <b>{group_name}</b>\nSelect agents to remove:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return

        # ── remove confirmed ──
        if action == "remove_confirm":
            agent_name = extra
            ok, msg = directory.group_remove_member(group_name, agent_name)
            text, markup = self._group_detail_view(directory, group_name)
            prefix = "✅ " if ok else "❌ "
            await query.edit_message_text(prefix + msg + "\n\n" + text, parse_mode="HTML", reply_markup=markup)
            return

        # ── rename (prompt) ──
        if action == "rename":
            await query.edit_message_text(
                f"To rename group <b>{group_name}</b>, send:\n<code>/group rename {group_name} &lt;new_name&gt;</code>",
                parse_mode="HTML",
            )
            return

        # ── start/stop/reboot all in group ──
        if action in ("start", "stop", "reboot"):
            orchestrator = getattr(self, "orchestrator", None)
            members = directory.resolve_group(group_name, exclude_self=self.name)
            if not members:
                await query.edit_message_text(f"Group <b>{group_name}</b> has no members to act on.", parse_mode="HTML")
                return

            lines = [f"<b>{'▶ Starting' if action == 'start' else '⏹ Stopping' if action == 'stop' else '🔄 Rebooting'} group {group_name}</b> ({len(members)} agents)\n"]

            if action == "reboot" and orchestrator:
                for name in members:
                    all_names = orchestrator.configured_agent_names()
                    if name in all_names:
                        num = all_names.index(name) + 1
                        orchestrator.request_restart(mode="number", agent_name=self.name, agent_number=num)
                        lines.append(f"  🔄 {name} — reboot queued")
                    else:
                        lines.append(f"  ⚠️ {name} — not found")
            elif action == "start" and orchestrator:
                for name in members:
                    ok, msg = await orchestrator.start_agent(name)
                    lines.append(f"  {'✅' if ok else '❌'} {name} — {msg}")
            elif action == "stop" and orchestrator:
                for name in members:
                    runtime = directory.get_runtime(name)
                    if runtime and hasattr(runtime, "backend_manager") and runtime.backend_manager.current_backend:
                        await runtime.backend_manager.current_backend.shutdown()
                        lines.append(f"  ⏹ {name} — stopped")
                    else:
                        lines.append(f"  ⚠️ {name} — not running or unavailable")
            else:
                lines.append("⚠️ Orchestrator unavailable.")

            await query.edit_message_text("\n".join(lines), parse_mode="HTML")
            return

        # ── broadcast message ──
        if action == "broadcast":
            members = directory.resolve_group(group_name, exclude_self=self.name)
            if not members:
                await query.edit_message_text(f"Group <b>{group_name}</b> has no members to broadcast to.", parse_mode="HTML")
                return
            await query.edit_message_text(
                f"📢 Broadcast to group <b>{group_name}</b> ({len(members)} agents)\n\n"
                f"Use: <code>/hchat @{group_name} &lt;your intent&gt;</code>",
                parse_mode="HTML",
            )
            return

    # ── /usage ────────────────────────────────────────────────────────────────

    async def cmd_usage(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        try:
            from tools.token_tracker import get_summary, format_summary_text
        except ImportError:
            await self._reply_text(update, "❌ token_tracker not available.")
            return

        args = [a.strip().lower() for a in (context.args or []) if a.strip()]
        show_all = args and args[0] == "all"

        if show_all:
            # Collect usage from all agents via orchestrator
            orchestrator = getattr(self, "orchestrator", None)
            if orchestrator is None:
                await self._reply_text(update, "❌ Orchestrator unavailable for all-agents view.")
                return
            lines = ["<b>📊 Token Usage — All Agents</b>\n"]
            total_cost = 0.0
            for runtime in orchestrator.runtimes:
                s = get_summary(runtime.workspace_dir, session_id=runtime.session_id_dt)
                all_t = s.get("all_time", {})
                if all_t.get("requests", 0) == 0:
                    continue
                tokens = all_t["input"] + all_t["output"]
                cost = all_t["cost_usd"]
                total_cost += cost
                sess = s.get("session", {}) or {}
                sess_tokens = sess.get("input", 0) + sess.get("output", 0)
                sess_cost = sess.get("cost_usd", 0.0)
                lines.append(
                    f"<b>{runtime.name}</b>  {tokens//1000}K tokens  ${cost:.4f}"
                    + (f"  (session {sess_tokens//1000}K ${sess_cost:.4f})" if sess.get("requests") else "")
                )
            lines.append(f"\n<b>Total: ${total_cost:.4f}</b>")
            await self._reply_text(update, "\n".join(lines), parse_mode="HTML")
        else:
            summary = get_summary(self.workspace_dir, session_id=self.session_id_dt)
            text = format_summary_text(summary, agent_name=self.name)
            await self._reply_text(update, text, parse_mode="HTML")

    async def cmd_token(self, update: Update, context: Any):
        """System-wide token usage summary grouped by backend type."""
        if not self._is_authorized_user(update.effective_user.id):
            return
        try:
            from tools.token_tracker import get_summary_extended, fmt_tokens, _week_start_utc, _month_start_utc
        except ImportError:
            await self._reply_text(update, "❌ token_tracker not available.")
            return

        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            await self._reply_text(update, "❌ Orchestrator unavailable.")
            return

        # ── Collect data from all runtimes ────────────────────────────────────
        groups: dict[str, dict[str, list]] = {}  # category -> backend -> [agent_entry]
        totals = {k: {"input": 0, "output": 0, "thinking": 0, "cost_usd": 0.0, "requests": 0}
                  for k in ("all_time", "session", "weekly", "monthly")}
        total_agents = 0

        for runtime in orchestrator.runtimes:
            summary = get_summary_extended(runtime.workspace_dir, session_id=runtime.session_id_dt)
            if summary["all_time"]["requests"] == 0:
                continue
            total_agents += 1

            # Current backend
            bm = getattr(runtime, "backend_manager", None)
            backend = (getattr(bm, "active_backend", None)
                       or getattr(getattr(runtime, "config", None), "active_backend", None)
                       or "unknown")

            # Current model
            model = "unknown"
            try:
                model = runtime.get_current_model() or "unknown"
            except Exception:
                pass

            # Categorise
            if backend.endswith("-cli"):
                cat = "🖥️ CLI Backends"
            elif backend.endswith("-api"):
                cat = "🌐 API Backends"
            else:
                cat = "❓ Other"

            groups.setdefault(cat, {}).setdefault(backend, []).append(
                {"name": runtime.name, "model": model, "summary": summary}
            )

            # Accumulate grand totals
            for period in ("all_time", "session", "weekly", "monthly"):
                src = summary.get(period) or {}
                for k in ("input", "output", "thinking", "cost_usd", "requests"):
                    totals[period][k] += src.get(k, 0)

        if total_agents == 0:
            await self._reply_text(update, "📊 No token usage recorded yet.")
            return

        # ── Build output ──────────────────────────────────────────────────────
        lines = ["<b>📊 Token Summary — All Agents</b>"]

        CAT_ORDER = ["🖥️ CLI Backends", "🌐 API Backends", "❓ Other"]
        for cat in CAT_ORDER:
            if cat not in groups:
                continue
            lines.append(f"\n<b>{cat}</b>")
            for backend, agents in sorted(groups[cat].items()):
                lines.append(f"  <b>{backend}</b>")
                backend_at = {"input": 0, "output": 0, "thinking": 0, "cost_usd": 0.0, "requests": 0}
                for agent in agents:
                    at = agent["summary"]["all_time"]
                    sess = agent["summary"].get("session") or {}
                    think_part = f"  💭{fmt_tokens(at['thinking'])}" if at["thinking"] > 0 else ""
                    sess_part  = (f"  <i>(sess ${sess['cost_usd']:.4f})</i>"
                                  if sess.get("requests", 0) > 0 else "")
                    lines.append(
                        f"    {agent['name']:<10} <code>{agent['model']}</code>"
                        f"  in:{fmt_tokens(at['input'])}"
                        f"  out:{fmt_tokens(at['output'])}"
                        f"{think_part}"
                        f"  <b>${at['cost_usd']:.4f}</b>{sess_part}"
                    )
                    for k in ("input", "output", "thinking", "cost_usd", "requests"):
                        backend_at[k] += at.get(k, 0)
                if len(agents) > 1:
                    lines.append(
                        f"    {'─'*38}\n"
                        f"    {'Subtotal':<10}  "
                        f"in:{fmt_tokens(backend_at['input'])}"
                        f"  out:{fmt_tokens(backend_at['output'])}"
                        f"  <b>${backend_at['cost_usd']:.4f}</b>"
                    )

        # ── Grand totals ──────────────────────────────────────────────────────
        lines.append(f"\n{'═'*44}")
        at = totals["all_time"]
        lines.append(
            f"<b>All-time</b>  {total_agents} agents"
            f"  in:{fmt_tokens(at['input'])}"
            f"  out:{fmt_tokens(at['output'])}"
            + (f"  💭{fmt_tokens(at['thinking'])}" if at["thinking"] > 0 else "")
            + f"  <b>${at['cost_usd']:.4f}</b>"
              f"  ({at['requests']} req)"
        )

        sess = totals["session"]
        if sess["requests"] > 0:
            lines.append(
                f"<b>Session</b>    in:{fmt_tokens(sess['input'])}"
                f"  out:{fmt_tokens(sess['output'])}"
                + (f"  💭{fmt_tokens(sess['thinking'])}" if sess["thinking"] > 0 else "")
                + f"  <b>${sess['cost_usd']:.4f}</b>"
            )

        weekly = totals["weekly"]
        if weekly["requests"] > 0:
            from datetime import timedelta as _td
            now_utc = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
            days_ago = (now_utc.weekday() + 1) % 7
            week_label = (now_utc - _td(days=days_ago)).strftime("%m/%d")
            lines.append(
                f"<b>This week</b>  (since {week_label})"
                f"  in:{fmt_tokens(weekly['input'])}"
                f"  out:{fmt_tokens(weekly['output'])}"
                + (f"  💭{fmt_tokens(weekly['thinking'])}" if weekly["thinking"] > 0 else "")
                + f"  <b>${weekly['cost_usd']:.4f}</b>"
            )

        monthly = totals["monthly"]
        if monthly["requests"] > 0:
            from datetime import timezone as _tz
            import datetime as _dt
            now_m = _dt.datetime.now(_tz.utc)
            month_label = now_m.strftime(f"%b 1–{now_m.day}")
            lines.append(
                f"<b>This month</b> ({month_label})"
                f"  in:{fmt_tokens(monthly['input'])}"
                f"  out:{fmt_tokens(monthly['output'])}"
                + (f"  💭{fmt_tokens(monthly['thinking'])}" if monthly["thinking"] > 0 else "")
                + f"  <b>${monthly['cost_usd']:.4f}</b>"
            )

        await self._reply_text(update, "\n".join(lines), parse_mode="HTML")

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
        if self.backend_manager.agent_mode == "wrapper":
            await self._reply_text(
                update,
                "Backend switching is managed by `/core` and `/wrap` in **wrapper** mode.\nUse `/mode flex` for normal `/backend` switching.",
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
        requested_model = None
        for raw_arg in args[1:]:
            raw_value = raw_arg.strip()
            if not raw_value:
                continue
            flag = raw_value.lower()
            if flag in {"+", "context", "handoff", "with-context"}:
                with_context = True
            else:
                requested_model = raw_value

        if target_engine not in allowed_engines:
            await self._reply_text(update, f"Backend not allowed: {target_engine}")
            return

        if requested_model:
            if target_engine == "claude-cli":
                requested_model = CLAUDE_MODEL_ALIASES.get(requested_model.lower(), requested_model)
            available = self._get_available_models_for(target_engine)
            if available and requested_model not in available:
                await self._reply_text(
                    update,
                    f"Unknown model for {target_engine}: {requested_model}\nUse /backend {target_engine} to see available options.",
                )
                return

            success, message = await self._switch_backend_mode(
                update.effective_chat.id,
                target_engine,
                target_model=requested_model,
                with_context=with_context,
            )
            await self._reply_text(update, message)
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

    async def cmd_ticket(self, update: Update, context: Any):
        """Submit an IT support ticket to Arale. Usage: /ticket <description>"""
        if not self._is_authorized_user(update.effective_user.id):
            return
        from orchestrator.ticket_manager import (
            create_ticket, detect_instance, format_ticket_notification,
            list_tickets, _resolve_tickets_dir,
        )

        args_text = " ".join(context.args).strip() if context.args else ""

        # /ticket with no args → list open tickets
        if not args_text:
            tickets_dir = _resolve_tickets_dir(self.global_config.project_root)
            open_tickets = list_tickets(tickets_dir, "open")
            ip_tickets = list_tickets(tickets_dir, "in_progress")
            lines = []
            if open_tickets:
                lines.append("Open tickets:")
                for t in open_tickets:
                    lines.append(f"  [{t['ticket_id']}] {t['source_agent']} — {t['summary'][:60]}")
            if ip_tickets:
                lines.append("In progress:")
                for t in ip_tickets:
                    lines.append(f"  [{t['ticket_id']}] {t['source_agent']} — {t['summary'][:60]}")
            if not lines:
                lines.append("No open tickets.")
            await self._reply_text(update, "\n".join(lines))
            return

        # Create the ticket (program-driven, no LLM needed)
        instance = detect_instance(self.global_config.project_root)
        ticket = create_ticket(
            project_root=self.global_config.project_root,
            source_agent=self.name,
            source_instance=instance,
            workspace_dir=self.workspace_dir,
            summary=args_text,
        )

        # Confirm to the submitting agent
        await self._reply_text(
            update,
            f"🎫 Ticket {ticket['ticket_id']} created.\n"
            f"Arale has been notified and will investigate.",
        )

        # Notify Arale via bridge (local) or hchat (cross-instance)
        notification = format_ticket_notification(ticket)
        orchestrator = getattr(self, "orchestrator", None)
        notified = False

        if orchestrator is not None:
            # Try to deliver via bridge to arale's runtime (same instance)
            for rt in getattr(orchestrator, "runtimes", []):
                if getattr(rt, "name", "") == "arale" and hasattr(rt, "enqueue_api_text"):
                    try:
                        await rt.enqueue_api_text(
                            f"[TICKET RECEIVED]\n{notification}\n\n"
                            f"Ticket file: {self.global_config.project_root / 'tickets' / 'open' / (ticket['ticket_id'] + '.json')}\n"
                            f"Please investigate and resolve per IT support protocol.",
                            source=f"ticket:{ticket['ticket_id']}",
                            deliver_to_telegram=True,
                        )
                        notified = True
                    except Exception as e:
                        logger.warning(f"Failed to notify arale via bridge: {e}")
                    break

        if not notified:
            # Arale not on this instance — deliver via hchat (real-time cross-instance)
            try:
                from tools.hchat_send import send_hchat
                hchat_text = (
                    f"[TICKET RECEIVED]\n{notification}\n\n"
                    f"Ticket file: {self.global_config.project_root / 'tickets' / 'open' / (ticket['ticket_id'] + '.json')}\n"
                    f"Please investigate and resolve per IT support protocol."
                )
                ok = send_hchat("arale", self.name, hchat_text)
                if ok:
                    notified = True
                    logger.info(f"Ticket {ticket['ticket_id']} notified to arale via hchat.")
                else:
                    logger.warning(f"Ticket {ticket['ticket_id']} hchat delivery to arale failed. Arale may be offline.")
            except Exception as e:
                logger.warning(f"Failed to notify arale via hchat: {e}")

        if not notified:
            logger.warning(f"Ticket {ticket['ticket_id']} created but could not notify arale. She will pick it up on next patrol.")

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

        self._workzone_dir = load_workzone(self.workspace_dir)
        self._sync_workzone_to_backend_config()
        switch_ok = await self.backend_manager.switch_backend(
            target_engine,
            target_model=target_model,
        )
        if not switch_ok:
            return False, f"Failed to switch backend to: {target_engine}"
        self._sync_workzone_to_backend_config()
        backend = self.backend_manager.current_backend
        if backend and getattr(backend.capabilities, "supports_sessions", False):
            await backend.handle_new_session()

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
        if self.backend_manager.agent_mode == "wrapper":
            await self._reply_text(
                update,
                "Model switching is managed by `/core` and `/wrap` in **wrapper** mode.\nUse `/mode flex` for normal `/model` switching.",
                parse_mode="Markdown",
            )
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

    def _is_wrapper_mode(self) -> bool:
        return getattr(self.backend_manager, "agent_mode", "flex") == "wrapper"

    async def _require_wrapper_mode(self, update: Update, command_name: str) -> bool:
        if self._is_wrapper_mode():
            return True
        await self._reply_text(
            update,
            f"`/{command_name}` only applies in **wrapper** mode.\nUse `/mode wrapper` first.",
            parse_mode="Markdown",
        )
        return False

    def _parse_backend_model_args(self, args: list[str]) -> tuple[dict[str, str], list[str]]:
        values: dict[str, str] = {}
        positional: list[str] = []
        for raw in args:
            if "=" in raw:
                key, value = raw.split("=", 1)
                key = key.strip().lower().replace("-", "_")
                value = value.strip()
                if key and value:
                    values[key] = value
            elif raw.strip():
                positional.append(raw.strip())
        return values, positional

    def _allowed_wrapper_engine(self, engine: str) -> bool:
        return any(b.get("engine") == engine for b in self.config.allowed_backends)

    def _normalize_wrapper_model(self, engine: str, model: str) -> str:
        if engine == "claude-cli":
            model = CLAUDE_MODEL_ALIASES.get(model.lower(), model)
        return normalize_model(engine, model) or model

    def _validate_wrapper_backend_model(self, engine: str, model: str) -> str | None:
        if not self._allowed_wrapper_engine(engine):
            allowed = ", ".join(b.get("engine", "?") for b in self.config.allowed_backends)
            return f"Backend not allowed for this agent: {engine}\nAllowed: {allowed}"
        available = self._get_available_models_for(engine)
        if available and model not in available:
            return f"Unknown model for {engine}: {model}"
        return None

    async def cmd_core(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        if not await self._require_wrapper_mode(update, "core"):
            return

        state = self.backend_manager.get_state_snapshot()
        cfg = load_wrapper_config(state)
        args = context.args or []
        if not args:
            await self._reply_text(
                update,
                "Wrapper core model:\n"
                f"• Backend: `{cfg.core_backend}`\n"
                f"• Model: `{cfg.core_model}`\n\n"
                "Usage: `/core backend=codex-cli model=gpt-5.5`",
                parse_mode="Markdown",
            )
            return

        values, positional = self._parse_backend_model_args(args)
        backend = values.get("backend") or (positional[0] if positional else cfg.core_backend)
        model = values.get("model") or (positional[1] if len(positional) > 1 else cfg.core_model)
        backend = backend.strip().lower()
        model = self._normalize_wrapper_model(backend, model.strip())

        error = self._validate_wrapper_backend_model(backend, model)
        if error:
            await self._reply_text(update, error)
            return

        self.backend_manager.update_wrapper_blocks(core={"backend": backend, "model": model})
        await self._reply_text(
            update,
            "Wrapper core updated:\n"
            f"• Backend: `{backend}`\n"
            f"• Model: `{model}`",
            parse_mode="Markdown",
        )

    async def cmd_wrap(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        if not await self._require_wrapper_mode(update, "wrap"):
            return

        state = self.backend_manager.get_state_snapshot()
        cfg = load_wrapper_config(state)
        args = context.args or []
        if not args:
            await self._reply_text(
                update,
                "Wrapper translator model:\n"
                f"• Backend: `{cfg.wrapper_backend}`\n"
                f"• Model: `{cfg.wrapper_model}`\n"
                f"• Context window: `{cfg.context_window}`\n"
                f"• Fallback: `{cfg.fallback}`\n\n"
                "Usage: `/wrap backend=claude-cli model=claude-haiku-4-5 context=3`",
                parse_mode="Markdown",
            )
            return

        values, positional = self._parse_backend_model_args(args)
        backend = values.get("backend") or (positional[0] if positional else cfg.wrapper_backend)
        model = values.get("model") or (positional[1] if len(positional) > 1 else cfg.wrapper_model)
        context_value = values.get("context_window") or values.get("context") or values.get("window")
        fallback = values.get("fallback") or cfg.fallback
        backend = backend.strip().lower()
        model = self._normalize_wrapper_model(backend, model.strip())

        error = self._validate_wrapper_backend_model(backend, model)
        if error:
            await self._reply_text(update, error)
            return

        context_window = cfg.context_window
        if context_value is not None:
            try:
                context_window = max(0, min(int(context_value), 20))
            except ValueError:
                await self._reply_text(update, "Context window must be an integer.")
                return

        self.backend_manager.update_wrapper_blocks(
            wrapper={
                "backend": backend,
                "model": model,
                "context_window": context_window,
                "fallback": fallback,
            }
        )
        await self._reply_text(
            update,
            "Wrapper translator updated:\n"
            f"• Backend: `{backend}`\n"
            f"• Model: `{model}`\n"
            f"• Context window: `{context_window}`\n"
            f"• Fallback: `{fallback}`",
            parse_mode="Markdown",
        )

    async def cmd_wrapper(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        if not await self._require_wrapper_mode(update, "wrapper"):
            return

        args = context.args or []
        action = (args[0].lower() if args else "list").strip()
        state = self.backend_manager.get_state_snapshot()
        slots = state.get("wrapper_slots")
        if not isinstance(slots, dict):
            slots = {}

        if action in {"list", "status"}:
            cfg = load_wrapper_config(state)
            lines = [
                "Wrapper mode configuration:",
                f"• Core: <code>{html.escape(cfg.core_backend)} / {html.escape(cfg.core_model)}</code>",
                f"• Wrapper: <code>{html.escape(cfg.wrapper_backend)} / {html.escape(cfg.wrapper_model)}</code>",
                f"• Context window: <code>{cfg.context_window}</code>",
                "",
                "Persona/style slots:",
            ]
            if slots:
                for key in sorted(slots, key=lambda value: (not str(value).isdigit(), int(value) if str(value).isdigit() else str(value))):
                    lines.append(f"• <code>{html.escape(str(key))}</code>: {html.escape(str(slots[key]))}")
            else:
                lines.append("• none")
            await self._reply_text(update, "\n".join(lines), parse_mode="HTML")
            return

        if action == "set":
            if len(args) < 3:
                await self._reply_text(update, "Usage: /wrapper set <slot> <text>")
                return
            slot = args[1].strip()
            text = " ".join(args[2:]).strip()
            if not slot or not text:
                await self._reply_text(update, "Usage: /wrapper set <slot> <text>")
                return
            slots[slot] = text
            self.backend_manager.update_wrapper_blocks(wrapper_slots=slots)
            await self._reply_text(update, f"Wrapper slot `{slot}` updated.", parse_mode="Markdown")
            return

        if action == "clear":
            if len(args) < 2:
                await self._reply_text(update, "Usage: /wrapper clear <slot|all>")
                return
            target = args[1].strip()
            if target.lower() == "all":
                slots = {}
                message = "All wrapper slots cleared."
            else:
                slots.pop(target, None)
                message = f"Wrapper slot `{target}` cleared."
            self.backend_manager.update_wrapper_blocks(wrapper_slots=slots)
            await self._reply_text(update, message, parse_mode="Markdown")
            return

        await self._reply_text(update, "Usage: /wrapper [list|set <slot> <text>|clear <slot|all>]")

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
            self.error_logger.error(f"callback_model error: {e}", exc_info=True)
            await query.answer(f"Error: {e}", show_alert=True)
            return
        await query.answer()

    async def cmd_mode(self, update: Update, context: Any):
        """Switch between fixed, flex, and wrapper configuration modes.

        Usage: /mode [fixed|flex|wrapper]
        """
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = (context.args[0].lower() if context.args else "").strip()
        current = self.backend_manager.agent_mode

        if not args or args not in ("fixed", "flex", "wrapper"):
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Fixed" if current == "fixed" else "Fixed", callback_data="tgl:mode:fixed"),
                InlineKeyboardButton("✅ Flex" if current == "flex" else "Flex", callback_data="tgl:mode:flex"),
                InlineKeyboardButton("✅ Wrapper" if current == "wrapper" else "Wrapper", callback_data="tgl:mode:wrapper"),
            ]])
            await self._reply_text(
                update,
                f"Current mode: <b>{current}</b>\n\n"
                f"• <b>fixed</b> — continuous CLI session, incremental prompts\n"
                f"• <b>flex</b> — multi-backend switching, full context injection\n"
                f"• <b>wrapper</b> — configure core/wrapper model pair with /core and /wrap",
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
        elif args == "flex":
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
        else:
            if hasattr(backend, "set_session_mode"):
                backend.set_session_mode(False)
            cfg = load_wrapper_config(self.backend_manager.get_state_snapshot())
            await self._reply_text(
                update,
                "Switched to **wrapper** mode.\n"
                f"• Core: `{cfg.core_backend}` / `{cfg.core_model}`\n"
                f"• Wrapper: `{cfg.wrapper_backend}` / `{cfg.wrapper_model}`\n"
                "• Use `/core`, `/wrap`, and `/wrapper` to configure\n"
                "• Response rewriting is enabled in a later implementation phase",
                parse_mode="Markdown",
            )

    async def cmd_workzone(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = context.args or []
        current = load_workzone(self.workspace_dir)
        if not args:
            if current:
                await self._reply_text(
                    update,
                    f"Workzone is ON:\n<code>{html.escape(str(current))}</code>\n\n"
                    "Use <code>/workzone off</code> to return to the agent home workspace.",
                    parse_mode="HTML",
                )
            else:
                await self._reply_text(
                    update,
                    f"Workzone is OFF. Agent home workspace:\n<code>{html.escape(str(self.workspace_dir))}</code>",
                    parse_mode="HTML",
                )
            return
        arg_text = " ".join(args).strip()
        if self._backend_busy():
            await self._reply_text(update, "Workzone change is blocked while a request is running or queued.")
            return
        if arg_text.lower() == "off":
            clear_workzone(self.workspace_dir)
            self._workzone_dir = None
            self._sync_workzone_to_backend_config()
            backend = self.backend_manager.current_backend
            if backend and getattr(backend.capabilities, "supports_sessions", False):
                await backend.handle_new_session()
            await self._reply_text(
                update,
                f"Workzone OFF. Working directory reset to agent home workspace:\n<code>{html.escape(str(self.workspace_dir))}</code>",
                parse_mode="HTML",
            )
            return
        try:
            zone = resolve_workzone_input(arg_text, self.global_config.project_root, self.workspace_dir)
        except ValueError as exc:
            await self._reply_text(update, f"Workzone not changed: {html.escape(str(exc))}", parse_mode="HTML")
            return
        save_workzone(self.workspace_dir, zone)
        self._workzone_dir = zone
        self._sync_workzone_to_backend_config()
        backend = self.backend_manager.current_backend
        if backend and getattr(backend.capabilities, "supports_sessions", False):
            await backend.handle_new_session()
        await self._reply_text(
            update,
            f"Workzone ON:\n<code>{html.escape(str(zone))}</code>\n\n"
            "Next request will run from this directory and include a workzone prompt.",
            parse_mode="HTML",
        )

    async def cmd_new(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        if not self.backend_manager.current_backend:
            return
        if not is_cli_backend(self.config.active_backend):
            await self._reply_text(
                update,
                "This agent is using a non-CLI backend. Use /fresh for a clean API context; /new is reserved for CLI session reset.",
            )
            return
        self._clear_transfer_state()
        # /new semantics (author intent): start stateless and ONLY rely on the agent's own agent.md
        # - No Bridge FYI injection
        # - No README/doc auto-reading claims
        # - No continuity restore
        self._pending_auto_recall_context = None

        # Clear recent_turns so polluted history from a previous mode doesn't bleed into the new session
        if hasattr(self.context_assembler, "memory_store") and hasattr(self.context_assembler.memory_store, "clear_turns"):
            self.context_assembler.memory_store.clear_turns()

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
        await self.enqueue_request(
            update.effective_chat.id,
            prompt,
            "system",
            "New session",
            skip_memory_injection=True,
        )

    async def cmd_fresh(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        if not self.backend_manager.current_backend:
            return
        if is_cli_backend(self.config.active_backend):
            await self._reply_text(
                update,
                "This agent is using a CLI backend. Use /new to reset the CLI session.",
            )
            return

        self._clear_transfer_state()
        self._pending_auto_recall_context = None
        assembler = getattr(self, "context_assembler", None)
        memory_store = getattr(assembler, "memory_store", None)
        if memory_store is not None and hasattr(memory_store, "clear_turns"):
            memory_store.clear_turns()
        if assembler is not None:
            assembler.turns_injection_enabled = True
            assembler.saved_memory_injection_enabled = False

        await self._reply_text(
            update,
            "Starting a fresh API context. Recent turns were cleared; saved memories are preserved but will not be auto-injected.",
        )
        prompt = (
            "SYSTEM: Fresh API context started. Do not reference previous chat or saved memories unless the user explicitly asks. "
            "Follow ONLY your agent.md instructions. Ask the user what they want to do next."
        )
        await self.enqueue_request(
            update.effective_chat.id,
            prompt,
            "system",
            "Fresh API context",
            skip_memory_injection=True,
        )

    def _get_skill_state(self) -> dict:
        path = self.workspace_dir / "skill_state.json"
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            return {}

    def _set_skill_state(self, key: str, value):
        path = self.workspace_dir / "skill_state.json"
        state = self._get_skill_state()
        state[key] = value
        path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    async def cmd_memory(self, update: Update, context: Any):
        """Control long-term memory injection and BGE sync for this agent.

        Usage:
          /memory              -> show current status
          /memory on           -> enable memory injection (default)
          /memory pause        -> disable injection without deleting any data
          /memory wipe         -> permanently delete all stored memories and turns
          /memory sync on      -> opt this agent into nightly BGE consolidation
          /memory sync off     -> opt this agent out of BGE consolidation (default)
        """
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = " ".join(context.args).strip().lower() if context.args else ""
        assembler = getattr(self, "context_assembler", None)

        if args in ("", "status"):
            if assembler:
                turns_state = "ON ✅" if assembler.turns_injection_enabled else "PAUSED ⏸️"
                saved_state = "ON ✅" if assembler.saved_memory_injection_enabled else "PAUSED ⏸️"
                state = f"turns={turns_state}, saved={saved_state}"
            else:
                state = "unknown (assembler not ready)"
            stats = self.memory_store.get_stats() if hasattr(self, "memory_store") else {}
            turns = stats.get("turns", "?")
            memories = stats.get("memories", "?")
            sync_on = self._get_skill_state().get("memory_sync", False)
            sync_state = "ON 🔄" if sync_on else "OFF ⬜"
            await self._reply_text(update,
                f"Memory injection: {state}\n"
                f"Stored: {turns} turns, {memories} memories\n"
                f"BGE sync: {sync_state}\n\n"
                f"Commands: /memory on | pause | saved on | saved off | saved status | wipe | sync on | sync off"
            )

        elif args == "on":
            if assembler:
                assembler.turns_injection_enabled = True
                assembler.saved_memory_injection_enabled = True
            await self._reply_text(update,
                "✅ Memory injection ON. Recent turns and saved memories will be included in context."
            )

        elif args == "pause":
            if assembler:
                assembler.turns_injection_enabled = False
                assembler.saved_memory_injection_enabled = False
            await self._reply_text(update,
                "⏸️ Memory injection PAUSED. Recent turns and saved memories are preserved but not injected into context.\n"
                "Use /memory on to resume."
            )

        elif args == "saved on":
            if assembler:
                assembler.saved_memory_injection_enabled = True
            await self._reply_text(update,
                "✅ Saved memory auto-injection ON. Long-term memories may be included in future context."
            )

        elif args == "saved off":
            if assembler:
                assembler.saved_memory_injection_enabled = False
            await self._reply_text(update,
                "⏸️ Saved memory auto-injection OFF. Long-term memories are preserved but not automatically injected."
            )

        elif args == "saved status":
            if assembler:
                state = "ON ✅" if assembler.saved_memory_injection_enabled else "PAUSED ⏸️"
            else:
                state = "unknown (assembler not ready)"
            await self._reply_text(update, f"Saved memory auto-injection: {state}")

        elif args == "wipe":
            if hasattr(self, "memory_store"):
                result = self.memory_store.clear_all()
                turns = result.get("deleted_turns", 0)
                mems = result.get("deleted_memories", 0)
                if assembler:
                    turns_state = "ON" if assembler.turns_injection_enabled else "PAUSED"
                    saved_state = "ON" if assembler.saved_memory_injection_enabled else "PAUSED"
                    state = f"turns={turns_state}, saved={saved_state}"
                else:
                    state = "unknown"
                await self._reply_text(update,
                    f"🗑️ Memory wiped: {turns} turns and {mems} memories deleted.\n"
                    f"Database structure preserved. Injection is still {state}."
                )
            else:
                await self._reply_text(update, "❌ Memory store not available.")

        elif args == "sync on":
            self._set_skill_state("memory_sync", True)
            agent = self.workspace_dir.name
            await self._reply_text(update,
                f"🔄 Memory sync ON for {agent}.\n"
                f"This agent's important memories will be queued for nightly BGE consolidation.\n"
                f"Use /memory sync off to opt out."
            )

        elif args == "sync off":
            self._set_skill_state("memory_sync", False)
            agent = self.workspace_dir.name
            await self._reply_text(update,
                f"⬜ Memory sync OFF for {agent}.\n"
                f"This agent will not participate in BGE consolidation.\n"
                f"Local memories are unaffected. Use /memory sync on to re-enable."
            )

        else:
            await self._reply_text(update,
                "Usage: /memory [on | pause | saved on | saved off | saved status | wipe | sync on | sync off | status]"
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

        # Re-init habit store (wipe/reset deletes habits.sqlite)
        self.habit_store = HabitStore(
            self.workspace_dir,
            self.global_config.project_root,
            self.name,
            self._get_agent_class(),
        )

        # Reset any pending continuity
        self._pending_auto_recall_context = None
        self._pending_session_primer = None
        self._clear_transfer_state()

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

        # Re-init habit store (wipe/reset deletes habits.sqlite)
        self.habit_store = HabitStore(
            self.workspace_dir,
            self.global_config.project_root,
            self.name,
            self._get_agent_class(),
        )

        # Reset any pending continuity
        self._pending_auto_recall_context = None
        self._pending_session_primer = None
        self._clear_transfer_state()

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

    # ------------------------------------------------------------------
    # /remote — one-click Hashi Remote start/stop
    # ------------------------------------------------------------------

    def _remote_config_snapshot(self) -> dict[str, Any]:
        root = self.global_config.project_root
        config_path = root / "remote" / "config.yaml"
        agents_path = root / "agents.json"
        instances_path = root / "instances.json"
        data: dict[str, Any] = {}
        try:
            data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        server = data.get("server") or {}
        discovery = data.get("discovery") or {}
        configured_port = server.get("port") or 8766
        try:
            agents = json.loads(agents_path.read_text(encoding="utf-8-sig")) if agents_path.exists() else {}
        except Exception:
            agents = {}
        global_cfg = agents.get("global") or {}
        if global_cfg.get("remote_port"):
            configured_port = global_cfg.get("remote_port")
        instance_id = str(global_cfg.get("instance_id") or "").strip().lower()
        if instances_path.exists() and instance_id:
            try:
                instances = json.loads(instances_path.read_text(encoding="utf-8")).get("instances", {}) or {}
                configured_port = (instances.get(instance_id) or {}).get("remote_port") or configured_port
            except Exception:
                pass
        return {
            "root": root,
            "port": int(configured_port or 8766),
            "use_tls": bool(server.get("use_tls", True)),
            "backend": str(discovery.get("backend") or "lan"),
        }

    def _remote_urls(self, path: str) -> list[str]:
        cfg = self._remote_config_snapshot()
        port = int(cfg["port"])
        schemes = ("https", "http") if cfg["use_tls"] else ("http", "https")
        return [f"{scheme}://127.0.0.1:{port}{path}" for scheme in schemes]

    async def _fetch_remote_json(self, path: str) -> tuple[dict[str, Any] | None, str | None]:
        timeout = aiohttp.ClientTimeout(total=4)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for url in self._remote_urls(path):
                try:
                    async with session.get(url, ssl=False) as resp:
                        if resp.status >= 500:
                            continue
                        return await resp.json(), url
                except Exception:
                    continue
        return None, None

    def _remote_start_log_path(self) -> Path:
        log_dir = self.global_config.project_root / "tmp"
        log_dir.mkdir(parents=True, exist_ok=True)
        agent_name = getattr(self.config, "agent_name", None) or "agent"
        return log_dir / f"{agent_name}_remote_startup.log"

    def _read_remote_start_log_excerpt(self, path: Path, max_chars: int = 1200) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            return ""
        if not text:
            return ""
        return text[-max_chars:]

    def _build_remote_start_failure_message(
        self,
        *,
        cfg: dict[str, Any],
        cmd: list[str],
        reason: str,
        log_path: Path,
        exit_code: int | None = None,
    ) -> str:
        cmd_text = html.escape(" ".join(str(part) for part in cmd))
        reason_text = html.escape(str(reason or "unknown startup failure"))
        lines = [
            "🔴 Hashi Remote failed to start.",
            f"Reason: <code>{reason_text}</code>",
            f"Port: <code>{cfg['port']}</code>  ·  TLS: <code>{'on' if cfg['use_tls'] else 'off'}</code>  ·  discovery: <code>{cfg['backend']}</code>",
        ]
        if exit_code is not None:
            lines.append(f"Exit code: <code>{exit_code}</code>")
        lines.append(f"Command: <code>{cmd_text}</code>")
        excerpt = self._read_remote_start_log_excerpt(log_path)
        if excerpt:
            lines.append(f"log tail: <code>{html.escape(excerpt)}</code>")
        else:
            lines.append(f"log file: <code>{html.escape(str(log_path))}</code>")
        return "\n".join(lines)

    async def _await_remote_start_health(
        self,
        *,
        process: asyncio.subprocess.Process,
        cfg: dict[str, Any],
        cmd: list[str],
        log_path: Path,
        timeout_seconds: float = 8.0,
    ) -> tuple[bool, str]:
        deadline = time.time() + max(1.0, float(timeout_seconds))
        while time.time() < deadline:
            if process.returncode is not None:
                return False, self._build_remote_start_failure_message(
                    cfg=cfg,
                    cmd=cmd,
                    reason="process exited before /health became ready",
                    log_path=log_path,
                    exit_code=process.returncode,
                )
            health, health_url = await self._fetch_remote_json("/health")
            if health:
                return True, str(health_url or "")
            await asyncio.sleep(0.5)

        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=2)
        except Exception:
            with suppress(Exception):
                process.kill()
        return False, self._build_remote_start_failure_message(
            cfg=cfg,
            cmd=cmd,
            reason="health endpoint did not become ready within timeout",
            log_path=log_path,
            exit_code=process.returncode,
        )

    def _format_remote_age(self, timestamp: Any) -> str:
        try:
            value = int(float(timestamp or 0))
        except (TypeError, ValueError):
            return "n/a"
        if value <= 0:
            return "n/a"
        delta = max(0, int(time.time()) - value)
        if delta < 60:
            return f"{delta}s ago"
        if delta < 3600:
            return f"{delta // 60}m ago"
        if delta < 86400:
            return f"{delta // 3600}h ago"
        return f"{delta // 86400}d ago"

    def _remote_peer_presence(self, peer: dict[str, Any]) -> tuple[int, str, str]:
        props = peer.get("properties") or {}
        live_status = str(props.get("live_status") or "").strip().lower()
        state = str(props.get("handshake_state") or "unknown")
        last_handshake_at = props.get("last_handshake_at")
        last_seen_ok = props.get("last_seen_ok")
        last_seen_error = props.get("last_seen_error")
        last_error = props.get("last_error")
        last_age = self._format_remote_age(last_handshake_at)
        stale = last_age != "n/a" and isinstance(last_handshake_at, (int, float, str))
        if stale:
            try:
                stale = (time.time() - float(last_handshake_at)) > 45
            except (TypeError, ValueError):
                stale = False
        if live_status == "online":
            return 0, "🟢 online", state
        if live_status == "stale":
            return 2, "🟠 stale", state
        if live_status == "offline":
            return 3, "🔴 offline", state
        if state in {"handshake_timed_out", "handshake_rejected", "unreachable"}:
            return 3, "🔴 offline", state
        if state == "handshake_in_progress" and (last_seen_error or last_error) and not last_seen_ok:
            return 3, "🔴 offline", state
        if state == "handshake_accepted" and not stale:
            return 0, "🟢 online", state
        if state == "handshake_in_progress":
            return 1, "🟡 connecting", state
        if state in {"handshake_pending", "unknown"}:
            return 1, "🟡 pending", state
        if state == "handshake_accepted" and stale:
            return 2, "🟠 stale", state
        return 3, "🔴 offline", state

    def _render_remote_peer_block(self, peer: dict[str, Any]) -> list[str]:
        props = peer.get("properties") or {}
        _rank, presence, state = self._remote_peer_presence(peer)
        instance_id = html.escape(str(peer.get("instance_id") or "unknown"))
        port = html.escape(str(peer.get("port") or "?"))
        backend = html.escape(str(props.get("preferred_backend") or props.get("discovery") or "unknown"))
        agents = len(props.get("remote_agents") or [])
        last_handshake = html.escape(self._format_remote_age(props.get("last_handshake_at")))
        last_seen_ok = html.escape(self._format_remote_age(props.get("last_seen_ok")))
        state_safe = html.escape(state)
        endpoint_lines = self._render_remote_peer_endpoints(peer)
        lines = [
            f"{presence} <b>{instance_id}</b>",
            *endpoint_lines,
            f"backend: <code>{backend}</code>  ·  port: <code>{port}</code>  ·  agents: <code>{agents}</code>",
            f"state: <code>{state_safe}</code>  ·  last handshake: <code>{last_handshake}</code>  ·  last seen: <code>{last_seen_ok}</code>",
        ]
        last_error = html.escape(str(props.get("last_error") or "").strip())
        if last_error:
            lines.append(f"error: <code>{last_error}</code>")
        refresh_error = html.escape(str(props.get("last_refresh_error") or "").strip())
        if refresh_error:
            lines.append(f"refresh: <code>{refresh_error}</code>")
        return lines

    def _load_remote_instances(self) -> dict[str, dict[str, Any]]:
        path = self.global_config.project_root / "instances.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        instances = data.get("instances") or {}
        return instances if isinstance(instances, dict) else {}

    def _peer_network_hosts(self, peer: dict[str, Any], entry: dict[str, Any]) -> list[str]:
        props = peer.get("properties") or {}
        hosts: list[str] = []
        seen: set[str] = set()

        def _add(value: Any) -> None:
            host = str(value or "").strip()
            if not host or host in {"127.0.0.1", "localhost", "0.0.0.0"}:
                return
            if host in seen:
                return
            seen.add(host)
            hosts.append(host)

        for key in ("lan_ip", "tailscale_ip", "api_host"):
            _add(entry.get(key))
        for field in ("address_candidates", "observed_candidates"):
            for item in props.get(field) or []:
                if not isinstance(item, dict):
                    continue
                scope = str(item.get("scope") or "").strip().lower()
                if scope in {"lan", "overlay", "routable", "peer"}:
                    _add(item.get("host"))
        return hosts

    def _render_remote_peer_endpoints(self, peer: dict[str, Any]) -> list[str]:
        instance_id = str(peer.get("instance_id") or "").strip().lower()
        entry = self._load_remote_instances().get(instance_id, {}) if instance_id else {}
        route_host = str(peer.get("resolved_route_host") or peer.get("host") or entry.get("api_host") or "?").strip() or "?"
        route_port = str(peer.get("resolved_route_port") or peer.get("port") or entry.get("remote_port") or "?").strip() or "?"
        network_hosts = self._peer_network_hosts(peer, entry if isinstance(entry, dict) else {})
        display_network_host = str(peer.get("display_network_host") or "").strip()
        if display_network_host and display_network_host not in network_hosts:
            network_hosts.insert(0, display_network_host)
        same_host = bool(peer.get("same_host")) or bool(str((entry or {}).get("same_host_loopback") or "").strip())

        if same_host and route_host in {"127.0.0.1", "localhost"}:
            network_host = network_hosts[0] if network_hosts else ""
            line = f"route: <code>{html.escape(route_host)}:{html.escape(route_port)}</code>  ·  <code>same host</code>"
            if network_host:
                line += f"  ·  network: <code>{html.escape(network_host)}:{html.escape(route_port)}</code>"
            return [line]

        if network_hosts and route_host not in network_hosts and route_host not in {"?", ""}:
            return [
                f"route: <code>{html.escape(route_host)}:{html.escape(route_port)}</code>",
                f"network: <code>{html.escape(network_hosts[0])}:{html.escape(route_port)}</code>",
            ]

        return [f"addr: <code>{html.escape(route_host)}:{html.escape(route_port)}</code>"]
    async def cmd_remote(self, update: Update, context: Any):
        """Start/stop Hashi Remote. Usage: /remote [on|off|status|list]"""
        if not self._is_authorized_user(update.effective_user.id):
            return
        arg = (context.args[0].lower() if context.args else "").strip()
        cfg = self._remote_config_snapshot()
        alive = self._remote_process is not None and self._remote_process.returncode is None

        # Status check
        if arg == "status" or not arg:
            health, health_url = await self._fetch_remote_json("/health")
            status, _status_url = await self._fetch_remote_json("/protocol/status")
            if not health:
                if alive:
                    await self._reply_text(
                        update,
                        "🟡 Hashi Remote process is running, but the API did not respond.\n"
                        f"PID: {self._remote_process.pid}\n"
                        f"Expected port: {cfg['port']}  ·  TLS: {'on' if cfg['use_tls'] else 'off'}"
                    )
                else:
                    await self._reply_text(update, "⚪ Hashi Remote is not running. Use /remote on to start.")
                return
            instance = health.get("instance") or {}
            peers = list((health.get("peers") or []))
            lines = [
                "🟢 <b>Hashi Remote Status</b>",
                f"Instance: <code>{instance.get('instance_id') or self.global_config.project_root.name.upper()}</code>",
                f"API: <code>{health_url}</code>",
                f"Port: <code>{cfg['port']}</code>  ·  TLS: <code>{'on' if cfg['use_tls'] else 'off'}</code>",
                f"Discovery: <code>{cfg['backend']}</code>",
                f"Process: <code>{'running' if alive else 'external/unknown'}</code>" + (f" (PID {self._remote_process.pid})" if alive else ""),
                f"Peers: <code>{len(peers)}</code>",
            ]
            if status:
                inflight = int(status.get("inflight_count") or 0)
                lines.append(f"Inflight: <code>{inflight}</code>")
            await self._reply_text(update, "\n".join(lines), parse_mode="HTML")
            return

        if arg == "list":
            data, _url = await self._fetch_remote_json("/peers")
            peers = list((data or {}).get("peers") or [])
            if not peers:
                await self._reply_text(update, "⚪ No remote peers are currently visible.")
                return
            peers = sorted(
                peers,
                key=lambda peer: (
                    self._remote_peer_presence(peer)[0],
                    str(peer.get("instance_id") or ""),
                ),
            )
            counts = {"online": 0, "attention": 0, "offline": 0}
            for peer in peers:
                rank, _presence, _state = self._remote_peer_presence(peer)
                if rank == 0:
                    counts["online"] += 1
                elif rank in {1, 2}:
                    counts["attention"] += 1
                else:
                    counts["offline"] += 1
            lines = [
                "📡 <b>Remote Instances</b>",
                f"online: <code>{counts['online']}</code>  ·  attention: <code>{counts['attention']}</code>  ·  offline: <code>{counts['offline']}</code>",
                f"refreshed: <code>{datetime.now().strftime('%H:%M:%S')}</code>",
                "",
            ]
            for idx, peer in enumerate(peers):
                lines.extend(self._render_remote_peer_block(peer))
                if idx != len(peers) - 1:
                    lines.append("")
            await self._reply_text(update, "\n".join(lines), parse_mode="HTML")
            return

        if arg == "off":
            if self._remote_process is None or self._remote_process.returncode is not None:
                await self._reply_text(update, "⚪ Hashi Remote is not running.")
                return
            self._remote_process.terminate()
            try:
                await asyncio.wait_for(self._remote_process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._remote_process.kill()
            self._remote_process = None
            await self._reply_text(update, "🔴 Hashi Remote stopped.")
            return

        if arg == "on":
            if alive:
                await self._reply_text(update, "🟢 Already running (PID %d)." % self._remote_process.pid)
                return

            root = cfg["root"]
            venv_python = root / ".venv" / "bin" / "python3"
            if not venv_python.exists():
                venv_python = root / ".venv" / "Scripts" / "python.exe"  # Windows
            if not venv_python.exists():
                await self._reply_text(
                    update,
                    f"🔴 Hashi Remote could not start.\nMissing interpreter: <code>{html.escape(str(venv_python))}</code>",
                    parse_mode="HTML",
                )
                return

            cmd = [str(venv_python), "-m", "remote"]
            cmd.extend(["--port", str(cfg["port"])])
            if not cfg["use_tls"]:
                cmd.append("--no-tls")
            if cfg["backend"] in {"lan", "tailscale", "both"}:
                cmd.extend(["--discovery", cfg["backend"]])
            log_path = self._remote_start_log_path()
            with suppress(Exception):
                log_path.unlink()
            log_handle = log_path.open("ab")
            try:
                self._remote_process = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(root),
                    stdout=log_handle,
                    stderr=log_handle,
                )
            finally:
                log_handle.close()

            ok, detail = await self._await_remote_start_health(
                process=self._remote_process,
                cfg=cfg,
                cmd=cmd,
                log_path=log_path,
            )
            if not ok:
                self._remote_process = None
                await self._reply_text(update, detail, parse_mode="HTML")
                return
            await self._reply_text(
                update,
                f"🟢 Hashi Remote started (PID {self._remote_process.pid})\n"
                f"   Port {cfg['port']} · TLS {'on' if cfg['use_tls'] else 'off'} · discovery {cfg['backend']}\n"
                f"   API <code>{html.escape(detail)}</code>\n"
                "   Use /remote off to stop.",
                parse_mode="HTML",
            )
            return

        await self._reply_text(update, "Usage: /remote [on|off|status|list]")

    async def cmd_oll(self, update: Update, context: Any):
        """Start/stop OLL Browser Gateway. Usage: /oll [on|off|status]"""
        if not self._is_authorized_user(update.effective_user.id):
            return
        from browser_gateway.service_control import start as start_oll_service, status as oll_status, stop as stop_oll_service

        arg = (context.args[0].lower() if context.args else "").strip()
        root = self.global_config.project_root

        if arg == "on":
            state = start_oll_service(root)
            await self._reply_text(
                update,
                "🟢 OLL Browser Gateway started.\n"
                f"PID: {state.get('pid') or 'unknown'}\n"
                f"Base URL: {state.get('base_url')}\n"
                f"Log: {state.get('log_file')}"
            )
            return

        if arg == "off":
            was_running = oll_status(root)
            state = stop_oll_service(root)
            if was_running.get("running"):
                await self._reply_text(update, "🔴 OLL Browser Gateway stopped.")
            else:
                await self._reply_text(update, "⚪ OLL Browser Gateway is not running.")
            return

        if arg == "status" or not arg:
            state = oll_status(root)
            if state.get("running"):
                await self._reply_text(
                    update,
                    "🟢 OLL Browser Gateway is running.\n"
                    f"PID: {state.get('pid')}\n"
                    f"Base URL: {state.get('base_url')}\n"
                    f"Log: {state.get('log_file')}\n"
                    f"State DB: {state.get('state_db')}"
                )
            else:
                await self._reply_text(
                    update,
                    "⚪ OLL Browser Gateway is not running.\n"
                    f"Base URL: {state.get('base_url')}\n"
                    "Use /oll on to start."
                )
            return

        await self._reply_text(update, "Usage: /oll [on|off|status]")

    async def cmd_wol(self, update: Update, context: Any):
        """Private local-only Wake-on-LAN helper. Usage: /wol [target]"""
        if not self._is_authorized_user(update.effective_user.id):
            return

        project_root = self.global_config.project_root
        if not private_wol_available(project_root):
            await self._reply_text(update, "⚪ /wol is not enabled on this instance.")
            return

        arg = (context.args[0].strip().lower() if context.args else "")
        if not arg or arg in {"list", "status", "help"}:
            targets = describe_wol_targets(project_root)
            lines = ["🪄 Private WoL targets on this instance:"]
            for row in targets:
                desc = f" — {row['description']}" if row["description"] else ""
                lines.append(f"- {row['name']} ({row['label']}){desc}")
            lines.append("")
            lines.append("Usage: /wol <pc_name>")
            await self._reply_text(update, "\n".join(lines))
            return

        await self._reply_text(update, f"🪄 Sending Wake-on-LAN packet for `{arg}`…")
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, lambda: run_private_wol(project_root, arg))
        if result.get("ok"):
            output = (result.get("stdout") or "").strip()
            if len(output) > 2500:
                output = output[:2500] + "\n...[truncated]"
            lines = [f"✅ WoL completed for {result.get('label') or arg}."]
            if output:
                lines.append("")
                lines.append(output)
            await self._reply_text(update, "\n".join(lines))
            return

        error = result.get("error") or result.get("stderr") or "unknown error"
        available = result.get("available_targets") or []
        lines = [f"❌ WoL failed for {arg}: {error}"]
        if available:
            lines.append(f"Available targets: {', '.join(available)}")
        await self._reply_text(update, "\n".join(lines))

    # ------------------------------------------------------------------
    # /long ... /end buffering (collect split Telegram messages)
    # ------------------------------------------------------------------

    async def cmd_long(self, update: Update, context: Any):
        """Start collecting multi-message input. Usage: /long [optional first line]"""
        if not self._is_authorized_user(update.effective_user.id):
            return
        # If already buffering, treat as nested /long — just acknowledge
        if self._long_buffer_active:
            await self._reply_text(update, "⏳ Already in /long mode. Send /end to finish.")
            return
        self._long_buffer = []
        self._long_buffer_active = True
        self._long_buffer_chat_id = update.effective_chat.id
        # If text was provided after /long, include it as the first chunk
        args_text = " ".join(context.args).strip() if context.args else ""
        if args_text:
            self._long_buffer.append(args_text)
        # Start safety timeout (5 minutes)
        if self._long_buffer_timeout_task and not self._long_buffer_timeout_task.done():
            self._long_buffer_timeout_task.cancel()
        self._long_buffer_timeout_task = asyncio.create_task(self._long_buffer_timeout())
        await self._reply_text(update, "📝 /long mode started. Paste your text, then send /end to submit.")

    async def cmd_end(self, update: Update, context: Any):
        """End /long buffering and submit collected text."""
        if not self._is_authorized_user(update.effective_user.id):
            return
        if not self._long_buffer_active:
            await self._reply_text(update, "No /long session active.")
            return
        # Cancel timeout
        if self._long_buffer_timeout_task and not self._long_buffer_timeout_task.done():
            self._long_buffer_timeout_task.cancel()
            self._long_buffer_timeout_task = None
        # Assemble and submit
        combined = "\n".join(self._long_buffer).strip()
        self._long_buffer = []
        self._long_buffer_active = False
        chat_id = self._long_buffer_chat_id or update.effective_chat.id
        self._long_buffer_chat_id = None
        if not combined:
            await self._reply_text(update, "⚠️ /long buffer was empty, nothing to submit.")
            return
        chunk_count = len(combined.splitlines())
        await self._reply_text(update, f"✅ Collected {chunk_count} lines. Submitting...")
        _print_user_message(self.name, combined)
        await self.enqueue_request(chat_id, combined, "text", _safe_excerpt(combined))

    async def _long_buffer_timeout(self):
        """Safety timeout: auto-submit after 5 minutes."""
        try:
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            return
        if not self._long_buffer_active:
            return
        combined = "\n".join(self._long_buffer).strip()
        self._long_buffer = []
        self._long_buffer_active = False
        chat_id = self._long_buffer_chat_id
        self._long_buffer_chat_id = None
        self._long_buffer_timeout_task = None
        if chat_id and combined:
            await self.send_long_message(
                chat_id,
                f"⏰ /long auto-submitted after 5min timeout ({len(combined.splitlines())} lines).",
                request_id=f"long-timeout-{uuid4().hex[:8]}",
                purpose="long-timeout",
            )
            _print_user_message(self.name, combined)
            await self.enqueue_request(chat_id, combined, "text", _safe_excerpt(combined))
        elif chat_id:
            await self.send_long_message(
                chat_id,
                "⏰ /long timed out with empty buffer. Cancelled.",
                request_id=f"long-timeout-{uuid4().hex[:8]}",
                purpose="long-timeout",
            )

    async def handle_message(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            self.logger.warning(f"Ignored message from unauthorized user ID: {update.effective_user.id}")
            return
        self._record_active_chat(update)
        if self._should_redirect_after_transfer():
            await self._reply_text(update, self._transfer_redirect_text())
            return
        text = update.message.text
        # If in /long buffering mode, collect instead of processing
        if self._long_buffer_active:
            self._long_buffer.append(text)
            return
        _print_user_message(self.name, text)
        self._capture_followup_habit_feedback(text)
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
        self._record_active_chat(update)
        if self._should_redirect_after_transfer():
            await self._reply_text(update, self._transfer_redirect_text())
            return
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
        if self._should_redirect_after_transfer():
            await self._reply_text(update, self._transfer_redirect_text())
            return
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

            # Safe voice: show confirmation before sending to agent
            if self._safevoice_enabled:
                chat_id = update.effective_chat.id
                chat_key = str(chat_id)
                self._pending_voice[chat_key] = {
                    "prompt": prompt,
                    "transcript": transcript,
                    "summary": f"{media_kind}: {filename}",
                    "timestamp": datetime.now().isoformat(),
                }
                max_preview = 3500
                if len(transcript) > max_preview:
                    preview = transcript[:max_preview] + f"\n\n…(共 {len(transcript)} 字，已截断)"
                else:
                    preview = transcript
                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Send", callback_data=f"safevoice:yes:{chat_key}"),
                        InlineKeyboardButton("❌ Discard", callback_data=f"safevoice:no:{chat_key}"),
                    ]
                ])
                await self._reply_text(
                    update,
                    f"🛡️ *Safe Voice — Confirm transcription:*\n\n_{preview}_",
                    reply_markup=keyboard,
                    parse_mode="Markdown",
                )
            else:
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
        if self._should_redirect_after_transfer():
            await self._reply_text(update, self._transfer_redirect_text())
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
                # Split into multiple messages instead of truncating
                if len(chunk_raw) <= tg_max_len:
                    await self.app.bot.send_message(chat_id=chat_id, text=chunk_raw)
                else:
                    remain = chunk_raw
                    while remain:
                        if len(remain) <= tg_max_len:
                            await self.app.bot.send_message(chat_id=chat_id, text=remain)
                            break
                        split_at = remain.rfind("\n", 0, tg_max_len)
                        if split_at == -1:
                            split_at = tg_max_len
                        await self.app.bot.send_message(chat_id=chat_id, text=remain[:split_at])
                        remain = remain[split_at:].lstrip("\n")

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
            # Always track thinking volume for token estimation (CLI backends only;
            # OpenRouter gets real counts from response.usage)
            if event.kind == KIND_THINKING and _engine != "openrouter-api":
                raw = event.summary or ""
                if raw and raw not in ("Thinking...",):
                    self._thinking_chars_this_req += len(raw)
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
                self._record_habit_outcome(item, success=False, error_text="background_task_cancelled")
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
                self._record_habit_outcome(item, success=False, error_text=str(exc))
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
                display_text = self._strip_transfer_accept_prefix(item, response.text)
                self._mark_success()
                self._record_habit_outcome(item, success=True, response_text=response.text)
                if self._should_buffer_during_transfer(item.request_id):
                    self._record_suppressed_transfer_result(item, success=True, text=display_text or response.text)
                    return
                self.last_response = {
                    "chat_id": item.chat_id,
                    "text": display_text or response.text,
                    "request_id": item.request_id,
                    "responded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
                try:
                    from tools.token_tracker import estimate_tokens, record_audit_event, record_usage
                    import hashlib as _hashlib
                    if response.usage:
                        # Real usage from API/CLI backend
                        _bg_input_tok = response.usage.input_tokens
                        _bg_output_tok = response.usage.output_tokens
                        _bg_thinking_tok = response.usage.thinking_tokens
                        _bg_tok_source = "api"
                        record_usage(
                            self.workspace_dir,
                            model=self.get_current_model(),
                            backend=self.config.active_backend,
                            input_tokens=_bg_input_tok,
                            output_tokens=_bg_output_tok,
                            thinking_tokens=_bg_thinking_tok,
                            session_id=self.session_id_dt,
                            cost_usd=getattr(response, "cost_usd", None),
                        )
                    else:
                        # CLI backend: estimate from full assembled prompt (includes history)
                        fallback_input = estimate_tokens(self._get_system_prompt_text()) + estimate_tokens(item.prompt)
                        _bg_input_tok = self._last_full_prompt_tokens or fallback_input
                        _bg_output_tok = estimate_tokens(display_text or response.text)
                        _bg_thinking_tok = self._thinking_chars_this_req // 4
                        _bg_tok_source = "estimated"
                        record_usage(
                            self.workspace_dir,
                            model=self.get_current_model(),
                            backend=self.config.active_backend,
                            input_tokens=_bg_input_tok,
                            output_tokens=_bg_output_tok,
                            thinking_tokens=_bg_thinking_tok,
                            session_id=self.session_id_dt,
                        )
                    _pa = self._last_prompt_audit
                    _sec_chars = {s["key"]: s["chars"] for s in _pa.get("sections", [])}
                    _sec_tokens = {s["key"]: s.get("tokens_est") or max(1, s["chars"] // 4) for s in _pa.get("sections", [])}
                    _sec_counts = {s["key"]: s.get("item_count", 0) for s in _pa.get("sections", [])}
                    record_audit_event(self.workspace_dir, {
                        "request_id": item.request_id,
                        "agent": self.name,
                        "runtime": "flex",
                        "backend": self.config.active_backend,
                        "model": self.get_current_model(),
                        "source": item.source,
                        "summary": item.summary,
                        "silent": item.silent,
                        "is_retry": item.is_retry,
                        "success": response.is_success,
                        "incremental_mode": False,
                        "token_source": _bg_tok_source,
                        "raw_prompt_chars": len(item.prompt),
                        "final_prompt_chars": self._last_full_prompt_tokens * 4,
                        "response_chars": len(response.text or ""),
                        "input_tokens": _bg_input_tok,
                        "output_tokens": _bg_output_tok,
                        "thinking_tokens": _bg_thinking_tok,
                        "tool_call_count": int(getattr(response, "tool_call_count", 0) or 0),
                        "tool_loop_count": int(getattr(response, "tool_loop_count", 0) or 0),
                        "tool_catalog_count": 0,
                        "tool_schema_chars": 0,
                        "tool_schema_tokens_est": 0,
                        "tool_schema_fingerprint": "",
                        "tool_max_loops": 0,
                        "budget_applied": bool(_pa.get("budget_applied")),
                        "context_expansion_ratio": round((_bg_input_tok * 4) / max(len(item.prompt), 1), 3),
                        "context_fingerprint": _pa.get("context_fingerprint", ""),
                        "request_fingerprint": _hashlib.sha1((item.prompt or "").encode("utf-8")).hexdigest()[:16],
                        "section_chars": _sec_chars,
                        "section_tokens_est": _sec_tokens,
                        "section_counts": _sec_counts,
                    })
                except Exception:
                    pass
                memory_user_text = item.prompt
                if item.source.lower() in {"document", "photo", "voice", "audio", "video", "sticker"}:
                    memory_user_text = f"[{item.source}] {item.summary}"
                if item.source not in {"startup", "system"}:
                    self.memory_store.record_turn("user", item.source, memory_user_text)
                    self.memory_store.record_turn("assistant", self.config.active_backend, display_text or response.text)
                    self.memory_store.record_exchange(memory_user_text, display_text or response.text, item.source)
                self.handoff_builder.append_transcript("user", item.prompt, item.source)
                self.handoff_builder.append_transcript("assistant", display_text or response.text)
                self.handoff_builder.refresh_recent_context()
                self.project_chat_logger.log_exchange(item.prompt, display_text or response.text, item.source)
                _print_final_response(self.name, display_text or response.text)
                total_s = (datetime.now() - datetime.fromisoformat(item.created_at)).total_seconds()
                send_elapsed_s, chunk_count = await self.send_long_message(
                    chat_id=item.chat_id,
                    text=display_text or response.text,
                    request_id=item.request_id,
                    purpose="bg-response",
                )
                await self._send_voice_reply(item.chat_id, display_text or response.text, item.request_id)
                self.logger.info(
                    f"Background task {item.request_id} delivered "
                    f"(total_s={total_s:.2f}, chunks={chunk_count}, send_s={send_elapsed_s:.2f})"
                )
            else:
                err_msg = response.error or "Unknown error"
                self._mark_error(err_msg)
                self._record_habit_outcome(item, success=False, error_text=err_msg)
                if self._should_buffer_during_transfer(item.request_id):
                    self._record_suppressed_transfer_result(item, success=False, error=err_msg)
                    return
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
            self._record_habit_outcome(item, success=False, error_text=str(e))
            self.error_logger.exception(
                f"Unhandled error in _on_background_complete for {item.request_id}: {e}"
            )

    async def process_queue(self):
        self.logger.info("Flex queue processor started.")
        while True:
            item = None
            try:
                item = await self.queue.get()
                if not item.prompt or not item.prompt.strip():
                    self.logger.debug(f"Skipping empty prompt in queue (source={item.source}, id={item.request_id})")
                    continue
                if not item.silent:
                    self.last_prompt = item
                is_bridge_request = item.source.startswith("bridge:") or item.source.startswith("bridge-transfer:")
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
                remote_backend_block = self._remote_backend_block_reason(item.source)
                if remote_backend_block:
                    self.error_logger.warning(remote_backend_block)
                    if item.deliver_to_telegram:
                        await self.send_long_message(
                            item.chat_id,
                            f"⚠️ {remote_backend_block}",
                            request_id=item.request_id,
                            purpose="remote-backend-policy",
                        )
                    continue
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
                habit_sections, habit_ids = self._build_habit_sections(item, effective_prompt)
                extra_sections = self._workzone_prompt_section() + habit_sections
                self.current_request_meta["habit_ids"] = habit_ids
                # In fixed mode with an active session, use incremental prompts
                _incremental = (
                    self.backend_manager.agent_mode == "fixed"
                    and hasattr(self.backend_manager.current_backend, "_session_id")
                    and self.backend_manager.current_backend._session_id is not None
                )
                _prompt_payload = self.context_assembler.build_prompt_payload(
                    effective_prompt,
                    self.config.active_backend,
                    extra_sections=extra_sections,
                    inject_memory=not item.skip_memory_injection,
                    incremental=_incremental,
                )
                final_prompt = _prompt_payload["final_prompt"]
                self._last_prompt_audit = _prompt_payload.get("audit", {})
                self._thinking_chars_this_req = 0
                self._last_full_prompt_tokens = len(final_prompt) // 4
                
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
                    self._record_habit_outcome(item, success=False, error_text=err_msg)
                    if self._should_buffer_during_transfer(item.request_id):
                        self._record_suppressed_transfer_result(item, success=False, error=err_msg)
                    if not item.silent:
                        if not self._should_buffer_during_transfer(item.request_id):
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
                    display_text = self._strip_transfer_accept_prefix(item, response.text)
                    self._mark_success()
                    self._record_habit_outcome(item, success=True, response_text=response.text)
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
                    try:
                        from tools.token_tracker import estimate_tokens, record_audit_event, record_usage
                        import hashlib as _hashlib
                        if response.usage:
                            _input_tok = response.usage.input_tokens
                            _output_tok = response.usage.output_tokens
                            _thinking_tok = response.usage.thinking_tokens
                            _tok_source = "api"
                            record_usage(
                                self.workspace_dir,
                                model=self.get_current_model(),
                                backend=self.config.active_backend,
                                input_tokens=_input_tok,
                                output_tokens=_output_tok,
                                thinking_tokens=_thinking_tok,
                                session_id=self.session_id_dt,
                                cost_usd=getattr(response, "cost_usd", None),
                            )
                        else:
                            # CLI backend: estimate from final assembled prompt
                            _input_tok = estimate_tokens(final_prompt)
                            _output_tok = estimate_tokens(display_text or response.text)
                            _thinking_tok = self._thinking_chars_this_req // 4
                            _tok_source = "estimated"
                            record_usage(
                                self.workspace_dir,
                                model=self.get_current_model(),
                                backend=self.config.active_backend,
                                input_tokens=_input_tok,
                                output_tokens=_output_tok,
                                thinking_tokens=_thinking_tok,
                                session_id=self.session_id_dt,
                            )
                        _pa = self._last_prompt_audit
                        _sec_chars = {s["key"]: s["chars"] for s in _pa.get("sections", [])}
                        _sec_tokens = {s["key"]: s.get("tokens_est") or max(1, s["chars"] // 4) for s in _pa.get("sections", [])}
                        _sec_counts = {s["key"]: s.get("item_count", 0) for s in _pa.get("sections", [])}
                        record_audit_event(self.workspace_dir, {
                            "request_id": item.request_id,
                            "agent": self.name,
                            "runtime": "flex",
                            "backend": self.config.active_backend,
                            "model": self.get_current_model(),
                            "source": item.source,
                            "summary": item.summary,
                            "silent": item.silent,
                            "is_retry": item.is_retry,
                            "success": response.is_success,
                            "incremental_mode": _incremental,
                            "token_source": _tok_source,
                            "raw_prompt_chars": len(item.prompt),
                            "effective_prompt_chars": len(effective_prompt),
                            "final_prompt_chars": len(final_prompt),
                            "response_chars": len(response.text or ""),
                            "input_tokens": _input_tok,
                            "output_tokens": _output_tok,
                            "thinking_tokens": _thinking_tok,
                            "tool_call_count": int(getattr(response, "tool_call_count", 0) or 0),
                            "tool_loop_count": int(getattr(response, "tool_loop_count", 0) or 0),
                            "tool_catalog_count": 0,
                            "tool_schema_chars": 0,
                            "tool_schema_tokens_est": 0,
                            "tool_schema_fingerprint": "",
                            "tool_max_loops": 0,
                            "budget_applied": bool(_pa.get("budget_applied")),
                            "budget_limit_chars": _pa.get("budget_limit_chars"),
                            "context_chars_before_budget": _pa.get("context_chars_before_budget", 0),
                            "time_fyi_chars": _pa.get("time_fyi_chars", 0),
                            "context_expansion_ratio": round(len(final_prompt) / max(len(item.prompt), 1), 3),
                            "context_fingerprint": _pa.get("context_fingerprint", ""),
                            "request_fingerprint": _hashlib.sha1((item.prompt or "").encode("utf-8")).hexdigest()[:16],
                            "section_chars": _sec_chars,
                            "section_tokens_est": _sec_tokens,
                            "section_counts": _sec_counts,
                        })
                    except Exception:
                        pass
                    if not item.silent:
                        if self._should_buffer_during_transfer(item.request_id):
                            self._record_suppressed_transfer_result(item, success=True, text=display_text or response.text)
                            continue
                        self.last_response = {
                            "chat_id": item.chat_id,
                            "text": display_text or response.text,
                            "request_id": item.request_id,
                            "responded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        }
                        memory_user_text = item.prompt
                        if item.source.lower() in {"document", "photo", "voice", "audio", "video", "sticker"}:
                            memory_user_text = f"[{item.source}] {item.summary}"
                        if item.source not in {"startup", "system"} and not is_bridge_request:
                            self.memory_store.record_turn("user", item.source, memory_user_text)
                            self.memory_store.record_turn("assistant", self.config.active_backend, display_text or response.text)
                            self.memory_store.record_exchange(memory_user_text, display_text or response.text, item.source)
                        if not is_bridge_request:
                            self.handoff_builder.append_transcript("user", item.prompt, item.source)
                            self.handoff_builder.append_transcript("assistant", display_text or response.text)
                            self.handoff_builder.refresh_recent_context()
                            self.project_chat_logger.log_exchange(item.prompt, display_text or response.text, item.source)
                        if not item.deliver_to_telegram:
                            continue
                        # CoS intercept: if /cos on and response ends with ?, route to Lily first
                        _response_text = display_text or response.text
                        _cos_handled = False
                        if (
                            self._cos_enabled
                            and self.name != "lily"
                            and not item.source.startswith("cos-query:")
                            and _response_text
                            and _response_text.rstrip().endswith(("?", "？"))
                        ):
                            cos_result = await self.cos_query(_response_text)
                            if cos_result.get("answered") and cos_result.get("response"):
                                # Lily answered — deliver Lily's response instead
                                _response_text = cos_result["response"]
                            else:
                                # Declined/timeout: deliver original to user, skip hchat re-routing
                                _cos_handled = True
                        _print_final_response(self.name, _response_text)
                        send_elapsed_s, chunk_count = await self.send_long_message(
                            chat_id=item.chat_id,
                            text=_response_text,
                            request_id=item.request_id,
                            purpose="response",
                        )
                        await self._send_voice_reply(item.chat_id, _response_text, item.request_id)
                        total_elapsed_s = (datetime.now() - queued_at).total_seconds()
                        self.logger.info(
                            f"Completed {item.request_id} delivery via {self.config.active_backend} "
                            f"(queue_wait_s={queue_wait_s:.2f}, backend_s={backend_elapsed:.2f}, "
                            f"telegram_send_s={send_elapsed_s:.2f}, total_s={total_elapsed_s:.2f}, "
                            f"chunks={chunk_count})"
                        )
                        self._log_maintenance(item, "send_success", text_len=len(_response_text or ""))
                        # Route hchat reply back to sender if applicable (skip if CoS already delivered direct)
                        if not _cos_handled:
                            await self._hchat_route_reply(item, _response_text)
                else:
                    err_msg = response.error or "Unknown error"
                    self._mark_error(err_msg)
                    self._record_habit_outcome(item, success=False, error_text=err_msg)
                    if self._should_buffer_during_transfer(item.request_id):
                        self._record_suppressed_transfer_result(item, success=False, error=err_msg)
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
                        if self._should_buffer_during_transfer(item.request_id):
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
                if item is not None:
                    self._record_habit_outcome(item, success=False, error_text=str(e))
                self.error_logger.exception(f"Error in flex queue processing: {e}")
                self.is_generating = False
            finally:
                self.current_request_meta = None
                if item is not None:
                    self.queue.task_done()

    def get_bot_commands(self) -> list[BotCommand]:
        commands = [
            BotCommand("help", "Show help menu"),
            BotCommand("start", "Start another stopped agent"),
            BotCommand("agents", "List all agents with controls; add <id> <name> [token]"),
            BotCommand("status", "View agent status"),
            BotCommand("voice", "Toggle native voice replies"),
            BotCommand("safevoice", "Toggle voice confirmation safety layer"),
            BotCommand("whisper", "Set Whisper model size [small|medium|large]"),
            BotCommand("active", "Toggle proactive heartbeat"),
            BotCommand("fyi", "Refresh bridge environment awareness"),
            BotCommand("debug", "Run in strict debug mode"),
            BotCommand("skill", "Browse and run skills"),
            BotCommand("backend", "Show backend buttons (+ means context)"),
            BotCommand("handoff", "Fresh session with recent continuity"),
            BotCommand("ticket", "Submit IT support ticket to Arale"),
            BotCommand("park", "List or save parked topics"),
            BotCommand("load", "Restore a parked topic"),
            BotCommand("transfer", "Transfer this session to another agent"),
            BotCommand("fork", "Fork this session to another agent"),
            BotCommand("cos", "Chief of Staff decision routing (on/off)"),
            BotCommand("long", "Start multi-message input (end with /end)"),
            BotCommand("end", "Submit collected /long input"),
            BotCommand("mode", "Switch fixed/flex/wrapper mode"),
            BotCommand("wrapper", "Configure wrapper persona slots"),
            BotCommand("core", "Configure wrapper core model"),
            BotCommand("wrap", "Configure wrapper translator model"),
            BotCommand("workzone", "Set temporary working directory [path|off]"),
            BotCommand("model", "View or change model"),
            BotCommand("effort", "View or change effort"),
            BotCommand("new", "Start a fresh CLI session"),
            BotCommand("fresh", "Start a clean API context"),
            BotCommand("memory", "Control memory injection"),
            BotCommand("clear", "Clear media/history"),
            BotCommand("stop", "Stop execution"),
            BotCommand("reboot", "Hot restart agents"),
            BotCommand("terminate", "Shut down this agent"),
            BotCommand("retry", "Resend response or rerun prompt"),
            BotCommand("verbose", "Toggle verbose long-task status [on|off]"),
            BotCommand("think", "Toggle thinking trace display [on|off]"),
            BotCommand("loop", "Create/manage recurring loop tasks"),
            BotCommand("jobs", "Show cron and heartbeat jobs"),
            BotCommand("cron", "Run or list cron jobs"),
            BotCommand("heartbeat", "Run or list heartbeat jobs"),
            BotCommand("timeout", "View or set request timeout [minutes]"),
            BotCommand("hchat", "Send a message to another agent [agent] [message]"),
            BotCommand("logo", "Play startup animation"),
            BotCommand("remote", "Start/stop Hashi Remote [on|off|status]"),
            BotCommand("oll", "Start/stop OLL Browser Gateway [on|off|status]"),
            BotCommand("wa_on", "Start WhatsApp transport"),
            BotCommand("wa_off", "Stop WhatsApp transport"),
            BotCommand("wa_send", "Send a WhatsApp message"),
            BotCommand("usecomputer", "Enable or run GUI-aware computer-use mode"),
            BotCommand("sys", "Manage system prompt slots"),
            BotCommand("credit", "Check API credit/usage"),
        ]
        if private_wol_available(self.global_config.project_root):
            commands.append(BotCommand("wol", "Send Wake-on-LAN magic packet [pc_name]"))
        return commands

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
