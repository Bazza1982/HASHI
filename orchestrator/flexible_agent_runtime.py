from __future__ import annotations
import html
import re
import sys
import time
import asyncio
import inspect
import logging
import shlex
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
from telegram.ext import ApplicationBuilder

from orchestrator.config import FlexibleAgentConfig, GlobalConfig
from orchestrator.agent_fyi import build_agent_fyi_primer
from orchestrator.bridge_memory import BridgeMemoryStore, BridgeContextAssembler, SysPromptManager
from orchestrator.flexible_backend_manager import FlexibleBackendManager
from orchestrator.command_registry import runtime_command_map
from orchestrator.extension_command_registry import available_workspace_commands, execute_workspace_command
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
from orchestrator.habits import HabitStore
from orchestrator.media_utils import is_image_file, normalize_image_file
from orchestrator.parked_topics import ParkedTopicStore
from orchestrator import runtime_status
from orchestrator import runtime_transfer
from orchestrator import runtime_mode
from orchestrator import runtime_delivery
from orchestrator import runtime_remote
from orchestrator import runtime_reboot
from orchestrator import runtime_pipeline
from orchestrator.runtime_common import (
    QueuedRequest,
    _md_to_html,
    _print_final_response,
    _print_thinking,
    _print_user_message,
    _safe_excerpt,
    resolve_authorized_telegram_ids,
)
from orchestrator.post_turn_observer import PostTurnObserver, PreTurnContextProvider, TurnContextRequest, TurnObservationRequest
from orchestrator.usecomputer_mode import (
    build_usecomputer_task_prompt,
    get_usecomputer_examples_text,
    get_usecomputer_status,
    set_usecomputer_mode,
)
from orchestrator.skill_manager import SkillDefinition, SkillManager
from orchestrator import runtime_command_binding
from orchestrator.voice_manager import VoiceManager

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
        self._post_turn_observers: list[PostTurnObserver] = []
        self._pre_turn_context_providers: list[PreTurnContextProvider] = []

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

        # help/status/new/wipe/clear/model/effort/mode should always be available
        self._enabled_commands.update({"help", "status", "new", "fresh", "wipe", "reset", "clear", "memory", "model", "effort", "mode", "jobs", "verbose", "think", "voice", "whisper", "transfer", "fork", "cos", "long", "end"})

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

    async def handle_workspace_command(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        self._record_active_chat(update)
        raw = (getattr(update.message, "text", "") or "").strip()
        if not raw.startswith("/"):
            return
        try:
            parts = shlex.split(raw[1:])
        except Exception:
            parts = raw[1:].split()
        if not parts:
            return
        command_name = parts[0].split("@", 1)[0].lower()
        args = parts[1:]
        if not self._is_command_allowed(command_name):
            await self._reply_text(update, f"/{command_name} is disabled for this agent.")
            return
        registry_command = runtime_command_map().get(command_name)
        if registry_command is not None:
            try:
                await registry_command.callback(self, update, context)
            except Exception as exc:
                self.error_logger.exception("Runtime command failed: %s", command_name)
                await self._reply_text(update, f"/{command_name} failed: {type(exc).__name__}: {exc}")
            return
        try:
            text = await execute_workspace_command(self, command_name, args, update=update, context=context)
        except KeyError:
            return
        except Exception as exc:
            self.error_logger.exception("Workspace command failed: %s", command_name)
            await self._reply_text(update, f"/{command_name} failed: {type(exc).__name__}: {exc}")
            return
        await self.send_long_message(
            update.effective_chat.id,
            text or f"/{command_name} returned no output.",
            request_id=f"workspace-command-{command_name}",
            purpose="command",
        )

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
        return await runtime_delivery.reply_text(self, update, text, **kwargs)

    async def _send_text(self, chat_id: int, text: str, **kwargs):
        return await runtime_delivery.send_text(self, chat_id, text, **kwargs)

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

    def reload_post_turn_observers(self) -> None:
        from orchestrator.post_turn_registry import build_post_turn_observers

        try:
            self._post_turn_observers = build_post_turn_observers(
                workspace_dir=self.workspace_dir,
                bridge_memory_store=self.memory_store,
                backend_invoker=getattr(self.backend_manager, "generate_ephemeral_response", None),
                backend_context_getter=self._current_backend_context,
            )
            self._pre_turn_context_providers = [
                observer for observer in self._post_turn_observers
                if isinstance(observer, PreTurnContextProvider)
            ]
            self.logger.info(
                "Turn observers initialized: post_count=%s pre_count=%s",
                len(self._post_turn_observers),
                len(self._pre_turn_context_providers),
            )
        except Exception as exc:
            self._post_turn_observers = []
            self._pre_turn_context_providers = []
            self.logger.warning("Failed to initialize post-turn observers: %s", exc)

    async def _build_pre_turn_context_sections(
        self,
        item: QueuedRequest,
        user_text: str,
        *,
        is_bridge_request: bool,
    ) -> list[tuple[str, str]]:
        if not self._pre_turn_context_providers:
            return []
        request = TurnContextRequest(
            request_id=item.request_id,
            source=item.source,
            user_text=user_text,
            model_name=self.get_current_model(),
            chat_id=item.chat_id,
            summary=item.summary,
            metadata={},
        )
        sections: list[tuple[str, str]] = []
        for provider in self._pre_turn_context_providers:
            try:
                if not provider.should_provide(item.source, is_bridge_request=is_bridge_request):
                    continue
                provider_sections = await provider.build_context_sections(request)
                sections.extend(provider_sections)
            except Exception as exc:
                self.logger.warning(
                    "Pre-turn context provider failed for %s via %s: %s",
                    item.request_id,
                    type(provider).__name__,
                    exc,
                )
        return sections

    def _schedule_post_turn_observers(
        self,
        item: QueuedRequest,
        user_text: str,
        assistant_text: str,
        *,
        is_bridge_request: bool,
    ) -> None:
        if not self._post_turn_observers:
            return
        request = TurnObservationRequest(
            request_id=item.request_id,
            source=item.source,
            user_text=user_text,
            assistant_text=assistant_text,
            model_name=self.get_current_model(),
            chat_id=item.chat_id,
            summary=item.summary,
            metadata={},
        )
        for observer in self._post_turn_observers:
            try:
                if observer.should_observe(item.source, is_bridge_request=is_bridge_request):
                    observer.schedule_observation(request, self._background_tasks)
            except Exception as exc:
                self.logger.warning(
                    "Post-turn observer failed to schedule for %s via %s: %s",
                    item.request_id,
                    type(observer).__name__,
                    exc,
                )

    def _observer_workspace_keep_names(self) -> set[str]:
        keep_names: set[str] = set()
        for observer in self._post_turn_observers:
            try:
                keep_names.update(str(name) for name in observer.workspace_files_to_preserve())
            except Exception as exc:
                self.logger.warning(
                    "Post-turn observer failed to report preserved files via %s: %s",
                    type(observer).__name__,
                    exc,
                )
        return keep_names

    def _current_backend_context(self) -> dict[str, str] | None:
        current_backend = getattr(self.backend_manager, "current_backend", None)
        if current_backend is None:
            return None
        config = getattr(current_backend, "config", None)
        engine = str(getattr(config, "engine", "") or "").strip()
        model = str(getattr(config, "model", "") or "").strip()
        if not engine or not model:
            return None
        return {"engine": engine, "model": model}

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
        return runtime_transfer.persist_transfer_state(self)

    def _clear_transfer_state(self) -> None:
        return runtime_transfer.clear_transfer_state(self)

    def has_active_transfer(self) -> bool:
        return runtime_transfer.has_active_transfer(self)

    def _transfer_redirect_text(self) -> str:
        return runtime_transfer.transfer_redirect_text(self)

    def _should_redirect_after_transfer(self) -> bool:
        return runtime_transfer.should_redirect_after_transfer(self)

    def _should_buffer_during_transfer(self, request_id: str | None) -> bool:
        return runtime_transfer.should_buffer_during_transfer(self, request_id)

    def _record_suppressed_transfer_result(
        self,
        item: QueuedRequest,
        *,
        success: bool,
        text: str | None = None,
        error: str | None = None,
    ) -> None:
        return runtime_transfer.record_suppressed_transfer_result(
            self,
            item,
            success=success,
            text=text,
            error=error,
        )

    async def _flush_suppressed_transfer_results(self) -> None:
        return await runtime_transfer.flush_suppressed_transfer_results(self)

    def _strip_transfer_accept_prefix(self, item: QueuedRequest, text: str) -> str:
        return runtime_transfer.strip_transfer_accept_prefix(item, text)

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
        runtime_command_binding.bind_flexible_runtime_handlers(self)

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

    def _resolve_bridge_handoff_endpoint(self, target_instance: str, mode: str) -> tuple[str, str]:
        return runtime_transfer.resolve_bridge_handoff_endpoint(self, target_instance, mode)
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
        return runtime_transfer.build_handoff_payload(self, target_agent, target_instance, mode)
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

    # ── /loop — recurring task management ──────────────────────────

    _LOOP_FREQ_PATTERNS: list[tuple] = []  # populated once at class level below

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

    # ------------------------------------------------------------------
    # /remote — one-click Hashi Remote start/stop
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # /long ... /end buffering (collect split Telegram messages)
    # ------------------------------------------------------------------

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
        return await runtime_delivery.send_long_message(
            self,
            chat_id=chat_id,
            text=text,
            request_id=request_id,
            purpose=purpose,
        )

    async def typing_loop(self, chat_id: int, stop_event: asyncio.Event):
        return await runtime_delivery.typing_loop(self, chat_id, stop_event)

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
            await runtime_pipeline.finalize_background_task(self, task, item)
        except Exception as e:
            self._mark_error(str(e))
            self._record_habit_outcome(item, success=False, error_text=str(e))
            self.error_logger.exception(
                f"Unhandled error in _on_background_complete for {item.request_id}: {e}"
            )

    async def process_queue(self):
        await runtime_pipeline.run_queue_loop(self)

    def get_bot_commands(self):
        commands = runtime_command_binding.get_flexible_bot_commands(self)
        registered = {command.command for command in commands}
        for spec in available_workspace_commands(self.workspace_dir):
            if spec.name in registered or not self._is_command_allowed(spec.name):
                continue
            commands.append(BotCommand(spec.name, spec.description or "Workspace command"))
            registered.add(spec.name)
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
