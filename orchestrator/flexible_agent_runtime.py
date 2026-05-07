from __future__ import annotations
import html
import re
import sys
import time
import asyncio
import inspect
import logging
import sqlite3
from uuid import uuid4
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Optional, Any, Mapping
import json

import aiohttp
import yaml
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, constants
from telegram.error import TimedOut as TelegramTimedOut
from telegram.ext import ApplicationBuilder

from orchestrator.config import FlexibleAgentConfig, GlobalConfig
from orchestrator import runtime_audit
from orchestrator.browser_mode import (
    build_browser_task_prompt,
    get_browser_examples_text,
    get_browser_menu_text,
    get_browser_status_text,
)
from orchestrator import runtime_control
from orchestrator import runtime_delivery
from orchestrator import runtime_habits
from orchestrator import runtime_lifecycle
from orchestrator import runtime_media
from orchestrator import runtime_command_binding
from orchestrator import runtime_mode
from orchestrator import runtime_pipeline
from orchestrator import runtime_remote
from orchestrator import runtime_session
from orchestrator import runtime_status
from orchestrator import runtime_transfer
from orchestrator import runtime_workspace
from orchestrator import runtime_wrapper
from orchestrator import runtime_workzone
from orchestrator.runtime_common import (
    QueuedRequest,
    _print_final_response,
    _print_thinking,
    _print_user_message,
    _safe_excerpt,
    resolve_authorized_telegram_ids,
)
from orchestrator.agent_fyi import build_agent_fyi_primer
from orchestrator.bridge_memory import BridgeMemoryStore, BridgeContextAssembler, SysPromptManager
from orchestrator.ephemeral_invoker import make_backend_sidecar_invoker
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
from orchestrator.habits import HabitStore
from orchestrator.media_utils import is_image_file, normalize_image_file
from orchestrator.parked_topics import ParkedTopicStore
from orchestrator.post_turn_observer import (
    PostTurnObserver,
    PreTurnContextProvider,
)
from orchestrator import runtime_observers
from orchestrator.usecomputer_mode import (
    build_usecomputer_task_prompt,
    get_usecomputer_examples_text,
    get_usecomputer_status,
    set_usecomputer_mode,
)
from orchestrator.skill_manager import SkillDefinition, SkillManager
from orchestrator.voice_manager import VoiceManager
from orchestrator.private_wol import describe_wol_targets, private_wol_available, run_private_wol
from orchestrator.workzone import load_workzone
from orchestrator.wrapper_mode import SESSION_RESET_SOURCE, load_wrapper_config, visible_wrapper_slots
from orchestrator.audit_mode import (
    AuditTelemetryCollector,
    visible_audit_criteria,
    load_audit_config,
    should_audit_source,
)

HABIT_BROWSER_PAGE_SIZE = 5
MAX_JOB_TRANSFER_SELECTIONS = 256

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
        self.core_transcript_log_path = self.workspace_dir / "core_transcript.jsonl"
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
        self._sidecar_invoker, self._sidecar_context_getter = make_backend_sidecar_invoker(self.backend_manager)
        self._post_turn_observers: list[PostTurnObserver] = []
        self._pre_turn_context_providers: list[PreTurnContextProvider] = []
        self.reload_post_turn_observers()

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
        return runtime_habits.build_habit_sections(self, item, prompt)

    def _record_habit_outcome(
        self,
        item: QueuedRequest,
        *,
        success: bool,
        response_text: str | None = None,
        error_text: str | None = None,
    ) -> None:
        runtime_habits.record_habit_outcome(
            self,
            item,
            success=success,
            response_text=response_text,
            error_text=error_text,
        )

    def _capture_followup_habit_feedback(self, text: str) -> None:
        runtime_habits.capture_followup_habit_feedback(self, text)

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
        self._enabled_commands.update({"help", "status", "new", "fresh", "wipe", "reset", "clear", "memory", "model", "effort", "mode", "wrapper", "audit", "core", "wrap", "jobs", "verbose", "think", "voice", "whisper", "transfer", "fork", "cos", "long", "end", "oll", "browser"})

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
        return await runtime_lifecycle.initialize(self)

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
        runtime_workzone.sync_workzone_to_backend_config(self)

    def _workzone_prompt_section(self) -> list[tuple[str, str]]:
        return runtime_workzone.workzone_prompt_section(self)

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
        runtime_observers.reload_post_turn_observers(self)

    async def _build_pre_turn_context_sections(
        self,
        item: QueuedRequest,
        user_text: str,
        *,
        is_bridge_request: bool,
    ) -> list[tuple[str, str]]:
        return await runtime_observers.build_pre_turn_context_sections(
            self,
            item,
            user_text,
            is_bridge_request=is_bridge_request,
        )

    def _schedule_post_turn_observers(
        self,
        item: QueuedRequest,
        user_text: str,
        assistant_text: str,
        *,
        is_bridge_request: bool,
    ) -> None:
        runtime_observers.schedule_post_turn_observers(
            self,
            item,
            user_text,
            assistant_text,
            is_bridge_request=is_bridge_request,
        )

    def _observer_workspace_keep_names(self) -> set[str]:
        return runtime_observers.observer_workspace_keep_names(self)

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
        return runtime_status.compute_status_string(self)

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
        runtime_transfer.persist_transfer_state(self)

    def _clear_transfer_state(self) -> None:
        runtime_transfer.clear_transfer_state(self)

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
        runtime_transfer.record_suppressed_transfer_result(
            self,
            item,
            success=success,
            text=text,
            error=error,
        )

    async def _flush_suppressed_transfer_results(self) -> None:
        await runtime_transfer.flush_suppressed_transfer_results(self)

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
        return runtime_status.job_counts(self)

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

    def _format_status_mode_block(self, mode: str, state: Mapping[str, Any], detailed: bool) -> list[str]:
        return runtime_status.format_status_mode_block(mode, state, detailed)

    def _build_status_text(self, detailed: bool = False) -> str:
        return runtime_status.build_status_text(self, detailed=detailed)

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
        return runtime_habits.habit_db_path(self)

    def _load_local_habit_counts(self) -> dict[str, int]:
        return runtime_habits.load_local_habit_counts(self)

    def _load_local_habit_rows(
        self,
        *,
        offset: int = 0,
        limit: int = HABIT_BROWSER_PAGE_SIZE,
    ) -> tuple[int, list[sqlite3.Row]]:
        return runtime_habits.load_local_habit_rows(self, offset=offset, limit=limit)

    def _habit_status_button_label(self, current: str, target: str) -> str:
        return runtime_habits.habit_status_button_label(current, target)

    def _build_habit_browser_view(
        self,
        *,
        offset: int = 0,
        selected_habit_id: str | None = None,
        notice: str | None = None,
    ) -> tuple[str, InlineKeyboardMarkup]:
        return runtime_habits.build_habit_browser_view(
            self,
            offset=offset,
            selected_habit_id=selected_habit_id,
            notice=notice,
        )

    def _set_local_habit_status(self, habit_id: str, target_status: str) -> tuple[bool, str]:
        return runtime_habits.set_local_habit_status(self, habit_id, target_status)

    def _build_habit_governance_view(self) -> str:
        return runtime_habits.build_habit_governance_view(self)

    async def _render_skill_jobs(self, update_or_query, kind: str):
        from orchestrator.runtime_jobs import _build_jobs_with_buttons
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
        return runtime_media.build_media_prompt(media_kind, filename, caption=caption, emoji=emoji)

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
            await runtime_mode.callback_mode_toggle(self, query, value)

        elif target == "retry":
            await runtime_control.callback_retry_toggle(self, query, value)

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
        return runtime_remote.load_instances()

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
        await runtime_remote.move_show_agent_picker(self, update, instances)

    async def _move_show_target_picker(self, update: Update, agent_id: str, instances: dict):
        await runtime_remote.move_show_target_picker(self, update, agent_id, instances)

    async def _move_show_options(self, update, agent_id: str, target: str):
        await runtime_remote.move_show_options(self, update, agent_id, target)

    async def _do_move(self, update, agent_id: str, target: str, instances: dict,
                       keep_source: bool = False, sync: bool = False, dry_run: bool = False):
        await runtime_remote.do_move(
            self,
            update,
            agent_id,
            target,
            instances,
            keep_source=keep_source,
            sync=sync,
            dry_run=dry_run,
        )

    async def callback_move(self, update: Update, context: Any):
        await runtime_remote.handle_move_callback(self, update, context)

    def _resolve_bridge_handoff_endpoint(self, target_instance: str, mode: str) -> tuple[str, str]:
        return runtime_transfer.resolve_bridge_handoff_endpoint(self, target_instance, mode)

    def _build_handoff_payload(self, target_agent: str, target_instance: str, mode: str) -> dict[str, Any]:
        return runtime_transfer.build_handoff_payload(self, target_agent, target_instance, mode)

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

    async def cmd_browser(self, update, context):
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = [a.strip() for a in (context.args or []) if a.strip()]
        if not args:
            await self._reply_text(update, get_browser_menu_text())
            return

        sub = args[0].lower()
        if sub == "status":
            secrets = getattr(self.backend_manager, "secrets", {}) or {}
            active_backend = getattr(self.config, "active_backend", None)
            extension_bridge_configured = Path("/tmp/hashi-browser-bridge.sock").exists()
            await self._reply_text(
                update,
                get_browser_status_text(
                    active_backend=active_backend,
                    brave_configured=bool(secrets.get("brave_api_key")),
                    extension_bridge_configured=extension_bridge_configured,
                ),
            )
            return
        if sub == "examples":
            await self._reply_text(update, get_browser_examples_text())
            return

        task = " ".join(args[1:]).strip()
        try:
            prompt, source, summary = build_browser_task_prompt(sub, task)
        except ValueError:
            await self._reply_text(update, get_browser_menu_text())
            return

        await self._reply_text(update, f"Running in /browser route {sub}...")
        await self.enqueue_request(update.effective_chat.id, prompt, source, summary)

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
            if await runtime_habits.handle_habit_callback(self, query, data):
                return
        if data.startswith("skilljob:"):
            from orchestrator import runtime_jobs
            if await runtime_jobs.handle_skill_job_callback(self, query, data):
                return
        if data.startswith("skill:"):
            from orchestrator import runtime_skill_callbacks
            if await runtime_skill_callbacks.handle_skill_callback(self, query, data):
                return
        await query.answer()

    def _build_job_transfer_keyboard(self, kind: str, task_id: str):
        from orchestrator import runtime_jobs
        return runtime_jobs.build_job_transfer_keyboard(self, kind, task_id)

    def _job_transfer_callback(self, kind: str, task_id: str, target_agent: str, *, instance_id: str | None = None) -> str:
        from orchestrator import runtime_jobs
        return runtime_jobs.job_transfer_callback(
            self,
            kind,
            task_id,
            target_agent,
            instance_id=instance_id,
            max_selections=MAX_JOB_TRANSFER_SELECTIONS,
        )

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
            from orchestrator.runtime_jobs import _build_jobs_with_buttons
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
        await runtime_status.cmd_status(self, update, context)

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
        from orchestrator.runtime_jobs import _build_jobs_with_buttons
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
        elif self._hchat_draft_delivery_enabled():
            self_prompt = self._build_hchat_draft_prompt(target_name, intent)
            await self._reply_text(update, f"💬 Drafting Hchat message to <b>{target_name}</b>...", parse_mode="HTML")
            await self.enqueue_api_text(
                self_prompt,
                source="bridge:hchat-draft",
                deliver_to_telegram=True,
            )
            return

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

    def _hchat_draft_delivery_enabled(self) -> bool:
        extra = self.config.extra if isinstance(getattr(self.config, "extra", None), dict) else {}
        value = extra.get("hchat_draft_delivery")
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _build_hchat_draft_prompt(self, target_name: str, intent: str) -> str:
        return (
            f"[HCHAT DRAFT TASK] The user wants you to draft a Hchat message to agent \"{target_name}\".\n\n"
            f"Intent: {intent}\n\n"
            f"Return ONLY a JSON object with this exact shape:\n"
            f'{{"target": "{target_name}", "message": "<complete message to send>", '
            f'"user_report": "<short report for the user after delivery>"}}\n\n'
            f"Rules:\n"
            f"- Do not run shell commands.\n"
            f"- Do not mention delivery tools or implementation details.\n"
            f"- Do not wrap the JSON in prose.\n"
            f"- Compose the message FROM you ({self.name}) TO {target_name}.\n"
            f"- Do not relay the user's words literally; include relevant context and be concise.\n"
            f"- The runtime will validate the JSON and send the message."
        )

    async def _prepare_hchat_draft_success(self, item: QueuedRequest, *, core_raw: str, completion_path: str):
        from orchestrator.hchat_delivery import (
            HChatDraftParseError,
            deliver_hchat_draft,
            draft_parse_error_text,
            hchat_delivery_log_fields,
            hchat_draft_parsed_log_fields,
            parse_hchat_draft,
        )
        from orchestrator.wrapper_mode import passthrough_result

        wrapper_result = passthrough_result(core_raw or "", fallback_reason="hchat_draft_delivery")
        try:
            draft = parse_hchat_draft(core_raw or "")
        except HChatDraftParseError as exc:
            visible_text = draft_parse_error_text(exc)
            self._mark_error(visible_text)
            self._record_habit_outcome(item, success=False, error_text=visible_text)
            self._append_core_transcript(
                item,
                core_raw=core_raw,
                visible_text=visible_text,
                completion_path=completion_path,
                wrapper_result=wrapper_result,
            )
            await self._notify_request_listeners(
                item.request_id,
                {
                    "request_id": item.request_id,
                    "success": False,
                    "text": visible_text,
                    "error": visible_text,
                    "source": item.source,
                    "summary": item.summary,
                },
            )
            self.logger.warning("HChat draft parse failed for %s: %s", item.request_id, visible_text)
            return runtime_pipeline.SuccessfulResponse(
                display_text=core_raw or "",
                visible_text=visible_text,
                wrapper_result=wrapper_result,
            )

        sender = getattr(self, "_hchat_draft_sender", None)
        result = deliver_hchat_draft(draft, from_agent=self.name, sender=sender)
        visible_text = (
            draft.user_report
            if result.success and draft.user_report
            else f"Message delivered to {result.target}."
            if result.success
            else f"[hchat] Delivery failed to {result.target}: {result.error or 'unknown error'}"
        )
        if result.success:
            self._mark_success()
            self._record_habit_outcome(item, success=True, response_text=visible_text)
        else:
            self._mark_error(visible_text)
            self._record_habit_outcome(item, success=False, error_text=visible_text)
        parsed_fields = hchat_draft_parsed_log_fields(draft)
        delivery_fields = hchat_delivery_log_fields(result)
        self._append_core_transcript(
            item,
            core_raw=core_raw,
            visible_text=visible_text,
            completion_path=completion_path,
            wrapper_result=wrapper_result,
        )
        await self._notify_request_listeners(
            item.request_id,
            {
                "request_id": item.request_id,
                "success": result.success,
                "text": visible_text,
                "error": None if result.success else visible_text,
                "source": item.source,
                "summary": item.summary,
                **parsed_fields,
                **delivery_fields,
            },
        )
        self.logger.info(
            "HChat draft delivery %s for %s target=%s attempt_id=%s",
            result.delivery_status,
            item.request_id,
            result.target,
            result.attempt_id,
        )
        return runtime_pipeline.SuccessfulResponse(
            display_text=core_raw or "",
            visible_text=visible_text,
            wrapper_result=wrapper_result,
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
        from orchestrator.runtime_display import _show_logo_animation
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
        if self.backend_manager.agent_mode == "audit":
            await self._reply_text(
                update,
                "Backend switching is managed by `/core` and `/audit` in **audit** mode.\nUse `/mode flex` for normal `/backend` switching.",
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
        if self.backend_manager.agent_mode == "audit":
            await self._reply_text(
                update,
                "Model switching is managed by `/core` and `/audit` in **audit** mode.\nUse `/mode flex` for normal `/model` switching.",
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

    def _is_audit_mode(self) -> bool:
        return getattr(self.backend_manager, "agent_mode", "flex") == "audit"

    def _is_managed_core_mode(self) -> bool:
        return getattr(self.backend_manager, "agent_mode", "flex") in {"wrapper", "audit"}

    async def _require_wrapper_mode(self, update: Update, command_name: str) -> bool:
        if self._is_wrapper_mode():
            return True
        await self._reply_text(
            update,
            f"`/{command_name}` only applies in **wrapper** mode.\nUse `/mode wrapper` first.",
            parse_mode="Markdown",
        )
        return False

    async def _require_managed_core_mode(self, update: Update, command_name: str) -> bool:
        if self._is_managed_core_mode():
            return True
        await self._reply_text(
            update,
            f"`/{command_name}` only applies in **wrapper** or **audit** mode.\nUse `/mode wrapper` or `/mode audit` first.",
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

    def _wrapper_core_keyboard(self, cfg) -> InlineKeyboardMarkup:
        models = ["gpt-5.5", "gpt-5.4", "gpt-5.3-codex"]
        rows: list[list[InlineKeyboardButton]] = []
        for model in models:
            label = f"✅ {model}" if cfg.core_backend == "codex-cli" and cfg.core_model == model else model
            rows.append([InlineKeyboardButton(label, callback_data=f"wcfg:core:codex-cli:{model}")])
        rows.append([
            InlineKeyboardButton("Wrapper model", callback_data="wcfg:menu:wrap"),
            InlineKeyboardButton("Slots", callback_data="wcfg:menu:wrapper"),
        ])
        return InlineKeyboardMarkup(rows)

    def _wrapper_model_choices(self) -> list[tuple[str, str, str, str]]:
        return [
            ("claude_haiku", "Claude Haiku", "claude-cli", "claude-haiku-4-5"),
            ("claude_sonnet", "Claude Sonnet", "claude-cli", "claude-sonnet-4-6"),
            ("gemini_flash", "Gemini Flash", "gemini-cli", "gemini-2.5-flash"),
            ("gemini_lite", "Gemini Lite", "gemini-cli", "gemini-2.5-flash-lite"),
            ("deepseek_flash", "DeepSeek Flash", "deepseek-api", "deepseek-v4-flash"),
            ("deepseek_chat", "DeepSeek Chat", "deepseek-api", "deepseek-chat"),
            ("or_deepseek", "OR DeepSeek", "openrouter-api", "deepseek/deepseek-v3.2-exp"),
            ("or_gemini", "OR Gemini", "openrouter-api", "google/gemini-3.1-flash-lite-preview"),
        ]

    def _wrapper_model_choice(self, choice_id: str) -> tuple[str, str, str, str] | None:
        return next((choice for choice in self._wrapper_model_choices() if choice[0] == choice_id), None)

    def _wrapper_wrap_keyboard(self, cfg) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        choices = {choice_id: (label, backend, model) for choice_id, label, backend, model in self._wrapper_model_choices()}
        grouped_rows = [
            ["claude_haiku", "claude_sonnet"],
            ["gemini_flash", "gemini_lite"],
            ["deepseek_flash", "deepseek_chat"],
            ["or_deepseek", "or_gemini"],
        ]
        for group in grouped_rows:
            row: list[InlineKeyboardButton] = []
            for choice_id in group:
                label, backend, model = choices[choice_id]
                active = cfg.wrapper_backend == backend and cfg.wrapper_model == model
                row.append(
                    InlineKeyboardButton(
                        f"✅ {label}" if active else label,
                        callback_data=f"wcfg:wrapid:{choice_id}:{cfg.context_window}",
                    )
                )
            rows.append(row)
        rows.append([
            InlineKeyboardButton(
                f"ctx {value}{' ✅' if cfg.context_window == value else ''}",
                callback_data=f"wcfg:wrapctx:{value}",
            )
            for value in (0, 3, 5)
        ])
        rows.append([
            InlineKeyboardButton("Core model", callback_data="wcfg:menu:core"),
            InlineKeyboardButton("Slots", callback_data="wcfg:menu:wrapper"),
        ])
        return InlineKeyboardMarkup(rows)

    def _wrapper_status_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Core model", callback_data="wcfg:menu:core"),
                    InlineKeyboardButton("Wrapper model", callback_data="wcfg:menu:wrap"),
                ],
                [InlineKeyboardButton("Refresh", callback_data="wcfg:menu:wrapper")],
            ]
        )

    def _wrapper_core_text(self, cfg) -> str:
        return (
            "Wrapper core model:\n"
            f"• Backend: `{cfg.core_backend}`\n"
            f"• Model: `{cfg.core_model}`\n\n"
            "Tap a button to change the core model, or type:\n"
            "`/core backend=codex-cli model=gpt-5.5`"
        )

    def _audit_core_model_choices(self) -> list[tuple[str, str, str, str]]:
        return runtime_audit.audit_core_model_choices(self)

    def _audit_auditor_model_choices(self) -> list[tuple[str, str, str, str]]:
        return runtime_audit.audit_auditor_model_choices(self)

    def _filter_allowed_model_choices(
        self,
        choices: list[tuple[str, str, str, str]],
    ) -> list[tuple[str, str, str, str]]:
        filtered: list[tuple[str, str, str, str]] = []
        for choice_id, label, backend, model in choices:
            if not self._allowed_wrapper_engine(backend):
                continue
            available = self._get_available_models_for(backend)
            if available and model not in available:
                continue
            filtered.append((choice_id, label, backend, model))
        return filtered

    def _audit_choice_by_id(self, target: str, choice_id: str) -> tuple[str, str, str, str] | None:
        return runtime_audit.audit_choice_by_id(self, target, choice_id)

    def _audit_model_keyboard(self, cfg, *, target: str) -> InlineKeyboardMarkup:
        return runtime_audit.audit_model_keyboard(self, cfg, target=target)

    def _audit_core_keyboard(self, cfg) -> InlineKeyboardMarkup:
        return self._audit_model_keyboard(cfg, target="core")

    def _audit_core_text(self, cfg) -> str:
        return runtime_audit.audit_core_text(cfg)

    def _audit_auditor_text(self, cfg) -> str:
        return runtime_audit.audit_auditor_text(cfg)

    def _audit_auditor_keyboard(self, cfg) -> InlineKeyboardMarkup:
        return self._audit_model_keyboard(cfg, target="audit")

    def _audit_config_keyboard(self, cfg) -> InlineKeyboardMarkup:
        return runtime_audit.audit_config_keyboard(cfg)

    def _audit_block_with(
        self,
        cfg,
        *,
        delivery: str | None = None,
        severity_threshold: str | None = None,
        timeout_s: float | None = None,
        backend: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        return runtime_audit.audit_block_with(
            cfg,
            delivery=delivery,
            severity_threshold=severity_threshold,
            timeout_s=timeout_s,
            backend=backend,
            model=model,
        )

    def _wrapper_wrap_text(self, cfg) -> str:
        return (
            "Wrapper translator model:\n"
            f"• Backend: `{cfg.wrapper_backend}`\n"
            f"• Model: `{cfg.wrapper_model}`\n"
            f"• Context window: `{cfg.context_window}`\n"
            f"• Fallback: `{cfg.fallback}`\n\n"
            "Tap a provider/model button below. Buttons are grouped by provider.\n"
            "Each model button changes both wrapper backend and model.\n"
            "Context buttons only change how many recent visible turns the wrapper sees.\n"
            "Default/recommended: `claude-cli / claude-haiku-4-5`.\n\n"
            "Or type one of these:\n"
            "`/wrap backend=claude-cli model=claude-haiku-4-5 context=3`\n"
            "`/wrap backend=gemini-cli model=gemini-2.5-flash context=3`\n"
            "`/wrap backend=deepseek-api model=deepseek-chat context=3`\n"
            "`/wrap backend=openrouter-api model=deepseek/deepseek-v3.2-exp context=3`"
        )

    def _wrapper_status_text(self, state: dict, slots: dict) -> str:
        cfg = load_wrapper_config(state)
        visible_slots = visible_wrapper_slots(slots)
        lines = [
            "Wrapper mode configuration:",
            f"• Core: <code>{html.escape(cfg.core_backend)} / {html.escape(cfg.core_model)}</code>",
            f"• Wrapper: <code>{html.escape(cfg.wrapper_backend)} / {html.escape(cfg.wrapper_model)}</code>",
            f"• Context window: <code>{cfg.context_window}</code>",
            "",
            "Tap buttons below to change core/wrapper models.",
            "Use <code>/wrapper set &lt;slot&gt; &lt;text&gt;</code> to edit persona/style.",
            "",
            "Persona/style slots:",
        ]
        if visible_slots:
            for key in sorted(visible_slots, key=lambda value: (not str(value).isdigit(), int(value) if str(value).isdigit() else str(value))):
                lines.append(f"• <code>{html.escape(str(key))}</code>: {html.escape(str(visible_slots[key]))}")
        else:
            lines.append("• none")
        return "\n".join(lines)

    async def _activate_wrapper_core_backend(
        self,
        chat_id: int,
        *,
        backend: str,
        model: str,
    ) -> tuple[bool, str]:
        current_model = self.get_current_model() if self.backend_manager.current_backend else None
        if self.config.active_backend == backend and current_model == model:
            return True, "Core backend already active."
        return await self._switch_backend_mode(
            chat_id,
            backend,
            target_model=model,
            with_context=False,
        )

    async def cmd_core(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        if not await self._require_managed_core_mode(update, "core"):
            return

        state = self.backend_manager.get_state_snapshot()
        mode = getattr(self.backend_manager, "agent_mode", "flex")
        cfg = load_wrapper_config(state) if mode == "wrapper" else load_audit_config(state)
        args = context.args or []
        if not args:
            text = self._wrapper_core_text(cfg) if mode == "wrapper" else self._audit_core_text(cfg)
            keyboard = self._wrapper_core_keyboard(cfg) if mode == "wrapper" else self._audit_core_keyboard(cfg)
            await self._reply_text(
                update,
                text,
                parse_mode="Markdown",
                reply_markup=keyboard,
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

        if mode == "wrapper":
            self.backend_manager.update_wrapper_blocks(core={"backend": backend, "model": model})
        else:
            self.backend_manager.update_audit_blocks(core={"backend": backend, "model": model})
        switch_ok, switch_message = await self._activate_wrapper_core_backend(
            update.effective_chat.id,
            backend=backend,
            model=model,
        )
        await self._reply_text(
            update,
            f"{mode.capitalize()} core updated:\n"
            f"• Backend: `{backend}`\n"
            f"• Model: `{model}`\n"
            f"• Active core: {'updated' if switch_ok else 'not changed'}\n"
            f"{switch_message}",
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
                self._wrapper_wrap_text(cfg),
                parse_mode="Markdown",
                reply_markup=self._wrapper_wrap_keyboard(cfg),
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
            await self._reply_text(
                update,
                self._wrapper_status_text(state, slots),
                parse_mode="HTML",
                reply_markup=self._wrapper_status_keyboard(),
            )
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
                slots = {"9": ""}
                message = "All wrapper slots cleared."
            else:
                if target == "9":
                    slots[target] = ""
                else:
                    slots.pop(target, None)
                message = f"Wrapper slot `{target}` cleared."
            self.backend_manager.update_wrapper_blocks(wrapper_slots=slots)
            await self._reply_text(update, message, parse_mode="Markdown")
            return

        await self._reply_text(update, "Usage: /wrapper [list|set <slot> <text>|clear <slot|all>]")

    def _audit_status_text(self, state: dict, criteria: dict) -> str:
        return runtime_audit.audit_status_text(state, criteria)

    def _audit_status_keyboard(self, cfg) -> InlineKeyboardMarkup:
        return self._audit_config_keyboard(cfg)

    async def cmd_audit(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        if not self._is_audit_mode():
            await self._reply_text(
                update,
                "`/audit` only applies in **audit** mode.\nUse `/mode audit` first.",
                parse_mode="Markdown",
            )
            return

        args = context.args or []
        action = (args[0].lower() if args else "list").strip()
        state = self.backend_manager.get_state_snapshot()
        criteria = state.get("audit_criteria")
        if not isinstance(criteria, dict):
            criteria = {}
        cfg = load_audit_config(state)

        if action in {"list", "status"}:
            await self._reply_text(
                update,
                self._audit_status_text(state, criteria),
                parse_mode="HTML",
                reply_markup=self._audit_status_keyboard(cfg),
            )
            return

        if action == "model":
            if len(args) == 1:
                await self._reply_text(
                    update,
                    self._audit_auditor_text(cfg),
                    parse_mode="Markdown",
                    reply_markup=self._audit_auditor_keyboard(cfg),
                )
                return
            values, positional = self._parse_backend_model_args(args[1:])
            backend = values.get("backend") or (positional[0] if positional else cfg.audit_backend)
            model = values.get("model") or (positional[1] if len(positional) > 1 else cfg.audit_model)
            backend = backend.strip().lower()
            model = self._normalize_wrapper_model(backend, model.strip())
            error = self._validate_wrapper_backend_model(backend, model)
            if error:
                await self._reply_text(update, error)
                return
            self.backend_manager.update_audit_blocks(
                audit={
                    "backend": backend,
                    "model": model,
                    "context_window": cfg.context_window,
                    "delivery": cfg.delivery,
                    "severity_threshold": cfg.severity_threshold,
                    "fail_policy": cfg.fail_policy,
                    "timeout_s": cfg.timeout_s,
                }
            )
            await self._reply_text(
                update,
                "Audit model updated:\n"
                f"• Backend: `{backend}`\n"
                f"• Model: `{model}`",
                parse_mode="Markdown",
            )
            return

        if action in {"delivery", "threshold", "timeout"}:
            if len(args) < 2:
                await self._reply_text(update, f"Usage: /audit {action} <value>")
                return
            value = args[1].strip().lower()
            audit_block = self._audit_block_with(cfg)
            if action == "delivery":
                if value not in {"silent", "issues_only", "always"}:
                    await self._reply_text(update, "Delivery must be one of: silent, issues_only, always")
                    return
                audit_block["delivery"] = value
            elif action == "threshold":
                if value not in {"low", "medium", "high", "critical"}:
                    await self._reply_text(update, "Threshold must be one of: low, medium, high, critical")
                    return
                audit_block["severity_threshold"] = value
            else:
                try:
                    audit_block["timeout_s"] = max(1.0, float(value))
                except ValueError:
                    await self._reply_text(update, "Timeout must be a number of seconds.")
                    return
            self.backend_manager.update_audit_blocks(audit=audit_block)
            result_key = {"delivery": "delivery", "threshold": "severity_threshold", "timeout": "timeout_s"}[action]
            await self._reply_text(update, f"Audit {action} updated to `{audit_block[result_key]}`.", parse_mode="Markdown")
            return

        if action == "set":
            if len(args) < 3:
                await self._reply_text(update, "Usage: /audit set <slot> <text>")
                return
            slot = args[1].strip()
            text = " ".join(args[2:]).strip()
            if not slot or not text:
                await self._reply_text(update, "Usage: /audit set <slot> <text>")
                return
            criteria[slot] = text
            self.backend_manager.update_audit_blocks(audit_criteria=criteria)
            await self._reply_text(update, f"Audit criterion `{slot}` updated.", parse_mode="Markdown")
            return

        if action == "clear":
            if len(args) < 2:
                await self._reply_text(update, "Usage: /audit clear <slot|all>")
                return
            target = args[1].strip()
            if target.lower() == "all":
                criteria = {"9": ""}
                message = "All audit criteria cleared."
            else:
                if target == "9":
                    criteria[target] = ""
                else:
                    criteria.pop(target, None)
                message = f"Audit criterion `{target}` cleared."
            self.backend_manager.update_audit_blocks(audit_criteria=criteria)
            await self._reply_text(update, message, parse_mode="Markdown")
            return

        await self._reply_text(
            update,
            "Usage: /audit [list|model backend=<backend> model=<model>|delivery <silent|issues_only|always>|threshold <low|medium|high|critical>|timeout <seconds>|set <slot> <text>|clear <slot|all>]",
        )

    async def callback_audit_config(self, update: Update, context: Any):
        query = update.callback_query
        if not self._is_authorized_user(query.from_user.id):
            return
        if not self._is_audit_mode():
            await query.answer("Audit controls require /mode audit.", show_alert=True)
            return

        data = query.data or ""
        try:
            state = self.backend_manager.get_state_snapshot()
            criteria = state.get("audit_criteria")
            if not isinstance(criteria, dict):
                criteria = {}
            cfg = load_audit_config(state)

            if data == "acfg:menu:core":
                await query.edit_message_text(
                    self._audit_core_text(cfg),
                    parse_mode="Markdown",
                    reply_markup=self._audit_core_keyboard(cfg),
                )
            elif data == "acfg:menu:auditmodel":
                await query.edit_message_text(
                    self._audit_auditor_text(cfg),
                    parse_mode="Markdown",
                    reply_markup=self._audit_auditor_keyboard(cfg),
                )
            elif data == "acfg:menu:audit":
                await query.edit_message_text(
                    self._audit_status_text(state, criteria),
                    parse_mode="HTML",
                    reply_markup=self._audit_status_keyboard(cfg),
                )
            elif data.startswith("acfg:coreid:") or data.startswith("acfg:auditid:"):
                parts = data.split(":", 2)
                if len(parts) != 3:
                    await query.answer("Invalid model selection.", show_alert=True)
                    return
                _, target_raw, choice_id = parts
                target = "core" if target_raw == "coreid" else "audit"
                choice = self._audit_choice_by_id(target, choice_id)
                if choice is None:
                    await query.answer("Unknown model choice.", show_alert=True)
                    return
                _, _label, backend, model = choice
                error = self._validate_wrapper_backend_model(backend, model)
                if error:
                    await query.answer(error, show_alert=True)
                    return
                if target == "core":
                    self.backend_manager.update_audit_blocks(core={"backend": backend, "model": model})
                    switch_ok, switch_message = await self._activate_wrapper_core_backend(
                        query.message.chat_id,
                        backend=backend,
                        model=model,
                    )
                    refreshed = load_audit_config(self.backend_manager.get_state_snapshot())
                    await query.edit_message_text(
                        "Audit core updated:\n"
                        f"• Backend: `{backend}`\n"
                        f"• Model: `{model}`\n"
                        f"• Active core: {'updated' if switch_ok else 'not changed'}\n"
                        f"{switch_message}",
                        parse_mode="Markdown",
                        reply_markup=self._audit_core_keyboard(refreshed),
                    )
                else:
                    self.backend_manager.update_audit_blocks(audit=self._audit_block_with(cfg, backend=backend, model=model))
                    refreshed = load_audit_config(self.backend_manager.get_state_snapshot())
                    await query.edit_message_text(
                        "Audit model updated:\n"
                        f"• Backend: `{backend}`\n"
                        f"• Model: `{model}`",
                        parse_mode="Markdown",
                        reply_markup=self._audit_auditor_keyboard(refreshed),
                    )
            elif data.startswith("acfg:delivery:") or data.startswith("acfg:threshold:"):
                parts = data.split(":", 2)
                if len(parts) != 3:
                    await query.answer("Invalid audit setting.", show_alert=True)
                    return
                _, setting, value = parts
                value = value.strip().lower()
                if setting == "delivery":
                    if value not in {"silent", "issues_only", "always"}:
                        await query.answer("Invalid delivery.", show_alert=True)
                        return
                    self.backend_manager.update_audit_blocks(audit=self._audit_block_with(cfg, delivery=value))
                else:
                    if value not in {"low", "medium", "high", "critical"}:
                        await query.answer("Invalid threshold.", show_alert=True)
                        return
                    self.backend_manager.update_audit_blocks(audit=self._audit_block_with(cfg, severity_threshold=value))
                refreshed_state = self.backend_manager.get_state_snapshot()
                refreshed = load_audit_config(refreshed_state)
                refreshed_criteria = refreshed_state.get("audit_criteria")
                if not isinstance(refreshed_criteria, dict):
                    refreshed_criteria = {}
                await query.edit_message_text(
                    self._audit_status_text(refreshed_state, refreshed_criteria),
                    parse_mode="HTML",
                    reply_markup=self._audit_status_keyboard(refreshed),
                )
            elif data.startswith("acfg:core:"):
                parts = data.split(":", 3)
                if len(parts) != 4:
                    await query.answer("Invalid core selection.", show_alert=True)
                    return
                _, _, backend, model = parts
                backend = backend.strip().lower()
                model = self._normalize_wrapper_model(backend, model.strip())
                error = self._validate_wrapper_backend_model(backend, model)
                if error:
                    await query.answer(error, show_alert=True)
                    return
                self.backend_manager.update_audit_blocks(core={"backend": backend, "model": model})
                switch_ok, switch_message = await self._activate_wrapper_core_backend(
                    query.message.chat_id,
                    backend=backend,
                    model=model,
                )
                refreshed = load_audit_config(self.backend_manager.get_state_snapshot())
                await query.edit_message_text(
                    "Audit core updated:\n"
                    f"• Backend: `{backend}`\n"
                    f"• Model: `{model}`\n"
                    f"• Active core: {'updated' if switch_ok else 'not changed'}\n"
                    f"{switch_message}",
                    parse_mode="Markdown",
                    reply_markup=self._audit_core_keyboard(refreshed),
                )
            else:
                await query.answer("Unknown audit control.", show_alert=True)
                return
        except Exception as e:
            self.error_logger.error(f"callback_audit_config error: {e}", exc_info=True)
            await query.answer(f"Error: {e}", show_alert=True)
            return
        await query.answer()

    async def callback_wrapper_config(self, update: Update, context: Any):
        query = update.callback_query
        if not self._is_authorized_user(query.from_user.id):
            return
        if not self._is_wrapper_mode():
            await query.answer("Wrapper controls require /mode wrapper.", show_alert=True)
            return

        data = query.data or ""
        try:
            state = self.backend_manager.get_state_snapshot()
            slots = state.get("wrapper_slots")
            if not isinstance(slots, dict):
                slots = {}
            cfg = load_wrapper_config(state)

            if data == "wcfg:menu:core":
                await query.edit_message_text(
                    self._wrapper_core_text(cfg),
                    parse_mode="Markdown",
                    reply_markup=self._wrapper_core_keyboard(cfg),
                )
            elif data == "wcfg:menu:wrap":
                await query.edit_message_text(
                    self._wrapper_wrap_text(cfg),
                    parse_mode="Markdown",
                    reply_markup=self._wrapper_wrap_keyboard(cfg),
                )
            elif data == "wcfg:menu:wrapper":
                await query.edit_message_text(
                    self._wrapper_status_text(state, slots),
                    parse_mode="HTML",
                    reply_markup=self._wrapper_status_keyboard(),
                )
            elif data.startswith("wcfg:core:"):
                parts = data.split(":", 3)
                if len(parts) != 4:
                    await query.answer("Invalid core selection.", show_alert=True)
                    return
                _, _, backend, model = parts
                backend = backend.strip().lower()
                model = self._normalize_wrapper_model(backend, model.strip())
                error = self._validate_wrapper_backend_model(backend, model)
                if error:
                    await query.answer(error, show_alert=True)
                    return
                self.backend_manager.update_wrapper_blocks(core={"backend": backend, "model": model})
                switch_ok, switch_message = await self._activate_wrapper_core_backend(
                    query.message.chat_id,
                    backend=backend,
                    model=model,
                )
                refreshed = load_wrapper_config(self.backend_manager.get_state_snapshot())
                await query.edit_message_text(
                    "Wrapper core updated:\n"
                    f"• Backend: `{backend}`\n"
                    f"• Model: `{model}`\n"
                    f"• Active core: {'updated' if switch_ok else 'not changed'}\n"
                    f"{switch_message}",
                    parse_mode="Markdown",
                    reply_markup=self._wrapper_core_keyboard(refreshed),
                )
            elif data.startswith("wcfg:wrap:"):
                parts = data.split(":", 4)
                if len(parts) != 5:
                    await query.answer("Invalid wrapper selection.", show_alert=True)
                    return
                _, _, backend, model, context_value = parts
                backend = backend.strip().lower()
                model = self._normalize_wrapper_model(backend, model.strip())
                error = self._validate_wrapper_backend_model(backend, model)
                if error:
                    await query.answer(error, show_alert=True)
                    return
                try:
                    context_window = max(0, min(int(context_value), 20))
                except ValueError:
                    await query.answer("Context window must be an integer.", show_alert=True)
                    return
                self.backend_manager.update_wrapper_blocks(
                    wrapper={
                        "backend": backend,
                        "model": model,
                        "context_window": context_window,
                        "fallback": cfg.fallback,
                    }
                )
                refreshed = load_wrapper_config(self.backend_manager.get_state_snapshot())
                await query.edit_message_text(
                    "Wrapper translator updated:\n"
                    f"• Backend: `{backend}`\n"
                    f"• Model: `{model}`\n"
                    f"• Context window: `{context_window}`\n"
                    f"• Fallback: `{cfg.fallback}`",
                    parse_mode="Markdown",
                    reply_markup=self._wrapper_wrap_keyboard(refreshed),
                )
            elif data.startswith("wcfg:wrapid:"):
                parts = data.split(":", 3)
                if len(parts) != 4:
                    await query.answer("Invalid wrapper selection.", show_alert=True)
                    return
                _, _, choice_id, context_value = parts
                choice = self._wrapper_model_choice(choice_id)
                if choice is None:
                    await query.answer("Unknown wrapper choice.", show_alert=True)
                    return
                _, _label, backend, model = choice
                error = self._validate_wrapper_backend_model(backend, model)
                if error:
                    await query.answer(error, show_alert=True)
                    return
                try:
                    context_window = max(0, min(int(context_value), 20))
                except ValueError:
                    await query.answer("Context window must be an integer.", show_alert=True)
                    return
                self.backend_manager.update_wrapper_blocks(
                    wrapper={
                        "backend": backend,
                        "model": model,
                        "context_window": context_window,
                        "fallback": cfg.fallback,
                    }
                )
                refreshed = load_wrapper_config(self.backend_manager.get_state_snapshot())
                await query.edit_message_text(
                    "Wrapper translator updated:\n"
                    f"• Backend: `{backend}`\n"
                    f"• Model: `{model}`\n"
                    f"• Context window: `{context_window}`\n"
                    f"• Fallback: `{cfg.fallback}`",
                    parse_mode="Markdown",
                    reply_markup=self._wrapper_wrap_keyboard(refreshed),
                )
            elif data.startswith("wcfg:wrapctx:"):
                parts = data.split(":", 2)
                if len(parts) != 3:
                    await query.answer("Invalid context selection.", show_alert=True)
                    return
                try:
                    context_window = max(0, min(int(parts[2]), 20))
                except ValueError:
                    await query.answer("Context window must be an integer.", show_alert=True)
                    return
                self.backend_manager.update_wrapper_blocks(
                    wrapper={
                        "backend": cfg.wrapper_backend,
                        "model": cfg.wrapper_model,
                        "context_window": context_window,
                        "fallback": cfg.fallback,
                    }
                )
                refreshed = load_wrapper_config(self.backend_manager.get_state_snapshot())
                await query.edit_message_text(
                    "Wrapper translator updated:\n"
                    f"• Backend: `{refreshed.wrapper_backend}`\n"
                    f"• Model: `{refreshed.wrapper_model}`\n"
                    f"• Context window: `{context_window}`\n"
                    f"• Fallback: `{refreshed.fallback}`",
                    parse_mode="Markdown",
                    reply_markup=self._wrapper_wrap_keyboard(refreshed),
                )
            else:
                await query.answer("Unknown wrapper control.", show_alert=True)
                return
        except Exception as e:
            self.error_logger.error(f"callback_wrapper_config error: {e}", exc_info=True)
            await query.answer(f"Error: {e}", show_alert=True)
            return
        await query.answer()

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
        await runtime_mode.cmd_mode(self, update, context)

    async def cmd_workzone(self, update: Update, context: Any):
        await runtime_workzone.cmd_workzone(self, update, context)

    async def cmd_new(self, update: Update, context: Any):
        await runtime_session.cmd_new(self, update, context)

    async def cmd_fresh(self, update: Update, context: Any):
        await runtime_session.cmd_fresh(self, update, context)

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
        await runtime_workspace.cmd_memory(self, update, context)

    async def cmd_wipe(self, update: Update, context: Any):
        await runtime_workspace.cmd_wipe(self, update, context)

    async def cmd_reset(self, update: Update, context: Any):
        await runtime_workspace.cmd_reset(self, update, context)

    async def cmd_clear(self, update: Update, context: Any):
        await runtime_workspace.cmd_clear(self, update, context)

    async def cmd_stop(self, update: Update, context: Any):
        await runtime_control.cmd_stop(self, update, context)

    async def cmd_retry(self, update: Update, context: Any):
        await runtime_control.cmd_retry(self, update, context)

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
        await runtime_remote.cmd_remote(self, update, context)

    async def cmd_oll(self, update: Update, context: Any):
        await runtime_remote.cmd_oll(self, update, context)

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
        return await runtime_media.download_media(self, file_id, filename)

    async def _handle_media_message(self, update, media_kind: str, filename: str, file_id: str, prompt: str, summary: str):
        await runtime_media.handle_media_message(self, update, media_kind, filename, file_id, prompt, summary)

    async def handle_document(self, update: Update, context: Any):
        await runtime_media.handle_document(self, update, context)

    async def handle_photo(self, update: Update, context: Any):
        await runtime_media.handle_photo(self, update, context)

    async def handle_voice(self, update: Update, context: Any):
        await runtime_media.handle_voice(self, update, context)

    async def handle_audio(self, update: Update, context: Any):
        await runtime_media.handle_audio(self, update, context)

    async def _handle_voice_or_audio(self, update: Update, media_kind: str, filename: str, file_id: str, caption: str = ""):
        await runtime_media.handle_voice_or_audio(self, update, media_kind, filename, file_id, caption=caption)

    async def handle_video(self, update: Update, context: Any):
        await runtime_media.handle_video(self, update, context)

    async def handle_sticker(self, update: Update, context: Any):
        await runtime_media.handle_sticker(self, update, context)

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
        await runtime_delivery.typing_loop(self, chat_id, stop_event)

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
                              think_buffer: list | None = None,
                              audit_collector: AuditTelemetryCollector | None = None):
        """Create an async callback that puts StreamEvents into the queue
        and/or appends all events to the think buffer."""
        from adapters.stream_events import KIND_THINKING
        _engine = self.config.active_backend
        _chunk_target = 100
        _chunk_hard_limit = 150
        _chunk_endings = ("。", "！", "？", "\n")
        async def _callback(event):
            if audit_collector is not None:
                # Best-effort hot path: audit telemetry must not disrupt stream delivery.
                with suppress(Exception):
                    await audit_collector.record(event)
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

    def _wrapper_enabled(self) -> bool:
        return runtime_wrapper.wrapper_enabled(self)

    def _wrapper_timeout_s(self) -> float:
        return runtime_wrapper.wrapper_timeout_s(self)

    def _wrapper_visible_context(self, context_window: int) -> list[dict[str, str]]:
        return runtime_wrapper.wrapper_visible_context(self, context_window)

    def _wrapper_audit_fields(self, wrapper_result) -> dict[str, Any]:
        return runtime_wrapper.wrapper_audit_fields(self, wrapper_result)

    def _wrapper_listener_fields(self, core_raw: str, visible_text: str, wrapper_result) -> dict[str, Any]:
        return runtime_wrapper.wrapper_listener_fields(core_raw, visible_text, wrapper_result)

    def _core_memory_assistant_text(self, core_raw: str, visible_text: str, wrapper_result) -> str:
        return runtime_wrapper.core_memory_assistant_text(self, core_raw, visible_text, wrapper_result)

    def _append_core_transcript(
        self,
        item: QueuedRequest,
        *,
        core_raw: str,
        visible_text: str,
        completion_path: str,
        wrapper_result,
    ) -> None:
        runtime_wrapper.append_core_transcript(
            self,
            item,
            core_raw=core_raw,
            visible_text=visible_text,
            completion_path=completion_path,
            wrapper_result=wrapper_result,
        )

    async def _send_wrapper_polishing_placeholder(self, item: QueuedRequest):
        return await runtime_wrapper.send_wrapper_polishing_placeholder(self, item)

    async def _delete_wrapper_polishing_placeholder(self, item: QueuedRequest, placeholder) -> None:
        await runtime_wrapper.delete_wrapper_polishing_placeholder(self, item, placeholder)

    async def _apply_wrapper_to_visible_text(self, item: QueuedRequest, visible_text: str):
        return await runtime_wrapper.apply_wrapper_to_visible_text(self, item, visible_text)

    @staticmethod
    def _wrapper_verbose_excerpt(text: str, *, limit: int = 1800) -> str:
        return runtime_wrapper.wrapper_verbose_excerpt(text, limit=limit)

    def _format_wrapper_verbose_trace(self, core_raw: str, visible_text: str, wrapper_result) -> str:
        return runtime_wrapper.format_wrapper_verbose_trace(self, core_raw, visible_text, wrapper_result)

    async def _send_wrapper_verbose_trace(self, item: QueuedRequest, core_raw: str, visible_text: str, wrapper_result) -> None:
        await runtime_wrapper.send_wrapper_verbose_trace(self, item, core_raw, visible_text, wrapper_result)

    def _audit_enabled(self) -> bool:
        return runtime_audit.audit_enabled(self)

    def _audit_timeout_s(self) -> float:
        return runtime_audit.audit_timeout_s(self)

    def _audit_visible_context(self, context_window: int) -> list[dict[str, str]]:
        return runtime_audit.audit_visible_context(self, context_window)

    def _build_audit_telemetry(self, item: QueuedRequest, response, collector: AuditTelemetryCollector | None) -> dict[str, Any]:
        return runtime_audit.build_audit_telemetry(self, item, response, collector)

    def _append_audit_transcript(
        self,
        item: QueuedRequest,
        *,
        core_raw: str,
        visible_text: str,
        telemetry: Mapping[str, Any],
        audit_result,
        completion_path: str,
    ) -> None:
        runtime_audit.append_audit_transcript(
            self,
            item,
            core_raw=core_raw,
            visible_text=visible_text,
            telemetry=telemetry,
            audit_result=audit_result,
            completion_path=completion_path,
        )

    def _write_audit_evidence(
        self,
        item: QueuedRequest,
        *,
        core_raw: str,
        visible_text: str,
        telemetry: Mapping[str, Any],
        completion_path: str,
        audit_criteria: Mapping[str, Any] | None,
        visible_context: list[dict[str, str]],
    ) -> str:
        return runtime_audit.write_audit_evidence(
            self,
            item,
            core_raw=core_raw,
            visible_text=visible_text,
            telemetry=telemetry,
            completion_path=completion_path,
            audit_criteria=audit_criteria,
            visible_context=visible_context,
        )

    def _schedule_audit_followup(
        self,
        item: QueuedRequest,
        *,
        core_raw: str,
        visible_text: str,
        response,
        audit_collector: AuditTelemetryCollector | None,
        completion_path: str,
    ) -> None:
        runtime_audit.schedule_audit_followup(
            self,
            item,
            core_raw=core_raw,
            visible_text=visible_text,
            response=response,
            audit_collector=audit_collector,
            completion_path=completion_path,
        )

    async def _run_audit_followup(
        self,
        item: QueuedRequest,
        *,
        core_raw: str,
        visible_text: str,
        response,
        audit_collector: AuditTelemetryCollector | None,
        completion_path: str,
    ) -> None:
        await runtime_audit.run_audit_followup(
            self,
            item,
            core_raw=core_raw,
            visible_text=visible_text,
            response=response,
            audit_collector=audit_collector,
            completion_path=completion_path,
        )

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
                await self._notify_request_listeners(
                    item.request_id,
                    {
                        "request_id": item.request_id,
                        "success": False,
                        "text": None,
                        "error": "background_task_cancelled",
                        "source": item.source,
                        "summary": item.summary,
                    },
                )
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
                await self._notify_request_listeners(
                    item.request_id,
                    {
                        "request_id": item.request_id,
                        "success": False,
                        "text": None,
                        "error": str(exc),
                        "source": item.source,
                        "summary": item.summary,
                    },
                )
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
                visible_text, wrapper_result = await self._apply_wrapper_to_visible_text(item, display_text or response.text)
                self._append_core_transcript(
                    item,
                    core_raw=response.text,
                    visible_text=visible_text,
                    completion_path="background",
                    wrapper_result=wrapper_result,
                )
                await self._notify_request_listeners(
                    item.request_id,
                    {
                        "request_id": item.request_id,
                        "success": True,
                        "text": visible_text,
                        "error": None,
                        "source": item.source,
                        "summary": item.summary,
                        **self._wrapper_listener_fields(response.text, visible_text, wrapper_result),
                    },
                )
                if self._should_buffer_during_transfer(item.request_id):
                    self._record_suppressed_transfer_result(item, success=True, text=visible_text)
                    return
                self.last_response = {
                    "chat_id": item.chat_id,
                    "text": visible_text,
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
                        _bg_output_tok = estimate_tokens(visible_text)
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
                        "completion_path": "background",
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
                        "response_chars": len(visible_text or ""),
                        "core_raw_chars": len(response.text or ""),
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
                        **self._wrapper_audit_fields(wrapper_result),
                    })
                except Exception:
                    pass
                memory_user_text = item.prompt
                if item.source.lower() in {"document", "photo", "voice", "audio", "video", "sticker"}:
                    memory_user_text = f"[{item.source}] {item.summary}"
                is_bridge_request = item.source.startswith("bridge:") or item.source.startswith("bridge-transfer:")
                if item.source not in {"startup", "system", SESSION_RESET_SOURCE}:
                    memory_assistant_text = self._core_memory_assistant_text(response.text, visible_text, wrapper_result)
                    self.memory_store.record_turn("user", item.source, memory_user_text)
                    self.memory_store.record_turn("assistant", self.config.active_backend, memory_assistant_text)
                    self.memory_store.record_exchange(memory_user_text, memory_assistant_text, item.source)
                    self._schedule_post_turn_observers(
                        item,
                        memory_user_text,
                        memory_assistant_text,
                        is_bridge_request=is_bridge_request,
                    )
                self.handoff_builder.append_transcript("user", item.prompt, item.source)
                self.handoff_builder.append_transcript("assistant", visible_text)
                self.handoff_builder.refresh_recent_context()
                self.project_chat_logger.log_exchange(item.prompt, visible_text, item.source)
                _print_final_response(self.name, visible_text)
                total_s = (datetime.now() - datetime.fromisoformat(item.created_at)).total_seconds()
                await self._send_wrapper_verbose_trace(item, response.text, visible_text, wrapper_result)
                send_elapsed_s, chunk_count = await self.send_long_message(
                    chat_id=item.chat_id,
                    text=visible_text,
                    request_id=item.request_id,
                    purpose="bg-response",
                )
                await self._send_voice_reply(item.chat_id, visible_text, item.request_id)
                self._schedule_audit_followup(
                    item,
                    core_raw=response.text,
                    visible_text=visible_text,
                    response=response,
                    audit_collector=getattr(item, "_audit_collector", None),
                    completion_path="background",
                )
                self.logger.info(
                    f"Background task {item.request_id} delivered "
                    f"(total_s={total_s:.2f}, chunks={chunk_count}, send_s={send_elapsed_s:.2f})"
                )
            else:
                err_msg = response.error or "Unknown error"
                self._mark_error(err_msg)
                self._record_habit_outcome(item, success=False, error_text=err_msg)
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
        await runtime_lifecycle.process_queue(self)

    def get_bot_commands(self):
        return runtime_command_binding.get_flexible_bot_commands(self)

    async def shutdown(self):
        await runtime_lifecycle.shutdown(self)
