from __future__ import annotations
import html
import re
import sys
import time
import asyncio
import inspect
import logging
from uuid import uuid4
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Optional, Any, Mapping
import json

import aiohttp
import yaml
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, constants
from telegram.error import RetryAfter, TimedOut as TelegramTimedOut
from telegram.ext import ApplicationBuilder

from orchestrator.config import FlexibleAgentConfig, GlobalConfig
from orchestrator.command_ui import (
    BACK_LABEL,
    REFRESH_LABEL,
    card_title,
    confirm_card,
    help_menu_text,
    selected_label,
    setting_card,
    status_label,
)
from orchestrator import runtime_audit, runtime_common, runtime_pending
from orchestrator import runtime_background_status
from orchestrator.browser_mode import (
    build_browser_task_prompt,
    get_browser_examples_text,
    get_browser_status_text,
)
from orchestrator.exp_mode import build_exp_task_prompt, get_exp_usage_text
from orchestrator import runtime_control
from orchestrator import runtime_cross_session
from orchestrator import runtime_delivery
from orchestrator import runtime_delivery_order
from orchestrator import runtime_lifecycle
from orchestrator import runtime_long
from orchestrator import runtime_media
from orchestrator import runtime_menu_views
from orchestrator import runtime_model_selection
from orchestrator import runtime_command_binding
from orchestrator import runtime_mode
from orchestrator import runtime_privacy
from orchestrator import runtime_nudge
from orchestrator import runtime_pipeline
from orchestrator import runtime_remote
from orchestrator import runtime_retry
from orchestrator import runtime_scheduler_recovery
from orchestrator import telegram_delivery_failover
from orchestrator import telegram_stream_policy
from orchestrator.telegram_notifications import disable_notification
from orchestrator.source_policy import source_requires_manual_remote_api_permission
from remote.local_http import local_http_hosts
from remote.runtime_identity import read_runtime_claim
from orchestrator import runtime_session
from orchestrator import runtime_status
from orchestrator import runtime_timeout
from orchestrator import runtime_transfer
from orchestrator import runtime_workspace
from orchestrator import runtime_wrapper
from orchestrator import runtime_workzone
from orchestrator.slash_command_audit import (
    SlashCommandAuditSession,
    default_audit_path,
    parse_inline_callback_command,
    resolve_handler_kind,
)
from orchestrator.enterprise.audit_schema import AuditEventWriter
from orchestrator.enterprise.channel_gate import EnterpriseChannelGate
from orchestrator.enterprise.policy import evaluate_governance_policy
from orchestrator.runtime_common import (
    QueuedRequest,
    _md_to_html,
    _print_final_response,
    _print_thinking,
    _print_user_message,
    _safe_excerpt,
    _streaming_status_to_html,
    resolve_authorized_telegram_ids,
)
from orchestrator.request_activity import RequestActivityStore
from orchestrator.runtime_defaults import DEFAULT_HASHI_REMOTE_PORT
from orchestrator.agent_fyi import build_agent_fyi_primer
from orchestrator.bridge_memory import BridgeMemoryStore, BridgeContextAssembler, SysPromptManager
from orchestrator.ephemeral_invoker import make_backend_sidecar_invoker
from orchestrator.flexible_backend_manager import FlexibleBackendManager
from orchestrator.flexible_backend_registry import (
    CLAUDE_MODEL_ALIASES,
    HER_V2_ENGINE,
    canonical_backend_engine,
    get_available_efforts,
    get_available_models,
    allows_custom_models,
    get_backend_label,
    normalize_effort,
    normalize_model,
)
from orchestrator.memory_index import MemoryIndex
from orchestrator.memory_search_mode import apply_memory_search_preference
from orchestrator.handoff_builder import HandoffBuilder
from orchestrator.media_utils import is_image_file, normalize_image_file
from orchestrator.parked_topics import ParkedTopicStore
from orchestrator.pcm import load_pcm_document
from orchestrator.post_turn_observer import (
    PostTurnObserver,
    PreTurnContextProvider,
)
from orchestrator import runtime_observers
from orchestrator.memory_plus_mode import (
    append_memory_plus_manual_note,
    clear_memory_plus_notepad,
    compact_memory_plus,
    extract_memory_plus_update_details,
    get_memory_plus_status,
    is_memory_plus_enabled,
    list_memory_plus_history,
    read_memory_plus_notepad,
    replace_memory_plus_notepad,
    search_memory_plus_history,
    set_memory_plus_enabled,
)
from orchestrator.usecomputer_mode import (
    build_usecomputer_task_prompt,
    get_usecomputer_examples_text,
    get_usecomputer_status,
    set_usecomputer_mode,
)
from orchestrator.skill_manager import SkillDefinition, SkillManager
from orchestrator.telegram_notifications import apply_disable_notification_default
from orchestrator.voice_manager import VoiceManager
from orchestrator.workzone import load_workzone
from orchestrator.wrapper_mode import SESSION_RESET_SOURCE, load_wrapper_config, visible_wrapper_slots
from orchestrator.audit_mode import (
    AuditTelemetryCollector,
    load_audit_config,
)
from orchestrator.dual_brain_mode import (
    DEFAULT_AFTER_ACTION_PROMPT,
    DEFAULT_LEFT_PROMPT,
    dual_brain_block_with,
    ensure_dual_brain_observer,
    load_dual_brain_config,
)

MAX_JOB_TRANSFER_SELECTIONS = 256


def _parse_key_values(args: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in args:
        key, sep, value = raw.partition("=")
        if sep and key.strip():
            values[key.strip().lower()] = value.strip()
    return values


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

        # Presentation-only, bounded request activity.  This is intentionally
        # independent from transcripts, durable jobs and audit ledgers.
        self.request_activity = RequestActivityStore(logger=self.logger)

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
        # Request metadata remains addressable after background detachment so
        # concurrent isolated HER runs never read another turn's context.
        self._request_meta_by_id: dict[str, dict[str, Any]] = {}
        self._background_request_ids: set[str] = set()
        self.last_activity_at = datetime.now()
        self.last_success_at: datetime | None = None
        self.last_error_at: datetime | None = None
        self.last_error_summary: str | None = None
        self.last_backend_switch_at: datetime | None = None
        self.is_shutting_down = False
        self._scheduled_retry_tasks: set[asyncio.Task] = set()
        # Background tasks spawned when bg_mode detaches a long-running generation.
        self._background_tasks: set[asyncio.Task] = set()
        # Tool-free, Persona-authored transition render/delivery helpers. These
        # are kept separate so they never masquerade as detached user work.
        self._persona_background_status_tasks: set[asyncio.Task] = set()
        self._request_listeners: dict[str, list] = {}
        self._pending_request_results: dict[str, dict] = {}
        self._transfer_state: dict | None = None
        self._suppressed_transfer_results: list[dict[str, Any]] = []
        # /long ... /end multimodal batching
        self._long_buffer: list[str] = []
        self._long_buffer_kinds: list[str] = []
        self._long_buffer_summaries: list[str] = []
        self._long_buffer_ids: list[str | None] = []
        self._long_buffer_metadata: list[dict[str, Any] | None] = []
        self._long_buffer_active: bool = False
        self._long_buffer_state: str = "idle"
        self._long_buffer_chat_id: int | None = None
        self._long_batch_id: str | None = None
        self._long_buffer_timeout_task: asyncio.Task | None = None
        self._long_finalize_task: asyncio.Task | None = None
        self._long_finalize_update: Any | None = None
        self._long_finalize_reason: str | None = None
        self._long_pending_media_ids: set[str] = set()
        self._long_batch_quiet_seconds: float = 2.0
        self._long_pending_voice_keys: set[str] = set()
        # Hashi Remote subprocess
        self._remote_process: asyncio.subprocess.Process | None = None
        self.skill_manager = skill_manager
        self.agent_fyi_path = self.global_config.project_root / "docs" / "AGENT_FYI.md"
        self._pending_session_primer: str | None = None
        self._pending_session_primer_session_id: str | None = None
        self._pending_auto_recall_context: str | None = None
        self._pending_auto_recall_session_id: str | None = None

        self.app = ApplicationBuilder().token(self.token).get_updates_connection_pool_size(8).build()

        # Workspace structure
        self.workspace_dir = config.workspace_dir
        # Load the durable workspace preference, migrating legacy markers once.
        self._verbose = telegram_stream_policy.get_display_preference(self, "verbose")
        # The same JSON survives sessions, runtime recreation, and host reboot.
        self._think = telegram_stream_policy.get_display_preference(self, "think")
        # HER persona updates are deliberately independent from think/verbose.
        self._commentary = telegram_stream_policy.get_display_preference(
            self,
            "commentary",
            default=True,
        )
        # Per-turn cost tail. Eligibility is frozen in _request_meta_by_id and
        # receipts are correlated by request ID; overlapping foreground and
        # background turns must never share a mutable "current" receipt.
        self._meter = telegram_stream_policy.get_display_preference(
            self, "meter", default=False
        )
        self._meter_receipt_by_id: dict[str, Any] = {}
        # Load persisted Telegram notification preference (.notify_on presence = audible, absence = silent)
        self._notify_enabled: bool = (self.workspace_dir / ".notify_on").exists()
        self._think_buffer: list[str] = []
        self._openrouter_think_chunk: str = ""
        self._last_openrouter_think_snippet: str | None = None
        self._thinking_chars_this_req: int = 0   # CLI thinking token estimation
        self._last_full_prompt_tokens: int = 0   # set before each request for bg-mode usage
        self._last_prompt_audit: dict = {}        # prompt section breakdown for token audit
        self.memory_dir = self.workspace_dir / "memory"
        self.sys_prompt_manager = SysPromptManager(self.workspace_dir)
        self.global_sys_prompt_manager = SysPromptManager.for_instance(self.global_config)
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
        default_session = runtime_session.initialize_runtime_sessions(self)
        legacy_workzone = load_workzone(self.workspace_dir)
        if default_session.get("workzone"):
            self._workzone_dir = Path(str(default_session["workzone"]))
        else:
            self._workzone_dir = legacy_workzone
            if legacy_workzone is not None:
                # One-time compatibility migration: the old Agent-wide value
                # becomes only the permanent default Session's Workzone.
                self.session_store.set_workzone(
                    default_session["session_id"], str(legacy_workzone)
                )
        self._sync_workzone_to_backend_config()
        self.voice_manager = VoiceManager(self.workspace_dir, self.media_dir, ffmpeg_cmd="ffmpeg", secrets=self.secrets)
        self._authorized_telegram_ids = resolve_authorized_telegram_ids(self.config.extra, self.global_config.authorized_id)
        self._active_chat_ids: dict[int, int] = {}  # user_id -> chat_id, populated on first message
        self._channel_gate = self._build_channel_gate()

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
        from orchestrator.canonical_audit import CanonicalAuditStore

        self.canonical_audit = CanonicalAuditStore(
            self.global_config.bridge_home,
            instance_id=self.global_config.instance_id,
            agent_id=self.config.name,
            config=getattr(self.global_config, "canonical_audit", None),
        )
        if self.transfer_state_path.exists():
            try:
                self._transfer_state = json.loads(self.transfer_state_path.read_text(encoding="utf-8"))
            except Exception:
                self._transfer_state = None

        # Initialize Memory and Handoff Subsystems
        self.memory_index = MemoryIndex(self.workspace_dir / "memory_index.sqlite")
        self.handoff_builder = HandoffBuilder(
            self.workspace_dir,
            canonical_audit=self.canonical_audit,
        )
        self.parked_topics = ParkedTopicStore(self.workspace_dir)
        self.memory_store = BridgeMemoryStore(self.workspace_dir)
        self.context_assembler = BridgeContextAssembler(
            self.memory_store,
            self.config.system_md,
            active_skill_provider=self._get_active_skill_sections,
            sys_prompt_manager=self.sys_prompt_manager,
            global_sys_prompt_manager=self.global_sys_prompt_manager,
            skill_catalog_provider=self._get_available_skill_catalogue,
            tool_catalog_provider=self._get_available_tool_catalogue,
        )
        apply_memory_search_preference(self.context_assembler, self.workspace_dir)
        # Initialize FlexibleBackendManager
        self.backend_manager = FlexibleBackendManager(config, global_config, secrets)
        self.backend_manager.runtime = self
        self._sidecar_invoker, self._sidecar_context_getter = make_backend_sidecar_invoker(
            self.backend_manager,
            session_id_getter=lambda: self.session_id_dt,
        )
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
            self._enabled_commands.update(
                {"bg", "jobs", "verbose", "think", "stream", "preview", "voice", "say", "whisper"}
            )

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
        self._enabled_commands.update({"help", "status", "new", "fresh", "sessions", "use", "current", "archive", "promote", "wipe", "reset", "clear", "memory", "notepad", "model", "effort", "mode", "wrapper", "audit", "brain", "core", "wrap", "bg", "jobs", "verbose", "think", "stream", "preview", "voice", "say", "whisper", "transfer", "fork", "cos", "long", "end", "browser", "exp"})

    def _is_command_allowed(self, cmd: str) -> bool:
        cmd = (cmd or "").lstrip("/").lower()
        if not cmd:
            return True
        evaluation = self._evaluate_enterprise_policy(
            "command.execute",
            resource=f"command:{cmd}",
            command_name=cmd,
        )
        if not evaluation.allowed:
            return False
        if cmd in self._disabled_commands:
            return False
        if self._command_policy_mode == "allow_all":
            return True
        if self._command_policy_mode == "allowlist":
            return cmd in self._enabled_commands
        # denylist
        return True

    def _evaluate_enterprise_policy(self, action: str, *, resource: str = "*", **context):
        global_config = getattr(self, "global_config", None)
        if global_config is None:
            return evaluate_governance_policy("noop", {"deployment_profile": "personal"})
        return evaluate_governance_policy(
            action,
            {
                "global_config": global_config,
                "agent_id": getattr(self, "name", None),
                "resource": resource,
                **context,
            },
        )

    def _build_channel_gate(self) -> EnterpriseChannelGate:
        profile = str(getattr(self.global_config, "deployment_profile", "personal") or "personal")
        if profile == "personal":
            return EnterpriseChannelGate.from_global_config(self.global_config)
        bridge_home = Path(getattr(self.global_config, "bridge_home", Path(".")))
        return EnterpriseChannelGate.from_global_config(
            self.global_config,
            audit_writer=AuditEventWriter(
                enabled=True,
                jsonl_path=bridge_home / "state" / "enterprise_audit.jsonl",
            ),
        )

    def _get_channel_gate(self) -> EnterpriseChannelGate:
        gate = getattr(self, "_channel_gate", None)
        if gate is None:
            gate = FlexibleAgentRuntime._build_channel_gate(self)
            self._channel_gate = gate
        return gate

    async def _telegram_channel_allowed(self, update: Update, *, source_channel: str) -> bool:
        query = getattr(update, "callback_query", None)
        effective_user = getattr(update, "effective_user", None) or getattr(query, "from_user", None)
        effective_chat = getattr(update, "effective_chat", None)
        if effective_chat is None and query is not None:
            effective_chat = getattr(getattr(query, "message", None), "chat", None)
        actor_id = getattr(effective_user, "id", None)
        chat_id = getattr(effective_chat, "id", None)
        result = FlexibleAgentRuntime._get_channel_gate(self).check_ingress(
            "telegram",
            actor_id=actor_id,
            user_id=str(actor_id) if actor_id is not None else None,
            agent_id=getattr(self, "name", None),
            audit_context={"chat_id": chat_id, "source_channel": source_channel},
        )
        if result.allowed:
            return True
        self.logger.warning(
            "Denied Telegram ingress via enterprise channel gate: agent=%s actor=%s chat=%s reason=%s",
            getattr(self, "name", None),
            actor_id,
            chat_id,
            result.reason,
        )
        if query is not None:
            answer = getattr(query, "answer", None)
            if callable(answer):
                maybe_awaitable = answer("Telegram access is not enabled for this enterprise HASHI workspace.")
                if inspect.isawaitable(maybe_awaitable):
                    await maybe_awaitable
        else:
            await self._reply_text(update, "Telegram access is not enabled for this enterprise HASHI workspace.")
        return False

    def _wrap_callback(self, handler_kind: str, handler):
        async def _wrapped(update: Update, context: Any):
            query = update.callback_query
            callback_data = getattr(query, "data", None) or ""
            command_name, args = parse_inline_callback_command(callback_data)
            actor_id = getattr(getattr(query, "from_user", None), "id", None)
            chat_id = getattr(getattr(getattr(query, "message", None), "chat", None), "id", None)
            session = SlashCommandAuditSession(
                audit_path=default_audit_path(self.workspace_dir),
                agent=self.name,
                command_name=command_name,
                args=args,
                source_channel="telegram_callback",
                handler_kind=handler_kind,
                actor_id=actor_id,
                chat_id=chat_id,
            )
            try:
                if not self._is_authorized_user(actor_id):
                    session.deny("unauthorized")
                    if query is not None:
                        await query.answer()
                    return
                if not await FlexibleAgentRuntime._telegram_channel_allowed(self, update, source_channel="telegram_callback"):
                    session.block("channel_denied")
                    return
                await handler(update, context)
            except Exception as exc:
                session.fail(exc)
                raise
            finally:
                session.finish()

        return _wrapped

    def _wrap_cmd(self, cmd: str, handler):
        async def _wrapped(update: Update, context: Any):
            args = list(getattr(context, "args", None) or [])
            actor_id = getattr(getattr(update, "effective_user", None), "id", None)
            chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
            session = SlashCommandAuditSession(
                audit_path=default_audit_path(self.workspace_dir),
                agent=self.name,
                command_name=cmd,
                args=args,
                source_channel="telegram",
                handler_kind=resolve_handler_kind(self, cmd),
                actor_id=actor_id,
                chat_id=chat_id,
            )
            try:
                if not self._is_authorized_user(actor_id):
                    session.deny("unauthorized")
                    return
                if not await FlexibleAgentRuntime._telegram_channel_allowed(self, update, source_channel="telegram"):
                    session.block("channel_denied")
                    return
                self._record_active_chat(update)
                if not self._is_command_allowed(cmd):
                    session.block("command_disabled")
                    await self._reply_text(update, f"/{cmd} is disabled for this agent.")
                    return
                await handler(update, context)
            except Exception as exc:
                session.fail(exc)
                raise
            finally:
                session.finish()
        return _wrapped

    def _setup_logging(self):
        formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        events_handler = None
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
            if cur_logger is self.logger:
                events_handler = fh

        # HER runs in process but uses its own backend-scoped logger. Route it
        # into the agent's durable event log so planning and background
        # Meditation lifecycle records are not lost to the console-only root
        # filter. Reusing the handler also keeps each record single-copy.
        her_logger = logging.getLogger(f"Backend.HER.{self.name}")
        her_logger.handlers.clear()
        her_logger.setLevel(logging.INFO)
        her_logger.propagate = False
        if events_handler is not None:
            her_logger.addHandler(events_handler)

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
                habit_learning_eligible=item.habit_learning_eligible,
                scheduler_context=item.scheduler_context,
                request_metadata=item.request_metadata,
                request_content=item.request_content,
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
        agent = re.sub(r"[^a-zA-Z0-9_-]+", "-", self.name).strip("-") or "agent"
        return f"req-{agent}-{self.session_id_dt}-{self.request_seq:04d}"

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
        habit_learning_eligible: bool = True,
        skill_id: str | None = None,
        scheduler_context: Mapping[str, str] | None = None,
        request_metadata: Mapping[str, Any] | None = None,
        request_content: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ):
        if not prompt or not prompt.strip():
            self.error_logger.error(f"Rejected empty prompt from {source} (summary={summary!r})")
            return None
        normalized_request_content = None
        manifest = ()
        if request_content is not None:
            from orchestrator.multimodal_contract import (
                attachment_manifest,
                normalize_request_content,
            )

            normalized_request_content = normalize_request_content(request_content)
            manifest = attachment_manifest(normalized_request_content)
        request_id = self.next_request_id()
        session, accepted, session_owner, session_surface, session_channel_key = (
            runtime_session.accept_request(
                self,
                request_id=request_id,
                chat_id=chat_id,
                prompt=prompt,
                source=source,
                request_metadata=request_metadata,
                request_content=normalized_request_content,
                idempotency_key=idempotency_key,
            )
        )
        if accepted is not None and accepted.replayed:
            self.message_logger.info(
                "Reused idempotent Session run %s for %s", accepted.run_id, accepted.request_id
            )
            return accepted.request_id
        metadata = dict(request_metadata or {})
        metadata.update(
            {
                "hashi_session_id": session["session_id"],
                "hashi_run_id": accepted.run_id if accepted is not None else None,
                "hashi_message_id": accepted.message_id if accepted is not None else None,
                "context_generation": int(session["context_generation"]),
                "owner_id": session_owner,
                "session_surface": session_surface,
                "session_channel_key": session_channel_key,
                "session_workspace": str(
                    self.session_store.session_workspace(
                        session["session_id"], int(session["context_generation"])
                    )
                ),
            }
        )
        item = runtime_common.QueuedRequest(
            request_id=request_id,
            chat_id=chat_id,
            prompt=prompt,
            source=source,
            summary=summary,
            created_at=datetime.now().isoformat(),
            silent=silent,
            is_retry=is_retry,
            deliver_to_telegram=deliver_to_telegram,
            skip_memory_injection=skip_memory_injection,
            session_id=session["session_id"],
            run_id=accepted.run_id if accepted is not None else None,
            message_id=accepted.message_id if accepted is not None else None,
            context_generation=int(session["context_generation"]),
            owner_id=session_owner,
            session_surface=session_surface,
            session_channel_key=session_channel_key,
            habit_learning_eligible=habit_learning_eligible,
            skill_id=skill_id,
            scheduler_context=(
                dict(scheduler_context) if scheduler_context else None
            ),
            request_metadata=metadata,
            request_content=normalized_request_content,
            attachment_manifest=manifest,
        )
        usage_recorder = getattr(
            getattr(self, "skill_manager", None), "record_skill_usage", None
        )
        if item.skill_id and callable(usage_recorder):
            item.skill_usage_event_id = usage_recorder(
                item.skill_id,
                agent=self.name,
                request_id=item.request_id,
                source=item.source,
            )
        runtime_delivery_order.register_turn(self, item)
        runtime_cross_session.capture_reply_target(self, item)
        self.request_activity.start(
            item.request_id,
            source=item.source,
            created_at=datetime.fromisoformat(item.created_at).timestamp(),
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
        # Commit the canonical Session terminal state before any transport or
        # in-memory listener observes the result.
        runtime_session.finish_request_from_listener(self, request_id, payload)
        try:
            self.request_activity.complete(
                request_id,
                success=bool(payload.get("success")),
                error=payload.get("error") or "",
            )
        except Exception as exc:  # display telemetry must never block delivery
            self.logger.warning(
                "Request activity completion failed for %s (%s)",
                request_id,
                type(exc).__name__,
            )
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
        apply_disable_notification_default(self, kwargs)
        request_id = kwargs.pop("_request_id", None)
        purpose = kwargs.pop("_purpose", "reply")
        delivery_mode = kwargs.pop("_delivery_mode", "normal_reply")
        chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
        if delivery_mode != "failover_notice":
            if await telegram_delivery_failover.handle_blocked_send(
                self,
                chat_id=chat_id,
                request_id=request_id,
                purpose=purpose,
                text=text,
            ):
                return None
        last_error = None
        for _ in range(2):
            try:
                return await update.message.reply_text(text, **kwargs)
            except RetryAfter as exc:
                last_error = exc
                await telegram_delivery_failover.handle_retry_after(
                    self,
                    exc=exc,
                    chat_id=chat_id,
                    request_id=request_id,
                    purpose=purpose,
                    text=text,
                )
                self.telegram_logger.warning(f"Reply failed: {exc}")
                return None
            except Exception as e:
                last_error = e
                self.telegram_logger.warning(f"Reply failed: {e}")
                await asyncio.sleep(0.8)
        raise last_error

    async def _send_text(self, chat_id: int, text: str, **kwargs):
        apply_disable_notification_default(self, kwargs)
        request_id = kwargs.pop("_request_id", None)
        purpose = kwargs.pop("_purpose", "send")
        delivery_mode = kwargs.pop("_delivery_mode", "normal_send")
        if delivery_mode != "failover_notice":
            if await telegram_delivery_failover.handle_blocked_send(
                self,
                chat_id=chat_id,
                request_id=request_id,
                purpose=purpose,
                text=text,
            ):
                return None
        last_error = None
        for _ in range(2):
            try:
                return await self.app.bot.send_message(chat_id=chat_id, text=text, **kwargs)
            except RetryAfter as exc:
                last_error = exc
                await telegram_delivery_failover.handle_retry_after(
                    self,
                    exc=exc,
                    chat_id=chat_id,
                    request_id=request_id,
                    purpose=purpose,
                    text=text,
                )
                self.telegram_logger.warning(f"Send failed: {exc}")
                return None
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
        canonical = getattr(self, "canonical_audit", None)
        if canonical is not None:
            try:
                canonical.record(
                    "operation_lifecycle",
                    {
                        "stage": stage,
                        "source": item.source,
                        "summary": item.summary,
                        "fields": fields,
                    },
                    request_id=item.request_id,
                    provenance={"source": "runtime_maintenance"},
                )
            except Exception as exc:
                self.error_logger.error(
                    "Canonical lifecycle audit failed for %s: %s",
                    item.request_id,
                    exc,
                )
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

    def get_current_provider(self) -> str | None:
        if self.config.active_backend == HER_V2_ENGINE:
            return self.backend_manager.get_her_v2_configuration().provider
        return None

    def reload_post_turn_observers(self) -> None:
        runtime_observers.reload_post_turn_observers(self)

    async def _build_pre_turn_context_sections(
        self,
        item: QueuedRequest,
        user_text: str,
        *,
        is_bridge_request: bool,
        metadata: dict[str, Any] | None = None,
    ) -> list[tuple[str, str]]:
        from orchestrator.fresh_context import automatic_context_suppressed

        fresh_suppressed = (
            str((metadata or {}).get("engine") or "") == HER_V2_ENGINE
            and automatic_context_suppressed(self)
        )
        if fresh_suppressed:
            sections = []
        else:
            sections = await runtime_observers.build_pre_turn_context_sections(
                self,
                item,
                user_text,
                is_bridge_request=is_bridge_request,
                metadata=metadata,
            )
            sections += runtime_scheduler_recovery.context_section(self, item.source)
        if (
            str((metadata or {}).get("engine") or "") == HER_V2_ENGINE
            and str(
                getattr(self.backend_manager, "agent_mode", "flex") or "flex"
            ).lower()
            == "flex"
        ):
            # HER-v2 receipts are merged into the managed conversation timeline
            # by actual completion time.  A second standalone section would
            # duplicate history and make old receipts look artificially recent.
            return sections
        if fresh_suppressed:
            return sections
        return sections + runtime_cross_session.context_section(self, item)

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

    def _notify_right_brain_started(
        self,
        item: QueuedRequest,
        user_text: str,
        *,
        final_prompt: str,
        is_bridge_request: bool,
    ) -> None:
        runtime_observers.notify_right_brain_started(
            self,
            item,
            user_text,
            final_prompt=final_prompt,
            is_bridge_request=is_bridge_request,
        )

    def _notify_right_brain_completed(
        self,
        item: QueuedRequest,
        user_text: str,
        assistant_text: str,
        *,
        is_bridge_request: bool,
        completion_path: str,
    ) -> None:
        runtime_observers.notify_right_brain_completed(
            self,
            item,
            user_text,
            assistant_text,
            is_bridge_request=is_bridge_request,
            completion_path=completion_path,
        )

    def _notify_right_brain_interrupted(
        self,
        item: QueuedRequest,
        user_text: str,
        *,
        is_bridge_request: bool,
        reason: str,
        error: str | None = None,
    ) -> None:
        runtime_observers.notify_right_brain_interrupted(
            self,
            item,
            user_text,
            is_bridge_request=is_bridge_request,
            reason=reason,
            error=error,
        )

    def _observer_workspace_keep_names(self) -> set[str]:
        return runtime_observers.observer_workspace_keep_names(self)

    def _get_system_prompt_text(self) -> str:
        """Return combined system prompt text for token estimation (CLI backends)."""
        parts = []
        try:
            md_path = getattr(self.config, "system_md", None)
            if md_path and Path(md_path).exists():
                parts.append(
                    load_pcm_document(
                        md_path,
                        workspace_dir=self.workspace_dir,
                    ).system
                )
        except Exception:
            pass
        try:
            for text in self.global_sys_prompt_manager.get_active_texts():
                parts.append(text)
        except Exception:
            pass
        try:
            for text in self.sys_prompt_manager.get_active_texts():
                parts.append(text)
        except Exception:
            pass
        return "\n".join(parts)

    def get_runtime_metadata(self) -> dict:
        delivery = telegram_delivery_failover.delivery_status_summary(self)
        display_policy = telegram_stream_policy.get_display_policy(self)
        return {
            "id": self.name,
            "name": self.name,
            "display_name": self.get_display_name(),
            "emoji": self.get_agent_emoji(),
            "engine": self.config.active_backend,
            "active_backend": self.config.active_backend,
            "model": self.get_current_model(),
            "provider": self.get_current_provider(),
            "allowed_backends": [dict(backend) for backend in self.config.allowed_backends],
            "workspace_dir": str(self.workspace_dir),
            "transcript_path": str(self.transcript_log_path),
            "online": bool(self.backend_ready),
            "status": self._compute_status_string(),
            "type": self.config.type,
            "telegram_connected": self.telegram_connected,
            "telegram_delivery_blocked": bool(delivery),
            "telegram_delivery_blocked_until": delivery.get("blocked_until") if delivery else None,
            "telegram_delivery_failover_agent": delivery.get("active_failover_agent") if delivery else None,
            "telegram_typing_enabled": display_policy.typing_enabled,
            "telegram_typing_source": display_policy.source,
            "telegram_display": {
                "typing": display_policy.typing_enabled,
                "verbose": self._verbose,
                "think": self._think,
            },
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

    def _get_available_tool_catalogue(self) -> list[dict[str, str]]:
        """Describe only tools attached to the active backend for this turn."""

        manager = getattr(self, "backend_manager", None)
        backend = getattr(manager, "current_backend", None)
        registry = getattr(backend, "tool_registry", None)
        if registry is None:
            return []
        active_backend = str(getattr(self.config, "active_backend", ""))
        if active_backend == "grok-cli":
            # Grok currently exposes only persistent user/project MCP config.
            # HASHI will not mutate that shared state or advertise Registry
            # tools until Grok provides an isolated per-invocation bridge.
            return []
        fixed_mcp = active_backend in {
            "codex-cli",
            "claude-cli",
        }
        if fixed_mcp and not bool(getattr(backend, "_hashi_mcp_enabled", False)):
            return []
        exposed_names = {
            str(name)
            for name in (
                (getattr(backend, "_hashi_mcp_descriptor", None) or {}).get(
                    "exposed_tools", []
                )
            )
        }
        definitions = registry.get_tool_definitions()
        catalogue: list[dict[str, str]] = []
        for definition in definitions:
            function = definition.get("function", {}) if isinstance(definition, dict) else {}
            name = str(function.get("name") or "").strip()
            if fixed_mcp:
                from tools.gateway.mcp_stdio import exposed_tool_name

                name = exposed_tool_name(name)
                if name not in exposed_names:
                    continue
            if not name:
                continue
            catalogue.append(
                {
                    "name": name,
                    "description": str(function.get("description") or "").strip(),
                }
            )
        return catalogue

    def _is_hashi_tool_connected(self, name: str) -> bool:
        """Check baseline Tool connectivity without inheriting a prior request scope."""

        manager = getattr(self, "backend_manager", None)
        backend = getattr(manager, "current_backend", None)
        registry = getattr(backend, "tool_registry", None)
        tool_name = str(name or "").strip()
        if registry is None or not tool_name or not registry.is_allowed(tool_name):
            return False
        active_backend = str(getattr(self.config, "active_backend", ""))
        if active_backend == "grok-cli":
            return False
        if active_backend in {"codex-cli", "claude-cli"}:
            if not bool(getattr(backend, "_hashi_mcp_enabled", False)):
                return False
            from tools.gateway.mcp_stdio import exposed_tool_name

            return exposed_tool_name(tool_name) in set(
                (getattr(backend, "_hashi_mcp_descriptor", None) or {}).get(
                    "exposed_tools", []
                )
            )
        return True

    def _get_available_skill_catalogue(self) -> list[dict[str, str]]:
        """Describe enabled Skills without injecting their instruction bodies."""

        manager = self.skill_manager
        if manager is None:
            return []
        tool_names = {
            item["name"] for item in self._get_available_tool_catalogue()
        }
        catalogue: list[dict[str, str]] = []
        for skill in manager.list_skills():
            if skill.id in manager.RUNTIME_TOGGLE_IDS:
                continue
            if not manager.is_skill_enabled(self.workspace_dir, skill.id):
                continue
            if skill.id == "memory-search" and "memory_search" not in tool_names:
                continue
            catalogue.append(
                {"name": skill.id, "description": skill.description}
            )
        return catalogue

    def _arm_session_primer(
        self, context_line: str, *, session_id: str | None = None
    ):
        primer = build_agent_fyi_primer(self.agent_fyi_path, context_line=context_line)
        if primer:
            self._pending_session_primer = primer
            self._pending_session_primer_session_id = str(
                session_id or getattr(self, "default_session_id", "") or ""
            ) or None

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
        return str(getattr(self.global_config, "instance_id", None) or "HASHI").upper()

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
        # Telegram is connected by this point. Resume durable Habit notices
        # that could not be delivered before a restart or temporary outage.
        from orchestrator import runtime_her_habits

        resumed_habit_notifications = runtime_her_habits.resume_pending_habit_notifications(self)
        if resumed_habit_notifications:
            self.logger.info(
                "Resumed %d pending HER Habit notification(s).",
                resumed_habit_notifications,
            )
        if not self.skill_manager:
            return
        active = self.skill_manager.get_active_toggle_ids(self.workspace_dir)
        if "recall" not in active or not unexpected_restart:
            return
        default_handoff = runtime_session.session_handoff_builder(
            self, surface="scheduled", channel_key="default"
        )
        context_block, exchange_count, word_count = default_handoff.build_recent_context_block(
            max_rounds=10,
            max_words=6000,
        )
        if exchange_count <= 0 or not context_block:
            return
        self._pending_auto_recall_context = (
            "This session is recovering from an unexpected interruption. Restore recent continuity from the bridge-managed transcript below and use it as background context only.\n\n"
            f"{context_block}"
        )
        self._pending_auto_recall_session_id = getattr(
            self, "default_session_id", None
        )
        self._arm_session_primer(
            f"Unexpected restart detected. Recall mode is ON, so restore the last {exchange_count} exchanges ({word_count} words) once before continuing.",
            session_id=getattr(self, "default_session_id", None),
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
        return f"{primer}\n\n--- CURRENT USER REQUEST — AUTHORITATIVE ---\n{request}"

    def _consume_session_primer(self, item: QueuedRequest) -> str:
        if item.source.startswith("scheduler") or item.source.startswith("bridge:") or item.source.startswith("bridge-transfer:"):
            return item.prompt
        if item.silent:
            return item.prompt
        sections = []
        item_session_id = str(getattr(item, "session_id", "") or "")
        if self._pending_session_primer and (
            not getattr(self, "_pending_session_primer_session_id", None)
            or self._pending_session_primer_session_id == item_session_id
        ):
            sections.append(self._pending_session_primer)
            self._pending_session_primer = None
            self._pending_session_primer_session_id = None
        if self._pending_auto_recall_context and (
            not getattr(self, "_pending_auto_recall_session_id", None)
            or self._pending_auto_recall_session_id == item_session_id
        ):
            sections.append(f"--- AUTO RECALL ---\n{self._pending_auto_recall_context}")
            self._pending_auto_recall_context = None
            self._pending_auto_recall_session_id = None
        if not sections:
            return item.prompt
        return "\n\n".join(sections + [item.prompt])

    def _source_requires_manual_permission(self, source: str) -> bool:
        return source_requires_manual_remote_api_permission(source)

    def _remote_backend_block_reason(self, source: str) -> str | None:
        engine = (self.config.active_backend or "").strip().lower()
        if engine not in {"openrouter-api", "deepseek-api", "xai-api"}:
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

    async def _summarize_current_topic_for_parking(
        self,
        title_override: str | None = None,
        *,
        update: Update | None = None,
    ) -> dict[str, Any] | None:
        handoff_builder = runtime_session.session_handoff_builder(
            self, update=update
        )
        context_block, exchange_count, _ = handoff_builder.build_recent_context_block(
            max_rounds=12,
            max_words=4500,
        )
        if exchange_count <= 0 or not context_block:
            return None

        recent_rounds = handoff_builder.get_recent_rounds(max_rounds=3)
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
        return runtime_menu_views.parked_topics_text(self.parked_topics.list_topics())

    def is_idle_for_proactive_message(self, min_idle_seconds: int = 900) -> bool:
        if self._backend_busy():
            return False
        session_store = getattr(self, "session_store", None)
        if session_store is not None:
            last_user_ts = session_store.last_user_message_at(
                agent_id=self.name,
                owner_id=runtime_session.owner_id(self),
            )
        else:
            last_user_ts = self.memory_store.get_last_user_turn_ts()
        if not last_user_ts:
            return True
        try:
            last_user_at = datetime.fromisoformat(str(last_user_ts).replace("Z", "+00:00"))
            now = datetime.now(last_user_at.tzinfo) if last_user_at.tzinfo else datetime.now()
            idle_for = (now - last_user_at).total_seconds()
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

    def _build_status_text(
        self, detailed: bool = False, *, update: Update | None = None
    ) -> str:
        return runtime_status.build_status_text(
            self, detailed=detailed, update=update
        )

    def _skill_keyboard(self) -> InlineKeyboardMarkup:
        from orchestrator.runtime_skill_callbacks import build_skill_catalog_keyboard

        if not self.skill_manager:
            return InlineKeyboardMarkup(
                [[InlineKeyboardButton("No skills", callback_data="skill:noop:none")]]
            )
        return build_skill_catalog_keyboard(self.skill_manager, self.workspace_dir)

    def _skill_action_keyboard(self, skill: SkillDefinition) -> InlineKeyboardMarkup | None:
        from orchestrator.runtime_skill_callbacks import build_skill_action_keyboard

        if not self.skill_manager:
            return None
        return build_skill_action_keyboard(self.skill_manager, self.workspace_dir, skill)

    async def _render_jobs(self, update_or_query, kind: str):
        from orchestrator.runtime_jobs import _build_jobs_with_buttons
        text, markup = _build_jobs_with_buttons(self, self.name, self.skill_manager, filter_agent=self.name)
        if hasattr(update_or_query, "edit_message_text"):
            await update_or_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        else:
            await self._reply_text(update_or_query, text, parse_mode="HTML", reply_markup=markup)

    async def invoke_scheduler_skill(
        self,
        skill_id: str,
        args: str,
        task_id: str,
        *,
        scheduler_context: Mapping[str, str] | None = None,
    ) -> tuple[bool, str | None]:
        if str(skill_id or "").casefold() == "dream":
            # Legacy scheduled Dream jobs must never reach the retired generic
            # memory/AGENT.md writer. Route them through native HER Dream.
            return await self.invoke_her_dream(task_id=task_id)
        from orchestrator.automation_runner import is_automation

        if is_automation(skill_id):
            manager = getattr(self, "skill_manager", None)
            skill = manager.get_skill(skill_id) if manager is not None else None
            enabled_check = getattr(manager, "is_skill_enabled", None)
            if skill is not None and callable(enabled_check) and not enabled_check(
                self.workspace_dir,
                skill.id,
            ):
                message = f"Scheduler Skill is disabled for {self.name}: {skill.id}"
                self.error_logger.error(message)
                return False, message
            return await self.invoke_scheduler_automation(
                automation_id=skill.id if skill is not None else skill_id,
                args=args,
                task_id=task_id,
            )
        if not self.skill_manager:
            message = f"Scheduler skill invocation requested without skill manager: {skill_id}"
            self.error_logger.error(message)
            return False, message
        skill = self.skill_manager.get_skill(skill_id)
        if skill is None:
            message = f"Unknown scheduler skill: {skill_id}"
            self.error_logger.error(message)
            return False, message
        enabled_check = getattr(self.skill_manager, "is_skill_enabled", None)
        if callable(enabled_check) and not enabled_check(self.workspace_dir, skill.id):
            message = f"Scheduler Skill is disabled for {self.name}: {skill.id}"
            self.error_logger.error(message)
            return False, message
        prompt = self.skill_manager.build_prompt_for_skill(skill, args or "")
        scheduler_kwargs = (
            {"scheduler_context": dict(scheduler_context)}
            if scheduler_context
            else {}
        )
        await self.enqueue_request(
            chat_id=self._primary_chat_id(),
            prompt=prompt,
            source="scheduler-skill",
            summary=f"Skill Task [{task_id}]",
            silent=False,
            skill_id=skill.id,
            **scheduler_kwargs,
        )
        return True, f"Scheduled prompt skill queued: {skill.id}"

    async def invoke_scheduler_automation(
        self,
        automation_id: str,
        args: str,
        task_id: str,
    ) -> tuple[bool, str | None]:
        from orchestrator.automation_runner import run_automation

        if not self.skill_manager:
            message = f"Scheduler automation requested without manager: {automation_id}"
            self.error_logger.error(message)
            return False, message
        skill = self.skill_manager.get_skill(automation_id)
        enabled_check = getattr(self.skill_manager, "is_skill_enabled", None)
        if skill is not None and callable(enabled_check) and not enabled_check(
            self.workspace_dir, skill.id
        ):
            message = f"Scheduler automation Skill is disabled for {self.name}: {skill.id}"
            self.error_logger.error(message)
            return False, message
        usage_recorder = getattr(self.skill_manager, "record_skill_usage", None)
        if skill is not None and callable(usage_recorder):
            usage_recorder(
                skill.id,
                agent=self.name,
                request_id=f"automation-{task_id}",
                source="scheduler-automation",
                task_id=task_id,
            )
        ok, text = await run_automation(
            project_root=self.skill_manager.project_root,
            workspace_dir=self.workspace_dir,
            automation_id=automation_id,
            args=args,
            extra_env={
                "BRIDGE_ACTIVE_BACKEND": self.config.active_backend,
                "BRIDGE_ACTIVE_MODEL": self.get_current_model(),
            },
        )
        if ok and text:
            await self.send_long_message(
                chat_id=self._primary_chat_id(),
                text=text,
                request_id=f"automation-{task_id}",
                purpose="scheduler-automation",
            )
        elif text:
            self.error_logger.error(text)
        return ok, text

    async def invoke_her_dream(
        self,
        *,
        task_id: str,
        scheduled_for: str | None = None,
    ) -> tuple[bool, str | None]:
        from orchestrator import runtime_her_dream

        return await runtime_her_dream.invoke_scheduled(
            self, task_id=task_id, scheduled_for=scheduled_for
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

    def get_progress_placeholder(self) -> tuple[str, str | None]:
        display_name = self.get_display_name()
        emoji = self.get_agent_emoji()
        return f"_{emoji}{display_name} is working..._", constants.ParseMode.MARKDOWN

    def _build_media_prompt(self, media_kind: str, filename: str, caption: str = "", emoji: str = "") -> tuple[str, str]:
        return runtime_media.build_media_prompt(media_kind, filename, caption=caption, emoji=emoji)

    async def enqueue_api_text(
        self,
        text: str,
        source: str = "api",
        deliver_to_telegram: bool = True,
        *,
        request_metadata: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ):
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
            request_metadata=request_metadata,
            idempotency_key=idempotency_key,
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
        request_metadata: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
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
            request_metadata=request_metadata,
            idempotency_key=idempotency_key,
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
        cmds = self.get_bot_commands()
        enabled = [c for c in cmds if self._is_command_allowed(c.command)]
        disabled = sorted({c.command for c in cmds if not self._is_command_allowed(c.command)})
        await self._reply_text(
            update,
            help_menu_text(
                agent_name=self.name,
                agent_type=getattr(self.config, "type", "flex"),
                commands=enabled,
                disabled=disabled,
            ),
            parse_mode="HTML",
        )

    def _startable_agent_keyboard(self) -> InlineKeyboardMarkup | None:
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            return None
        names = orchestrator.get_startable_agent_names(exclude_name=self.name)
        if not names:
            return None
        rows = [[InlineKeyboardButton(name, callback_data=f"startagent:{name}")] for name in names]
        rows.append([InlineKeyboardButton("Start all", callback_data="startagent:__all__")])
        return InlineKeyboardMarkup(rows)

    def _voice_keyboard(self) -> InlineKeyboardMarkup:
        enabled = bool(self.voice_manager.get_state().get("enabled"))
        rows = [
            [
                InlineKeyboardButton(selected_label("On", enabled), callback_data="voice:toggle:on"),
                InlineKeyboardButton(selected_label("Off", not enabled), callback_data="voice:toggle:off"),
            ]
        ]
        active_alias = self.voice_manager.get_active_preset_alias()
        preset_buttons = []
        for alias, preset, available in self.voice_manager.get_voice_presets():
            base = preset.get("label") or alias
            label = selected_label(base, alias == active_alias)
            if available != "ready":
                label = f"🔒 {base} ({available})"
            preset_buttons.append(InlineKeyboardButton(label, callback_data=f"voice:use:{alias}"))
        for i in range(0, len(preset_buttons), 2):
            rows.append(preset_buttons[i:i + 2])
        rows.append([InlineKeyboardButton(REFRESH_LABEL, callback_data="voice:refresh:menu")])
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
        await self._reply_text(
            update,
            setting_card(
                "▶️",
                "Start agents",
                current=f"<code>{html.escape(self.name)}</code> running",
                consequence="Starting an agent launches its configured runtime immediately.",
                action="Choose an agent to start.",
            ),
            parse_mode="HTML",
            reply_markup=keyboard,
        )

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
        all_agents = orchestrator.get_all_agents_raw()
        running_names = set(orchestrator._runtime_map().keys())
        starting_names = set(orchestrator._startup_tasks.keys())

        lines = [
            card_title("🤖", "Hashi agents"),
            "",
            f"<b>Current</b> · <code>{len(running_names)}</code> running · <code>{len(all_agents)}</code> configured",
            "<b>Changes</b> · activation state persists; lifecycle actions apply immediately",
            "",
            "<b>AGENTS</b>",
        ]
        rows = []

        for agent in all_agents:
            name = agent.get("name", "?")
            display = agent.get("display_name", name)
            is_active = agent.get("is_active", True)

            if name in starting_names:
                status_icon, status_text = "⏳", "starting"
            elif name in running_names:
                status_icon, status_text = "🟢", "running"
            elif is_active:
                status_icon, status_text = "⚪", "stopped"
            else:
                status_icon, status_text = "🔴", "inactive"

            lines.append(
                f"{status_icon} <b>{html.escape(name)}</b> · "
                f"{html.escape(str(display))} · <code>{status_text}</code>"
            )

            btn_row = []
            if is_active:
                btn_row.append(InlineKeyboardButton(f"Deactivate {name}", callback_data=f"agents:deactivate:{name}"))
            else:
                btn_row.append(InlineKeyboardButton(f"Activate {name}", callback_data=f"agents:activate:{name}"))

            if name in starting_names:
                btn_row.append(InlineKeyboardButton("⏳", callback_data="agents:noop"))
            elif name in running_names:
                btn_row.append(InlineKeyboardButton("Stop", callback_data=f"agents:stop:{name}"))
            elif is_active:
                btn_row.append(InlineKeyboardButton("Start", callback_data=f"agents:start:{name}"))

            btn_row.append(InlineKeyboardButton("Delete", callback_data=f"agents:delete:{name}"))
            rows.append(btn_row)

        rows.append([InlineKeyboardButton(REFRESH_LABEL, callback_data="agents:refresh")])
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
            confirm_markup = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(f"Delete {name}", callback_data=f"agents:confirmdelete:{name}")],
                    [InlineKeyboardButton("← Keep agent", callback_data="agents:refresh")],
                ]
            )
            await query.edit_message_text(
                confirm_card(
                    "⚠️",
                    "Delete agent",
                    target=f"<code>{html.escape(name)}</code>",
                    consequence="This removes the agent from configuration. Its workspace files are kept.",
                ),
                reply_markup=confirm_markup,
                parse_mode="HTML",
            )
            return
        elif action == "confirmdelete":
            if name in orchestrator._runtime_map():
                await query.answer(f"Stop {name} first.", show_alert=True)
                return
            delayed = await runtime_pending.delayed_count(self, agent_name=name)
            if delayed:
                await query.answer(
                    f"Recall {delayed} delayed message(s) from {name} first.",
                    show_alert=True,
                )
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
        await query.edit_message_text(text, reply_markup=self._voice_keyboard(), parse_mode="HTML")
        await query.answer()

    # ── toggle callback ──────────────────────────────────────────────────────────
    # Handles: tgl:verbose:on/off, tgl:think:on/off, tgl:commentary:on/off,
    #          tgl:typing:on/off,
    #          tgl:mode:fixed/flex,
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
            telegram_stream_policy.set_display_preference(self, "verbose", self._verbose)
            _f = self.workspace_dir / ".verbose_off"
            if self._verbose:
                _f.unlink(missing_ok=True)
            else:
                _f.touch()
            await query.edit_message_text(
                self._verbose_menu_text(),
                parse_mode="HTML",
                reply_markup=self._verbose_keyboard(),
            )
            await query.answer(f"Verbose {status_label(self._verbose)}")

        elif target == "think":
            self._think = value == "on"
            telegram_stream_policy.set_display_preference(self, "think", self._think)
            _f = self.workspace_dir / ".think_off"
            if self._think:
                _f.unlink(missing_ok=True)
            else:
                _f.touch()
            await query.edit_message_text(
                self._think_menu_text(),
                parse_mode="HTML",
                reply_markup=self._think_keyboard(),
            )
            await query.answer(f"Think {status_label(self._think)}")

        elif target == "commentary":
            if not self._commentary_available():
                await query.edit_message_text(
                    self._commentary_unavailable_text(),
                    parse_mode="HTML",
                )
                await query.answer("HER commentary is unavailable for this backend")
                return
            self._set_commentary_enabled(value == "on")
            await query.edit_message_text(
                self._commentary_menu_text(),
                parse_mode="HTML",
                reply_markup=self._commentary_keyboard(),
            )
            await query.answer(f"Commentary {status_label(self._commentary)}")

        elif target == "typing":
            enabled = value == "on"
            telegram_stream_policy.set_typing_enabled(self, enabled)
            await query.edit_message_text(
                self._typing_menu_text(),
                parse_mode="HTML",
                reply_markup=self._typing_keyboard(),
            )
            await query.answer(f"Typing {status_label(enabled)}")

        elif target == "meter":
            enabled = value == "on"
            telegram_stream_policy.set_display_preference(self, "meter", enabled)
            self._meter = enabled
            await query.edit_message_text(
                self._meter_menu_text(),
                parse_mode="HTML",
                reply_markup=self._meter_keyboard(),
            )
            await query.answer(f"Meter {status_label(enabled)}")

        elif target == "stream":
            await query.answer(
                "Stream controls moved to /typing, /verbose and /think.",
                show_alert=True,
            )

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
            await query.edit_message_text(
                self._whisper_menu_text(new_size),
                parse_mode="HTML",
                reply_markup=self._whisper_keyboard(new_size),
            )
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
            await query.edit_message_text(
                self._active_menu_text(notice=msg),
                parse_mode="HTML",
                reply_markup=self._active_keyboard(),
            )
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
                if num < 1 or num > len(all_names):
                    await query.answer(
                        "Invalid agent number.",
                        show_alert=True,
                    )
                    return
                mode, label = (
                    "number",
                    f"Restarting agent #{num} (<b>{all_names[num - 1]}</b>)...",
                )
            elif value == "same":
                mode, label = "same", "Restarting all running agents..."
            else:
                await query.answer("Invalid reboot target.", show_alert=True)
                return
            await query.edit_message_text(label, parse_mode="HTML")
            await query.answer()
            orchestrator.request_restart(mode=mode, agent_name=self.name,
                                          agent_number=int(value) if value.isdigit() else None)
        else:
            await query.answer()

    def _active_keyboard(self) -> InlineKeyboardMarkup:
        status = self.skill_manager.describe_active_heartbeat(self.name) if self.skill_manager else ""
        enabled = "OFF" not in status.upper() and "DISABLED" not in status.upper()
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(selected_label("On", enabled), callback_data="tgl:active:on"),
                InlineKeyboardButton(selected_label("Off", not enabled), callback_data="tgl:active:off"),
            ],
            [
                InlineKeyboardButton("10m", callback_data="tgl:active:10"),
                InlineKeyboardButton("30m", callback_data="tgl:active:30"),
                InlineKeyboardButton("60m", callback_data="tgl:active:60"),
            ],
        ])

    def _active_menu_text(self, *, notice: str | None = None) -> str:
        status = self.skill_manager.describe_active_heartbeat(self.name) if self.skill_manager else "Unavailable"
        facts = [f"<b>Agent</b> · <code>{html.escape(self.name)}</code>"]
        if notice:
            facts.append(f"✅ {html.escape(str(notice))}")
        return setting_card(
            "🫧",
            "Active continuation",
            current=html.escape(status),
            facts=facts,
            consequence="Changes take effect immediately and are stored by the task manager.",
            action="Choose the active state or continuation interval.",
        )

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
            running_names = {rt.name for rt in orchestrator.runtimes}
            lines = [
                card_title("🔄", "Reboot agents"),
                "",
                f"<b>Current</b> · <code>{len(running_names)}</code> running",
                f"<b>Agent</b> · <code>{html.escape(self.name)}</code>",
                "<b>Effect</b> · reloads code and runtime state for the selected target",
                "",
                "Active requests on restarted agents are interrupted. Choose the exact target:",
                "",
                "<b>AGENTS</b>",
            ]
            for i, name in enumerate(all_names, 1):
                running = name in running_names
                marker = "●" if running else "○"
                lines.append(f"{i}. {marker} <code>{html.escape(name)}</code>")
            rows = [
                [
                    InlineKeyboardButton("This bot", callback_data="tgl:reboot:min"),
                    InlineKeyboardButton("All active", callback_data="tgl:reboot:max"),
                ],
                [InlineKeyboardButton("All running", callback_data="tgl:reboot:same")],
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
        elif arg == "same":
            mode, label = "same", "Restarting all running agents..."
        else:
            await self._reply_text(
                update,
                "Invalid reboot target. Use /reboot help, min, max, same, or an agent number.",
            )
            return
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

    def _build_handoff_payload(
        self,
        target_agent: str,
        target_instance: str,
        mode: str,
        *,
        update: Update | None = None,
    ) -> dict[str, Any]:
        builder = (
            runtime_session.session_handoff_builder(self, update=update)
            if update is not None
            else self.handoff_builder
        )
        return runtime_transfer.build_handoff_payload(
            self,
            target_agent,
            target_instance,
            mode,
            handoff_builder=builder,
        )

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
        if action == "transfer":
            delayed = await runtime_pending.delayed_count(self)
            if delayed:
                await self._reply_text(
                    update,
                    f"Transfer is blocked while {delayed} delayed message(s) are pending. "
                    "Use /recall first.",
                )
                return

        target_agent = args[0]
        target_instance = self._normalize_instance_name(args[1] if len(args) > 1 else self._detect_instance_name())
        current_instance = self._normalize_instance_name(self._detect_instance_name())
        if target_agent == self.name and target_instance == current_instance:
            await self._reply_text(update, f"Cannot {action} to the same agent on the same instance.")
            return

        package = self._build_handoff_payload(
            target_agent, target_instance, action, update=update
        )
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
            timeout = aiohttp.ClientTimeout(
                total=None,
                connect=10,
                sock_connect=10,
            )
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
            await self._reply_text(
                update,
                runtime_menu_views.cos_menu_text(enabled=self._cos_enabled),
                parse_mode="HTML",
            )
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

    async def cos_query(self, question: str) -> dict[str, Any]:
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

            response_text = await response_future

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
        from orchestrator import runtime_sys_prompts

        await runtime_sys_prompts.cmd_sys(self, update, context)

    async def callback_sys(self, update, context):
        from orchestrator import runtime_sys_prompts

        await runtime_sys_prompts.callback_sys(self, update, context)

    async def cmd_habit(self, update, context):
        # Resolve lazily so /reboot can replace HER command behaviour without
        # leaving a stale module object attached to the runtime class.
        from orchestrator import runtime_her_habits

        await runtime_her_habits.cmd_habit(self, update, context)

    async def callback_habit(self, update, context):
        from orchestrator import runtime_her_habits

        await runtime_her_habits.callback_habit(self, update, context)

    async def cmd_dream(self, update, context):
        # Keep Dream implementation HER-local and hot-reloadable, matching the
        # adapter-owned /habit command boundary.
        from orchestrator import runtime_her_dream

        await runtime_her_dream.cmd_dream(self, update, context)

    async def callback_dream(self, update, context):
        from orchestrator import runtime_her_dream

        await runtime_her_dream.callback_dream(self, update, context)

    async def _deliver_her_habit_notification(self, job):
        from orchestrator import runtime_her_habits

        return await runtime_her_habits.deliver_habit_notification(self, job)

    async def cmd_usecomputer(self, update, context):
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = [a.strip() for a in (context.args or []) if a.strip()]
        if not args:
            await self._reply_text(
                update,
                get_usecomputer_status(self.sys_prompt_manager),
                parse_mode="HTML",
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
            await self._reply_text(
                update,
                get_usecomputer_status(self.sys_prompt_manager),
                parse_mode="HTML",
            )
            return
        if sub == "examples":
            await self._reply_text(
                update,
                get_usecomputer_examples_text(),
                parse_mode="HTML",
            )
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
        async def reply_browser_status():
            secrets = getattr(self.backend_manager, "secrets", {}) or {}
            secrets_path = getattr(getattr(self, "global_config", None), "secrets_path", None)
            if secrets_path:
                try:
                    with open(secrets_path, "r", encoding="utf-8-sig") as f:
                        latest_secrets = json.load(f)
                    if isinstance(latest_secrets, dict):
                        secrets = latest_secrets
                        self.secrets = latest_secrets
                        if hasattr(self.backend_manager, "secrets"):
                            self.backend_manager.secrets = latest_secrets
                except Exception as e:
                    self.logger.warning("Failed to refresh secrets for /browser status: %s", e)
            active_backend = getattr(self.config, "active_backend", None)
            extension_bridge_configured = Path("/tmp/hashi-browser-bridge.sock").exists()
            await self._reply_text(
                update,
                get_browser_status_text(
                    active_backend=active_backend,
                    brave_configured=bool(secrets.get("brave_api_key")),
                    extension_bridge_configured=extension_bridge_configured,
                ),
                parse_mode="HTML",
            )

        args = [a.strip() for a in (context.args or []) if a.strip()]
        sub = args[0].lower() if args else "status"
        if sub == "status":
            await reply_browser_status()
            return
        if sub == "examples":
            await self._reply_text(update, get_browser_examples_text(), parse_mode="HTML")
            return

        task = " ".join(args[1:]).strip()
        try:
            prompt, source, summary = build_browser_task_prompt(sub, task)
        except ValueError:
            await reply_browser_status()
            return

        await self._reply_text(update, f"Running in /browser route {sub}...")
        await self.enqueue_request(
            update.effective_chat.id,
            prompt,
            source,
            summary,
            habit_learning_eligible=True,
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
        await self._reply_text(
            update,
            runtime_menu_views.credit_status_text(data),
            parse_mode="HTML",
        )

    # ── /safevoice command ─────────────────────────────────────────────────
    async def cmd_safevoice(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = [a.strip().lower() for a in (context.args or []) if a.strip()]
        if not args:
            await self._reply_text(
                update,
                runtime_menu_views.safevoice_menu_text(enabled=self._safevoice_enabled),
                parse_mode="HTML",
                reply_markup=runtime_menu_views.safevoice_keyboard(enabled=self._safevoice_enabled),
            )
            return
        if args[0] == "on":
            self._safevoice_enabled = True
            self._set_skill_state("safevoice", True)
            await self._reply_text(
                update,
                runtime_menu_views.safevoice_menu_text(enabled=True),
                parse_mode="HTML",
                reply_markup=runtime_menu_views.safevoice_keyboard(enabled=True),
            )
        elif args[0] == "off":
            self._safevoice_enabled = False
            self._set_skill_state("safevoice", False)
            runtime_long.discard_pending_voice_confirmations(self)
            self._pending_voice.clear()
            await self._reply_text(
                update,
                runtime_menu_views.safevoice_menu_text(enabled=False),
                parse_mode="HTML",
                reply_markup=runtime_menu_views.safevoice_keyboard(enabled=False),
            )
        else:
            await self._reply_text(update, "Usage: /safevoice on | off")

    async def callback_safevoice(self, update: Update, context: Any):
        query = update.callback_query
        if not self._is_authorized_user(query.from_user.id):
            return
        parts = (query.data or "").split(":", 2)
        action = parts[1] if len(parts) > 1 else ""
        chat_key = parts[2] if len(parts) > 2 else ""
        if action == "set" and chat_key in {"on", "off"}:
            self._safevoice_enabled = chat_key == "on"
            self._set_skill_state("safevoice", self._safevoice_enabled)
            if not self._safevoice_enabled:
                runtime_long.discard_pending_voice_confirmations(self)
                self._pending_voice.clear()
            await query.edit_message_text(
                runtime_menu_views.safevoice_menu_text(enabled=self._safevoice_enabled),
                parse_mode="HTML",
                reply_markup=runtime_menu_views.safevoice_keyboard(enabled=self._safevoice_enabled),
            )
            await query.answer("Safe Voice updated")
            return
        was_long_batch_voice = chat_key in self._long_pending_voice_keys
        pending = self._pending_voice.pop(chat_key, None)
        is_long_batch_voice = was_long_batch_voice or bool(pending and pending.get("long_batch"))
        if action == "yes" and pending:
            if is_long_batch_voice:
                added = runtime_long.resolve_voice_confirmation(self, chat_key, pending)
                if added:
                    await query.edit_message_text(
                        f"✅ Confirmed and added to the active /long batch:\n\n_{pending['transcript']}_",
                        parse_mode="Markdown",
                    )
                    await query.answer("Added to /long batch")
                else:
                    await query.edit_message_text("⏰ The /long batch is no longer active. Voice message discarded.")
                    await query.answer("Expired")
            else:
                await query.edit_message_text(f"✅ Confirmed. Sending to agent:\n\n_{pending['transcript']}_", parse_mode="Markdown")
                await query.answer("Sending...")
                pending_chat_id = int(pending.get("chat_id") or chat_key)
                await self.enqueue_request(
                    pending_chat_id,
                    pending["prompt"],
                    "voice_transcript",
                    pending["summary"],
                )
        elif action == "no":
            if is_long_batch_voice:
                runtime_long.discard_voice_confirmation(self, chat_key)
            await query.edit_message_text("❌ Voice message discarded.")
            await query.answer("Discarded")
        else:
            if is_long_batch_voice:
                runtime_long.discard_voice_confirmation(self, chat_key)
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
                parse_mode="HTML",
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
                parse_mode="HTML",
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
                runtime_menu_views.loop_manager_text(),
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
            await self._reply_text(
                update,
                runtime_menu_views.loop_list_text(loops),
                parse_mode="HTML",
            )
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

    async def cmd_nudge(self, update: Update, context: Any):
        """Create/manage idle-bound nudge tasks."""
        if not self._is_authorized_user(update.effective_user.id):
            return
        raw = (update.message.text or "").strip()
        parts = raw.split(None, 1)
        args_text = parts[1].strip() if len(parts) > 1 else ""
        await runtime_nudge.handle_nudge_command(self, update, args_text)

    async def cmd_superloop(self, update: Update, context: Any):
        """Create/manage recording-first superloops."""
        if not self._is_authorized_user(update.effective_user.id):
            return
        from orchestrator import runtime_superloop

        raw = (update.message.text or "").strip()
        parts = raw.split(None, 1)
        args_text = parts[1].strip() if len(parts) > 1 else ""
        await runtime_superloop.handle_superloop_command(self, update, args_text)

    def _whisper_keyboard(self, current: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(selected_label("Small", current == "small"), callback_data="tgl:whisper:small"),
            InlineKeyboardButton(selected_label("Medium", current == "medium"), callback_data="tgl:whisper:medium"),
            InlineKeyboardButton(selected_label("Large", current.startswith("large")), callback_data="tgl:whisper:large"),
        ]])

    def _whisper_menu_text(self, current: str) -> str:
        return setting_card(
            "🎙️",
            "Whisper transcription",
            current=f"<code>{html.escape(current)}</code>",
            facts=["<b>Scope</b> · local Telegram voice transcription"],
            consequence="Larger models improve accuracy but use more memory and load more slowly.",
            action="The next transcription loads the selected model. This setting does not require a reboot.",
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
            await self._reply_text(
                update,
                self._whisper_menu_text(cur),
                parse_mode="HTML",
                reply_markup=self._whisper_keyboard(cur),
            )
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
            self._whisper_menu_text(new_size),
            parse_mode="HTML",
            reply_markup=self._whisper_keyboard(new_size),
        )

    async def _invoke_prompt_skill_from_command(self, update: Update, skill_id: str, args: list[str]):
        if not self.skill_manager:
            await self._reply_text(update, "Skill system is not configured.")
            return
        skill = self.skill_manager.get_skill(skill_id)
        if skill is None:
            await self._reply_text(update, f"Unknown skill: {skill_id}")
            return
        if not self.skill_manager.is_skill_enabled(self.workspace_dir, skill.id):
            await self._reply_text(
                update,
                f"Skill '{skill.id}' is disabled for this agent. Use /skill enable {skill.id} first.",
            )
            return
        prompt_text = " ".join(args or []).strip()
        if not prompt_text:
            await self._reply_text(update, f"Usage: /{skill_id} <prompt>")
            return
        prompt = self.skill_manager.build_prompt_for_skill(skill, prompt_text)
        await self._reply_text(update, f"Running skill {skill.id}...")
        await self.enqueue_request(
            update.effective_chat.id,
            prompt,
            f"skill:{skill.id}",
            f"Skill {skill.id}",
            skill_id=skill.id,
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
            enabled = "debug" in self.skill_manager.get_active_toggle_ids(self.workspace_dir)
            await self._reply_text(
                update,
                runtime_menu_views.debug_menu_text(enabled=enabled),
                parse_mode="HTML",
            )
            return
        if not self.skill_manager.is_skill_enabled(self.workspace_dir, skill.id):
            await self._reply_text(
                update,
                "Skill 'debug' is disabled for this agent. Use /skill enable debug first.",
            )
            return
        prompt = self.skill_manager.build_prompt_for_skill(skill, prompt_text)
        await self._reply_text(update, f"Running skill {skill.id}...")
        await self.enqueue_request(
            update.effective_chat.id,
            prompt,
            f"skill:{skill.id}",
            f"Skill {skill.id}",
            skill_id=skill.id,
        )

    async def cmd_skill(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = list(context.args or [])
        if args and args[0].strip().casefold() == "dream":
            # Transition compatibility: /skill dream uses the native /dream
            # command and can no longer execute the legacy cross-backend writer.
            from orchestrator import runtime_her_dream

            await runtime_her_dream.cmd_dream(
                self,
                update,
                context,
                args_override=args[1:],
            )
            return
        if not self.skill_manager:
            await self._reply_text(update, "Skill system is not configured.")
            return

        sub = args[0].strip().lower() if args else ""
        if sub in {"cron", "heartbeat"}:
            await self._reply_text(update, "Cron and heartbeat controls moved to /jobs.")
            return
        if sub == "recall":
            rest = " ".join(args[1:]).strip().lower()
            if rest not in {"on", "off"}:
                await self._reply_text(update, "Recall is a runtime setting. Usage: /skill recall on|off")
                return
            _, message = self.skill_manager.set_toggle_state(
                self.workspace_dir,
                "recall",
                enabled=(rest == "on"),
            )
            await self._reply_text(update, message)
            return
        if sub == "debug" and len(args) == 2 and args[1].strip().lower() in {"on", "off"}:
            enabled = args[1].strip().lower() == "on"
            _, message = self.skill_manager.set_toggle_state(
                self.workspace_dir,
                "debug",
                enabled=enabled,
            )
            await self._reply_text(update, message)
            return
        from orchestrator.runtime_skill_commands import handle_standard_skill_command

        async def reply(text: str, **kwargs):
            return await self._reply_text(update, text, **kwargs)

        await handle_standard_skill_command(self, update, args, reply)

    async def cmd_exp(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        task = " ".join(context.args or []).strip()
        if not task:
            await self._reply_text(update, get_exp_usage_text(), parse_mode="HTML")
            return
        prompt = build_exp_task_prompt(task)
        await self._reply_text(update, "Running with EXP guidebook...")
        await self.enqueue_request(
            update.effective_chat.id,
            prompt,
            "exp",
            "EXP-guided task",
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
            from orchestrator import runtime_jobs
            if await runtime_jobs.handle_skill_job_callback(self, query, data):
                return
        if data.startswith("nudgejob:"):
            from orchestrator import runtime_nudge
            if await runtime_nudge.handle_nudge_callback(self, query, data):
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

    def export_daily_transcript(self, cutoff_dt: datetime) -> bool:
        from orchestrator.transcript_export import export_daily_transcript

        return export_daily_transcript(
            self.transcript_log_path,
            self.workspace_dir / "journals",
            cutoff_dt,
        )

    async def _run_job_now(
        self,
        job: dict[str, Any],
        *,
        kind: str | None = None,
    ) -> tuple[bool, str]:
        from orchestrator.job_ownership import ownership_mismatch_label
        from orchestrator.her_v2.request_policy import (
            build_scheduler_request_context,
            infer_scheduler_job_kind,
        )

        mismatch = ownership_mismatch_label(job)
        if mismatch:
            message = f"Refusing to run job {job.get('id')}: {mismatch}."
            await self.send_long_message(
                chat_id=self._primary_chat_id(),
                text=message,
                request_id=f"job-{job.get('id')}",
                purpose="skill-job-run",
            )
            return False, message
        action = job.get("action", "enqueue_prompt")
        if action == "export_transcript":
            exported = self.export_daily_transcript(datetime.now())
            text = "Transcript exported." if exported else "No transcript entries to export."
            await self.send_long_message(
                chat_id=self._primary_chat_id(),
                text=text,
                request_id=f"job-{job.get('id')}",
                purpose="skill-job-run",
            )
            return True, text
        if action in {"her:dream", "skill:dream"}:
            return await self.invoke_her_dream(
                task_id=str(job.get("id") or "manual"),
            )
        if action.startswith("automation:"):
            return await self.invoke_scheduler_automation(
                automation_id=action.split(":", 1)[1],
                args=job.get("args", "") or job.get("prompt", ""),
                task_id=job.get("id", "manual"),
            )
        if action.startswith("skill:"):
            try:
                resolved_kind = infer_scheduler_job_kind(job, kind)
                scheduler_context = build_scheduler_request_context(
                    job,
                    kind=resolved_kind,
                    trigger="manual",
                )
            except ValueError as exc:
                return False, f"Invalid scheduler policy for {job.get('id')}: {exc}"
            return await self.invoke_scheduler_skill(
                skill_id=action.split(":", 1)[1],
                args=job.get("args", "") or job.get("prompt", ""),
                task_id=job.get("id", "manual"),
                scheduler_context=scheduler_context,
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
        try:
            resolved_kind = infer_scheduler_job_kind(job, kind)
            scheduler_context = build_scheduler_request_context(
                job,
                kind=resolved_kind,
                trigger="manual",
            )
        except ValueError as exc:
            return False, f"Invalid scheduler policy for {job.get('id')}: {exc}"
        summary_prefix = (
            "Heartbeat Task" if resolved_kind == "heartbeat" else "Cron Task"
        )
        await self.enqueue_request(
            chat_id=self._primary_chat_id(),
            prompt=prompt,
            source="scheduler",
            summary=f"{summary_prefix} [{job.get('id')}]",
            scheduler_context=scheduler_context,
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
            text, markup = _build_jobs_with_buttons(self, self.name, self.skill_manager, filter_agent=self.name)
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
        await self._run_job_now(job, kind=kind)

    async def cmd_cron(self, update: Update, context: Any):
        await self._handle_job_command(update, "cron", list(context.args or []))

    async def cmd_heartbeat(self, update: Update, context: Any):
        await self._handle_job_command(update, "heartbeat", list(context.args or []))

    async def cmd_status(self, update: Update, context: Any):
        await runtime_status.cmd_status(self, update, context)

    def _verbose_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(selected_label("On", self._verbose), callback_data="tgl:verbose:on"),
            InlineKeyboardButton(selected_label("Off", not self._verbose), callback_data="tgl:verbose:off"),
        ]])

    def _verbose_menu_text(self) -> str:
        backend = getattr(getattr(self, "backend_manager", None), "current_backend", None)
        capabilities = getattr(backend, "capabilities", None)
        progress_available = bool(getattr(capabilities, "supports_progress_stream", False))
        tools_available = bool(getattr(capabilities, "supports_tool_stream", False))
        return setting_card(
            "🔍",
            "Verbose display",
            current=f"<b>{status_label(self._verbose)}</b>",
            facts=[
                f"<b>Progress events</b> · <code>{'AVAILABLE' if progress_available else 'BASIC TIMER ONLY'}</code>",
                f"<b>Tool summaries</b> · <code>{'AVAILABLE' if tools_available else 'NOT EXPOSED'}</code>",
                "<b>Saved</b> · workspace setting",
            ],
            consequence=(
                "Shows a temporary progress card with timing and available tool-result summaries. "
                "Model reasoning and answer drafts stay out of this view."
                if self._verbose
                else "Progress and tool summaries are hidden."
            ),
            action="Changes apply immediately and persist across reboot.",
        )

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
        telegram_stream_policy.set_display_preference(self, "verbose", self._verbose)
        await self._reply_text(
            update,
            self._verbose_menu_text(),
            parse_mode="HTML",
            reply_markup=self._verbose_keyboard(),
        )

    def _think_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(selected_label("On", self._think), callback_data="tgl:think:on"),
            InlineKeyboardButton(selected_label("Off", not self._think), callback_data="tgl:think:off"),
        ]])

    def _think_menu_text(self) -> str:
        backend = getattr(getattr(self, "backend_manager", None), "current_backend", None)
        capabilities = getattr(backend, "capabilities", None)
        reasoning_available = bool(getattr(capabilities, "supports_thinking_stream", False))
        commentary_available = bool(getattr(capabilities, "supports_commentary_stream", False))
        return runtime_menu_views.thinking_output_text(
            enabled=self._think,
            her_backend=self._commentary_available(),
            reasoning_available=reasoning_available,
            commentary_available=commentary_available,
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
        telegram_stream_policy.set_display_preference(self, "think", self._think)
        await self._reply_text(
            update,
            self._think_menu_text(),
            parse_mode="HTML",
            reply_markup=self._think_keyboard(),
        )

    def _commentary_available(self) -> bool:
        return (
            canonical_backend_engine(
                getattr(self.config, "active_backend", "")
            )
            == "her-v2"
        )

    def _set_commentary_enabled(self, enabled: bool) -> None:
        self._commentary = bool(enabled)
        marker = self.workspace_dir / ".commentary_off"
        if self._commentary:
            marker.unlink(missing_ok=True)
        else:
            marker.touch()
        telegram_stream_policy.set_display_preference(
            self,
            "commentary",
            self._commentary,
        )

    def _commentary_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                selected_label("On", self._commentary),
                callback_data="tgl:commentary:on",
            ),
            InlineKeyboardButton(
                selected_label("Off", not self._commentary),
                callback_data="tgl:commentary:off",
            ),
        ]])

    def _commentary_unavailable_text(self) -> str:
        backend = str(getattr(self.config, "active_backend", "unknown") or "unknown")
        return setting_card(
            "🌿",
            "HER commentary",
            current="<b>UNAVAILABLE</b>",
            facts=[
                "<b>Availability</b> · <code>HER ONLY</code>",
                f"<b>Current backend</b> · <code>{html.escape(backend)}</code>",
            ],
            consequence=(
                "This switch is available only for the HER backend. The current backend "
                "continues to follow its own commentary, reasoning, and display rules."
            ),
            action="Nothing was changed.",
        )

    def _commentary_menu_text(self) -> str:
        backend = getattr(getattr(self, "backend_manager", None), "current_backend", None)
        effort = str(getattr(backend, "effort", "unknown") or "unknown").upper()
        return runtime_menu_views.her_commentary_text(
            enabled=self._commentary,
            effort=effort,
        )

    async def cmd_commentary(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        if not self._commentary_available():
            await self._reply_text(
                update,
                self._commentary_unavailable_text(),
                parse_mode="HTML",
            )
            return
        args = [a.strip().lower() for a in (context.args or []) if a.strip()]
        if args and args[0] in {"on", "true", "1"}:
            self._set_commentary_enabled(True)
        elif args and args[0] in {"off", "false", "0"}:
            self._set_commentary_enabled(False)
        await self._reply_text(
            update,
            self._commentary_menu_text(),
            parse_mode="HTML",
            reply_markup=self._commentary_keyboard(),
        )

    def _typing_keyboard(self) -> InlineKeyboardMarkup:
        enabled = telegram_stream_policy.get_display_policy(self).typing_enabled
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(selected_label("On", enabled), callback_data="tgl:typing:on"),
            InlineKeyboardButton(selected_label("Off", not enabled), callback_data="tgl:typing:off"),
        ]])

    def _typing_menu_text(self) -> str:
        policy = telegram_stream_policy.get_display_policy(self)
        return setting_card(
            "⌨️",
            "Telegram typing",
            current=f"<b>{status_label(policy.typing_enabled)}</b>",
            facts=[
                '<b>Temporary bubble</b> · <code>Agent is typing...</code>',
                "<b>Telegram header</b> · native typing indicator",
                f"<b>Source</b> · <code>{html.escape(str(policy.source))}</code>",
                "<b>Saved</b> · workspace setting",
            ],
            consequence=(
                "Both typing indicators appear while a Telegram request is running."
                if policy.typing_enabled
                else "Both typing indicators are hidden. Verbose progress and thinking remain independent."
            ),
            action="Changes apply immediately and persist across reboot.",
        )

    async def cmd_typing(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = [a.strip().lower() for a in (context.args or []) if a.strip()]
        current = telegram_stream_policy.get_display_policy(self).typing_enabled
        if args and args[0] in {"on", "true", "1"}:
            enabled = True
        elif args and args[0] in {"off", "false", "0"}:
            enabled = False
        elif args and args[0] == "status":
            enabled = current
        else:
            enabled = not current
        if not args or args[0] != "status":
            telegram_stream_policy.set_typing_enabled(self, enabled)
        await self._reply_text(
            update,
            self._typing_menu_text(),
            parse_mode="HTML",
            reply_markup=self._typing_keyboard(),
        )

    def _meter_enabled(self) -> bool:
        return bool(
            telegram_stream_policy.get_display_preference(self, "meter", default=False)
        )

    def _meter_keyboard(self) -> InlineKeyboardMarkup:
        enabled = self._meter_enabled()
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(selected_label("On", enabled), callback_data="tgl:meter:on"),
            InlineKeyboardButton(selected_label("Off", not enabled), callback_data="tgl:meter:off"),
        ]])

    def _meter_menu_text(self) -> str:
        enabled = self._meter_enabled()
        return setting_card(
            "💰",
            "Turn cost tail",
            current=f"<b>{status_label(enabled)}</b>",
            facts=[
                "<b>Default</b> · <code>OFF</code>",
                "<b>Scope</b> · foreground turn cost only",
                "<b>Saved</b> · workspace setting",
            ],
            consequence=(
                "A short cost tail is sent after each answer is delivered."
                if enabled
                else "No cost tail is sent after answers."
            ),
            action="Changes apply immediately and persist across reboot.",
        )

    async def cmd_meter(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = [a.strip().lower() for a in (context.args or []) if a.strip()]
        current = self._meter_enabled()
        if len(args) > 1 or (args and args[0] not in {"on", "off", "status"}):
            await self._reply_text(
                update,
                "Usage: <code>/meter [on|off|status]</code>",
                parse_mode="HTML",
            )
            return
        if args and args[0] == "on":
            enabled = True
        elif args and args[0] == "off":
            enabled = False
        else:
            enabled = current
        if args and args[0] in {"on", "off"}:
            telegram_stream_policy.set_display_preference(self, "meter", enabled)
            self._meter = enabled
        await self._reply_text(
            update,
            self._meter_menu_text(),
            parse_mode="HTML",
            reply_markup=self._meter_keyboard(),
        )

    async def cmd_stream(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        await self._reply_text(
            update,
            (
                "📡 <b>Telegram stream menu retired</b>\n\n"
                "Use <code>/typing</code> for the temporary typing bubble and Telegram header, "
                "<code>/verbose</code> for progress and tool summaries, and "
                "<code>/think</code> for model commentary and provider reasoning.\n\n"
                "Answers are delivered once, when complete."
            ),
            parse_mode="HTML",
        )

    async def cmd_preview(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        await self._reply_text(
            update,
            (
                "👁️ <b>Live answer preview retired</b>\n\n"
                "Telegram now receives only the completed answer. Use <code>/verbose</code> "
                "for temporary progress and tool-result summaries."
            ),
            parse_mode="HTML",
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
        text, markup = _build_jobs_with_buttons(self, self.name, self.skill_manager, filter_agent=filter_agent)
        await self._reply_text(update, text, parse_mode="HTML", reply_markup=markup)

    async def cmd_timeout(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        await runtime_timeout.cmd_timeout(self, update, context)

    async def cmd_hchat(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = [a.strip() for a in (context.args or []) if a.strip()]
        if len(args) < 2:
            await self._reply_text(
                update,
                runtime_menu_views.hchat_help_text(),
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
        else:
            self._mark_error(visible_text)
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
        from orchestrator import runtime_groups

        return runtime_groups.group_detail_view(directory, group_name)

    def _group_list_view(self, directory) -> tuple[str, "InlineKeyboardMarkup"]:
        from orchestrator import runtime_groups

        return runtime_groups.group_list_view(directory)

    async def cmd_group(self, update: Update, context: Any):
        from orchestrator import runtime_groups

        await runtime_groups.cmd_group(self, update, context)

    async def callback_group(self, update: Update, context: Any):
        from orchestrator import runtime_groups

        await runtime_groups.callback_group(self, update, context)

    # ── /usage ────────────────────────────────────────────────────────────────

    async def cmd_usage(self, update: Update, context: Any):
        from orchestrator import runtime_usage

        await runtime_usage.cmd_usage(self, update, context)

    async def cmd_token(self, update: Update, context: Any):
        from orchestrator import runtime_usage

        await runtime_usage.cmd_token(self, update, context)

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
        current_mode = self.backend_manager.agent_mode
        if current_mode != "flex":
            await self._reply_text(
                update,
                self._backend_flex_confirmation_text(current_mode),
                parse_mode="HTML",
                reply_markup=self._backend_flex_confirmation_keyboard(current_mode),
            )
            return

        args = context.args
        allowed_engines = [b["engine"] for b in self.config.allowed_backends]

        if not args:
            await self._reply_text(
                update,
                self._build_backend_menu_text(),
                parse_mode="HTML",
                reply_markup=self._backend_keyboard(),
            )
            return

        target_engine = canonical_backend_engine(args[0].lower())
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

        if target_engine == HER_V2_ENGINE:
            if requested_model:
                await self._reply_text(
                    update,
                    "HER v2 does not select a single backend model. "
                    "Use /backend her-v2 [+], then configure Quick/Pro and task "
                    "routes with /model.",
                )
                return
            success, message = await self._switch_backend_mode(
                update.effective_chat.id,
                HER_V2_ENGINE,
                with_context=with_context,
            )
            if not success:
                await self._reply_text(update, message)
                return
            await self._reply_text(
                update,
                runtime_menu_views.her_v2_backend_selected_text(
                    with_context=with_context
                ),
                parse_mode="HTML",
            )
            return

        if requested_model:
            if target_engine == "claude-cli":
                requested_model = CLAUDE_MODEL_ALIASES.get(requested_model.lower(), requested_model)
            available = self._get_available_models_for(target_engine)
            if not allows_custom_models(target_engine) and available and requested_model not in available:
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
            if not success:
                await self._reply_text(update, message)
                return
            text, reply_markup = self._configuration_followup("backend")
            await self._reply_text(update, text, parse_mode="HTML", reply_markup=reply_markup)
            return

        await self._reply_text(
            update,
            self._build_backend_model_prompt(target_engine, with_context),
            parse_mode="HTML",
            reply_markup=self._backend_model_keyboard(target_engine, with_context),
        )

    cmd_provider = runtime_model_selection.cmd_provider

    async def cmd_handoff(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        if self._backend_busy():
            await self._reply_text(update, "Handoff is blocked while a request is running or queued.")
            return

        await self._reply_text(update, "Starting a fresh session with recent bridge history...")
        handoff_builder = runtime_session.session_handoff_builder(self, update=update)
        handoff_builder.refresh_recent_context()
        handoff_builder.build_handoff()
        prompt, exchange_count, word_count = handoff_builder.build_session_restore_prompt(
            max_rounds=10,
            max_words=6000,
        )
        if exchange_count <= 0:
            await self._send_text(update.effective_chat.id, "No recent bridge transcript was available for handoff.")
            return

        self._arm_session_primer(
            "This is a bridge-managed handoff restore. Review AGENT FYI, then use the recent transcript as continuity context.",
            session_id=runtime_session.current_session_for_update(
                self, update
            )["session_id"],
        )
        if self.backend_manager.current_backend and getattr(self.backend_manager.current_backend.capabilities, "supports_sessions", False):
            await self.backend_manager.current_backend.handle_new_session()

        await self._send_text(
            update.effective_chat.id,
            f"Handoff prepared from {exchange_count} recent exchanges ({word_count} words). Restoring continuity now...",
        )
        await self.enqueue_request(
            update.effective_chat.id,
            prompt,
            "handoff",
            f"Handoff restore [{exchange_count} exchanges]",
            skip_memory_injection=True,
        )

    async def cmd_ticket(self, update: Update, context: Any):
        """Submit an IT support ticket to Arale. Usage: /ticket <description>"""
        if not self._is_authorized_user(update.effective_user.id):
            return
        from orchestrator.ticket_manager import (
            create_ticket, format_ticket_notification,
            list_tickets, _resolve_tickets_dir,
        )

        args_text = " ".join(context.args).strip() if context.args else ""

        # /ticket with no args → list open tickets
        if not args_text:
            tickets_dir = _resolve_tickets_dir(self.global_config.project_root)
            open_tickets = list_tickets(tickets_dir, "open")
            ip_tickets = list_tickets(tickets_dir, "in_progress")
            await self._reply_text(
                update,
                runtime_menu_views.ticket_list_text(open_tickets, ip_tickets),
                parse_mode="HTML",
            )
            return

        # Create the ticket (program-driven, no LLM needed)
        instance = str(getattr(self.global_config, "instance_id", None) or "HASHI").upper()
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
                        self.logger.warning(f"Failed to notify arale via bridge: {e}")
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
                    self.logger.info(f"Ticket {ticket['ticket_id']} notified to arale via hchat.")
                else:
                    self.logger.warning(f"Ticket {ticket['ticket_id']} hchat delivery to arale failed. Arale may be offline.")
            except Exception as e:
                self.logger.warning(f"Failed to notify arale via hchat: {e}")

        if not notified:
            self.logger.warning(f"Ticket {ticket['ticket_id']} created but could not notify arale. She will pick it up on next patrol.")

    async def cmd_park(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        args = [a.strip() for a in (context.args or []) if a.strip()]
        if not args:
            await self._reply_text(
                update,
                self._format_parked_topics_text(),
                parse_mode="HTML",
            )
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
        summary = await self._summarize_current_topic_for_parking(
            title_override=title_override, update=update
        )
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
        active_session_id = runtime_session.current_session_for_update(
            self, update
        )["session_id"]
        self._pending_auto_recall_session_id = active_session_id
        self._arm_session_primer(
            f"Loading parked topic [{slot_id}] {title}. Resume it as the active working context.",
            session_id=active_session_id,
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
            await self._reply_text(
                update,
                self._active_menu_text(),
                parse_mode="HTML",
                reply_markup=self._active_keyboard(),
            )
            return

        mode = args[0]
        if mode == "off":
            _, message = self.skill_manager.set_active_heartbeat(self.name, enabled=False)
            await self._reply_text(
                update,
                self._active_menu_text(notice=message),
                parse_mode="HTML",
                reply_markup=self._active_keyboard(),
            )
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
        await self._reply_text(
            update,
            self._active_menu_text(notice=message),
            parse_mode="HTML",
            reply_markup=self._active_keyboard(),
        )


    def _get_available_models(self) -> list[str]:
        if self.config.active_backend == HER_V2_ENGINE:
            selected = self.backend_manager.get_her_v2_configuration()
            option = self.backend_manager._her_v2_provider_option(selected.provider)
            return list(option["models"]) if option and option["available"] else []
        return get_available_models(self.config.active_backend)

    def _get_available_models_for(
        self,
        engine: str,
    ) -> list[str]:
        if engine == HER_V2_ENGINE:
            selected = self.backend_manager.get_her_v2_configuration()
            option = self.backend_manager._her_v2_provider_option(selected.provider)
            return list(option["models"]) if option and option["available"] else []
        models = get_available_models(engine)
        backend_cfg = self._get_backend_cfg(engine)
        if not backend_cfg:
            return models

        # Agent-local model rows extend the shared catalog. This lets one Agent
        # opt into an OpenRouter model without exposing it to every Agent or
        # removing any globally registered choices.
        configured_models: list[object] = []
        raw_models = backend_cfg.get("models")
        if isinstance(raw_models, list):
            configured_models.extend(raw_models)
        configured_models.extend(
            [backend_cfg.get("model"), backend_cfg.get("default_model")]
        )
        for configured_model in configured_models:
            model = str(configured_model or "").strip()
            if model and model not in models:
                models.append(model)
        return models

    def _get_configured_model_for(self, engine: str) -> str | None:
        configured = str(
            (self._get_backend_cfg(engine) or {}).get("model") or ""
        ).strip()
        if configured and configured in self._get_available_models_for(engine):
            return configured
        return normalize_model(engine, configured)

    def _get_available_efforts(self) -> list[str]:
        return get_available_efforts(self.config.active_backend, self.get_current_model())

    def _get_available_efforts_for(self, engine: str, model: str | None = None) -> list[str]:
        return get_available_efforts(engine, model)

    def _get_backend_cfg(
        self,
        engine: str,
    ) -> dict | None:
        candidates = [b for b in self.config.allowed_backends if b["engine"] == engine]
        return next(iter(candidates), None)

    def _get_current_effort(self) -> Optional[str]:
        if self.backend_manager.current_backend:
            effort = getattr(self.backend_manager.current_backend, "effort", None)
            if effort:
                return effort
        backend_cfg = self._get_backend_cfg(self.config.active_backend)
        if backend_cfg:
            return backend_cfg.get("effort")
        return None

    _set_backend_model = runtime_model_selection.set_backend_model

    def _set_active_effort(self, requested: str):
        normalized = normalize_effort(
            self.config.active_backend,
            requested,
            self.get_current_model(),
        )
        if not normalized:
            return
        if self.backend_manager.current_backend and hasattr(self.backend_manager.current_backend, "effort"):
            self.backend_manager.current_backend.effort = normalized
        backend_cfg = self._get_backend_cfg(self.config.active_backend)
        if backend_cfg is not None:
            backend_cfg["effort"] = normalized
        self.backend_manager.persist_state()

    def _backend_flex_confirmation_text(self, current_mode: str) -> str:
        consequences = {
            "fixed": "The persistent CLI-session behavior will stop. Full context injection resumes.",
            "memory+": "Legacy Memory+ mode will move to Flex while Memory+ continuity remains enabled.",
            "wrapper": "The core-and-wrapper response flow will stop, but its configuration is kept.",
            "audit": "The audit follow-up flow will stop, but its configuration is kept.",
            "dual-brain": "The left/right-brain flow will stop, but its configuration is kept.",
        }
        consequence = consequences.get(
            current_mode,
            "The current specialized mode will stop, but its saved configuration is kept.",
        )
        continuity = get_memory_plus_status(self.workspace_dir)
        facts = [f"<b>Backend</b> · <code>{html.escape(self.config.active_backend)}</code>"]
        if continuity["enabled"] or current_mode == "memory+":
            facts.append("<b>Memory+</b> · remains <code>ON</code> after switching")
        return setting_card(
            "🧠",
            "Switch backend",
            current=f"<code>{html.escape(current_mode)}</code>",
            facts=facts,
            consequence=f"Backend switching requires Flex mode. {consequence}",
            action="Switch to Flex and continue to the backend menu, or keep the current mode.",
        )

    def _backend_flex_confirmation_keyboard(self, current_mode: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Switch to Flex and choose backend", callback_data="backend_mode_confirm")],
                [
                    InlineKeyboardButton(
                        f"← Keep {current_mode}",
                        callback_data=f"backend_mode_cancel:{current_mode}",
                    )
                ],
            ]
        )

    _backend_keyboard = runtime_model_selection.backend_keyboard

    def _model_keyboard(self, current_model: Optional[str] = None, engine: Optional[str] = None) -> InlineKeyboardMarkup:
        active_engine = engine or self.config.active_backend
        active = current_model or self.get_current_model()
        buttons = []
        for model in self._get_available_models_for(active_engine):
            label = selected_label(model, model == active)
            buttons.append([InlineKeyboardButton(label, callback_data=f"model:{model}")])
        return InlineKeyboardMarkup(buttons)

    def _effort_keyboard(
        self,
        current_effort: Optional[str] = None,
        *,
        source: str | None = None,
    ) -> InlineKeyboardMarkup:
        active = current_effort or self._get_current_effort()
        buttons = []
        for effort in self._get_available_efforts():
            if self.config.active_backend == HER_V2_ENGINE:
                from orchestrator.her_v2.models import effort_display_label

                visible_effort = effort_display_label(effort)
            else:
                visible_effort = effort
            label = selected_label(visible_effort, effort == active)
            callback_data = f"effort:{source}:{effort}" if source else f"effort:{effort}"
            buttons.append([InlineKeyboardButton(label, callback_data=callback_data)])
        if source in {"backend", "model"}:
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"Keep {active or 'default'}",
                        callback_data=f"effort:{source}:keep",
                    )
                ]
            )
            back_callback = "backend_menu" if source == "backend" else "model_menu"
            buttons.append([InlineKeyboardButton(BACK_LABEL, callback_data=back_callback)])
        return InlineKeyboardMarkup(buttons)

    def _build_effort_followup_text(self) -> str:
        available = self._get_available_efforts()
        current = self._get_current_effort() or (available[0] if available else "n/a")
        if self.config.active_backend == HER_V2_ENGINE:
            consequence = (
                "Direct runs one Quick agent with no orchestration stages. Higher HER "
                "execution modes add Planning, Replanning, independent Review, and "
                "Assured Verification. /effort never changes provider reasoning."
            )
        else:
            consequence = (
                "This optional step controls reasoning depth. If no selection is made, "
                "the current value remains active."
            )
        facts = [
            f"<b>Backend</b> · <code>{html.escape(self.config.active_backend)}</code>",
        ]
        provider = self.get_current_provider()
        if provider:
            facts.append(f"<b>Provider</b> · <code>{html.escape(provider)}</code>")
        if self.config.active_backend == HER_V2_ENGINE:
            selected = self.backend_manager.get_her_v2_configuration()
            facts.extend(
                [
                    f"<b>Quick</b> · <code>{html.escape(selected.fast_model)}</code>",
                    f"<b>Pro</b> · <code>{html.escape(selected.pro_model)}</code>",
                ]
            )
        else:
            facts.append(f"<b>Model</b> · <code>{html.escape(self.get_current_model())}</code>")
        if self.config.active_backend == HER_V2_ENGINE:
            from orchestrator.her_v2.models import effort_display_label

            current_display = effort_display_label(current)
            title = "HER execution mode"
            action = "Choose an execution mode, or keep the current value."
        else:
            current_display = current
            title = "Choose effort"
            action = "Choose an effort level, or keep the current value."
        return setting_card(
            "🎛️",
            title,
            current=f"<code>{html.escape(current_display)}</code>",
            facts=facts,
            consequence=consequence,
            action=action,
        )

    def _build_model_configuration_summary(self) -> str:
        if self.config.active_backend == HER_V2_ENGINE:
            return runtime_model_selection.her_v2_model_menu_text(self)
        effort = self._get_current_effort() if self._get_available_efforts() else None
        lines = [
            card_title("✅", "Model configuration"),
            "",
            f"<b>Mode</b> · <code>{html.escape(self.backend_manager.agent_mode)}</code>",
            f"<b>Backend</b> · <code>{html.escape(self.config.active_backend)}</code>",
        ]
        provider = self.get_current_provider()
        if provider:
            lines.append(f"<b>Provider</b> · <code>{html.escape(provider)}</code>")
        lines.extend(
            [
                f"<b>Model</b> · <code>{html.escape(self.get_current_model())}</code>",
                f"<b>Effort</b> · <code>{html.escape(effort or 'n/a')}</code>",
                "",
                "Configuration saved and active immediately.",
            ]
        )
        return "\n".join(lines)

    def _configuration_followup(self, source: str) -> tuple[str, InlineKeyboardMarkup | None]:
        available = self._get_available_efforts()
        if not available:
            return self._build_model_configuration_summary(), None
        current = self._get_current_effort()
        if not current:
            self._set_active_effort(available[0])
            current = self._get_current_effort() or available[0]
        return self._build_effort_followup_text(), self._effort_keyboard(current, source=source)

    def _backend_model_keyboard(
        self,
        target_engine: str,
        with_context: bool,
        current_model: Optional[str] = None,
    ) -> InlineKeyboardMarkup:
        active_model = current_model or self._get_configured_model_for(target_engine)
        mode_flag = "c" if with_context else "p"
        buttons = []
        for model in self._get_available_models_for(target_engine):
            label = selected_label(model, model == active_model)
            buttons.append([InlineKeyboardButton(label, callback_data=f"bmodel:{target_engine}:{mode_flag}:{model}")])
        buttons.append([InlineKeyboardButton(BACK_LABEL, callback_data="backend_menu")])
        return InlineKeyboardMarkup(buttons)

    def _build_backend_menu_text(self) -> str:
        return runtime_menu_views.backend_menu_text(active_backend=self.config.active_backend)

    def _build_backend_model_prompt(self, target_engine: str, with_context: bool) -> str:
        current_model = self._get_configured_model_for(target_engine)
        return runtime_menu_views.backend_model_prompt_text(
            backend=target_engine,
            current_model=current_model,
            with_context=with_context,
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
        target_provider: str | None = None,
        with_context: bool = False,
    ) -> tuple[bool, str]:
        allowed_engines = [b["engine"] for b in self.config.allowed_backends]
        if target_engine not in allowed_engines:
            return False, f"Backend not allowed: {target_engine}"

        policy = self._evaluate_enterprise_policy(
            "backend.switch",
            resource=f"backend:{target_engine}",
            target_backend=target_engine,
            target_model=target_model,
            target_provider=target_provider,
            with_context=with_context,
        )
        if not policy.allowed:
            if policy.decision.value == "approval_required":
                return False, f"Backend switch requires approval: {target_engine}"
            return False, f"Backend switch blocked by policy: {target_engine}"

        if self._backend_busy():
            return False, "Backend switch blocked while a request is running or queued."

        selected_session = runtime_session.current_session(
            self, surface="telegram", channel_key=str(chat_id)
        )
        workzone_value = str(selected_session.get("workzone") or "").strip()
        self._workzone_dir = Path(workzone_value) if workzone_value else None
        self._sync_workzone_to_backend_config()
        switch_ok = await self.backend_manager.switch_backend(
            target_engine,
            target_model=target_model,
            target_provider=target_provider,
        )
        if not switch_ok:
            return False, f"Failed to switch backend to: {target_engine}"
        self._sync_workzone_to_backend_config()
        backend = self.backend_manager.current_backend
        supports_sessions = bool(
            backend and getattr(getattr(backend, "capabilities", None), "supports_sessions", False)
        )
        if backend and hasattr(backend, "set_session_mode"):
            # Every backend switch is a one-shot/Flex-style transition. Fixed
            # mode never switches backend in place; it must be left first.
            backend.set_session_mode(False)
        if backend and supports_sessions:
            await backend.handle_new_session()

        if with_context:
            with suppress(Exception):
                handoff_builder = runtime_session.session_handoff_builder(
                    self, surface="telegram", channel_key=str(chat_id)
                )
                handoff_builder.refresh_recent_context()
                handoff_builder.build_handoff()
        else:
            self._clear_handoff_state()
        self.last_backend_switch_at = datetime.now()

        primer_note = (
            f"Backend switched to {target_engine} with continuity handoff available."
            if with_context
            else f"Backend switched to {target_engine}. Review AGENT FYI before the next task."
        )
        self._arm_session_primer(
            primer_note, session_id=selected_session["session_id"]
        )

        # A continuation request is an actual one-time backend delivery, not a
        # handoff file that merely waits for a future user message. Stateless
        # Flex targets already receive history every turn and need no package.
        if with_context and supports_sessions:
            restore_prompt, exchange_count, _word_count = (
                runtime_session.session_handoff_builder(
                    self, surface="telegram", channel_key=str(chat_id)
                ).build_session_restore_prompt(
                    max_rounds=10,
                    max_words=6000,
                )
            )
            if exchange_count:
                await self.enqueue_request(
                    chat_id,
                    restore_prompt,
                    "handoff",
                    f"Backend continuation [{exchange_count} exchanges]",
                    silent=True,
                    deliver_to_telegram=False,
                    skip_memory_injection=True,
                )

        model = self.get_current_model()
        provider = self.get_current_provider()
        effort = self._get_current_effort()
        mode_text = "with handoff context" if with_context else "without handoff context"
        message = f"Backend switched to: {target_engine}\n"
        if provider:
            message += f"Provider: {provider}\n"
        message += f"Model: {model}\nMode: {mode_text}"
        if effort:
            message += f"\nEffort: {effort}"
        return True, message

    cmd_model = runtime_model_selection.cmd_model

    async def cmd_effort(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        if not self.backend_manager.current_backend:
            return

        available = self._get_available_efforts()
        if not available:
            await self._reply_text(update, "Effort control is not available for the active backend/model.")
            return

        args = context.args
        if args:
            requested = args[0].strip().lower()
            if requested == "extra":
                requested = "extra_high"
            if self.config.active_backend == HER_V2_ENGINE:
                from orchestrator.her_v2.models import parse_effort

                try:
                    requested = parse_effort(requested).value
                except ValueError:
                    pass
            if requested not in available:
                await self._reply_text(update, f"Unknown effort level: {requested}\nAvailable: {', '.join(available)}")
                return
            self._set_active_effort(requested)
            if self.config.active_backend == HER_V2_ENGINE:
                from orchestrator.her_v2.models import effort_display_label

                switched = f"HER execution mode switched to: {effort_display_label(requested)}"
            else:
                switched = f"Effort switched to: {requested}"
            await self._reply_text(update, switched)
            return

        current_effort = self._get_current_effort() or available[0]
        if self.config.active_backend == HER_V2_ENGINE:
            consequence = (
                "Direct runs one Quick agent with no orchestration stages. Higher HER "
                "execution modes add Planning, Replanning, independent Review, and "
                "Assured Verification. /effort does not change provider reasoning."
            )
        else:
            consequence = (
                "Higher effort may improve difficult work but takes longer and can use more tokens."
            )
        if self.config.active_backend == HER_V2_ENGINE:
            selected = self.backend_manager.get_her_v2_configuration()
            effort_facts = [
                f"<b>Quick</b> · <code>{html.escape(selected.fast_model)}</code>",
                f"<b>Pro</b> · <code>{html.escape(selected.pro_model)}</code>",
            ]
        else:
            effort_facts = [
                f"<b>Model</b> · <code>{html.escape(self.get_current_model())}</code>"
            ]
        if self.config.active_backend == HER_V2_ENGINE:
            from orchestrator.her_v2.models import effort_display_label

            effort_title = "HER execution mode"
            current_display = effort_display_label(current_effort)
        else:
            effort_title = "Model effort"
            current_display = current_effort
        await self._reply_text(
            update,
            setting_card(
                "🎛️",
                effort_title,
                current=f"<code>{html.escape(current_display)}</code>",
                facts=effort_facts,
                consequence=consequence,
                action="The selection applies immediately and persists.",
            ),
            parse_mode="HTML",
            reply_markup=self._effort_keyboard(current_effort),
        )

    def _is_wrapper_mode(self) -> bool:
        return getattr(self.backend_manager, "agent_mode", "flex") == "wrapper"

    def _is_audit_mode(self) -> bool:
        return getattr(self.backend_manager, "agent_mode", "flex") == "audit"

    def _is_dual_brain_mode(self) -> bool:
        return getattr(self.backend_manager, "agent_mode", "flex") == "dual-brain"

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
        if not allows_custom_models(engine) and available and model not in available:
            return f"Unknown model for {engine}: {model}"
        return None

    def _wrapper_core_keyboard(self, cfg) -> InlineKeyboardMarkup:
        models = [
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-5.5",
            "gpt-5.3-codex-spark",
            "gpt-5.4",
            "gpt-5.3-codex",
        ]
        rows: list[list[InlineKeyboardButton]] = []
        for model in models:
            label = selected_label(
                model,
                cfg.core_backend == "codex-cli" and cfg.core_model == model,
            )
            rows.append([InlineKeyboardButton(label, callback_data=f"wcfg:core:codex-cli:{model}")])
        rows.append([
            InlineKeyboardButton("Wrapper model", callback_data="wcfg:menu:wrap"),
            InlineKeyboardButton(BACK_LABEL, callback_data="wcfg:menu:wrapper"),
        ])
        return InlineKeyboardMarkup(rows)

    def _wrapper_model_choices(self) -> list[tuple[str, str, str, str]]:
        return [
            ("claude_haiku", "Claude Haiku", "claude-cli", "claude-haiku-4-5"),
            ("claude_sonnet", "Claude Sonnet", "claude-cli", "claude-sonnet-4-6"),
            ("gemini_flash", "Gemini Flash", "gemini-cli", "gemini-2.5-flash"),
            ("gemini_lite", "Gemini Lite", "gemini-cli", "gemini-2.5-flash-lite"),
            ("deepseek_flash", "DeepSeek Flash", "deepseek-api", "deepseek-v4-flash"),
            ("deepseek_pro", "DeepSeek Pro", "deepseek-api", "deepseek-v4-pro"),
            ("or_deepseek", "OR DeepSeek Flash", "openrouter-api", "deepseek/deepseek-v4-flash"),
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
            ["deepseek_flash", "deepseek_pro"],
            ["or_deepseek", "or_gemini"],
        ]
        for group in grouped_rows:
            row: list[InlineKeyboardButton] = []
            for choice_id in group:
                label, backend, model = choices[choice_id]
                active = cfg.wrapper_backend == backend and cfg.wrapper_model == model
                row.append(
                    InlineKeyboardButton(
                        selected_label(label, active),
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
            InlineKeyboardButton(BACK_LABEL, callback_data="wcfg:menu:wrapper"),
        ])
        return InlineKeyboardMarkup(rows)

    def _wrapper_status_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Core model", callback_data="wcfg:menu:core"),
                    InlineKeyboardButton("Wrapper model", callback_data="wcfg:menu:wrap"),
                ],
                [InlineKeyboardButton(REFRESH_LABEL, callback_data="wcfg:menu:wrapper")],
            ]
        )

    def _wrapper_core_text(self, cfg) -> str:
        return setting_card(
            "🧠",
            "Wrapper core model",
            current=f"<code>{html.escape(cfg.core_backend)} / {html.escape(cfg.core_model)}</code>",
            facts=["<b>Role</b> · performs the task before response translation"],
            consequence="Changing this selection updates the active core model used in Wrapper mode.",
            action=(
                "Choose a model below or use "
                "<code>/core backend=codex-cli model=gpt-5.6-sol</code>."
            ),
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
        backend: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        return runtime_audit.audit_block_with(
            cfg,
            delivery=delivery,
            severity_threshold=severity_threshold,
            backend=backend,
            model=model,
        )

    def _wrapper_wrap_text(self, cfg) -> str:
        return setting_card(
            "🎭",
            "Wrapper translator model",
            current=f"<code>{html.escape(cfg.wrapper_backend)} / {html.escape(cfg.wrapper_model)}</code>",
            facts=[
                f"<b>Context window</b> · <code>{cfg.context_window}</code> visible turns",
                f"<b>Fallback</b> · <code>{html.escape(cfg.fallback)}</code>",
                "<b>Recommended</b> · <code>claude-cli / claude-haiku-4-5</code>",
            ],
            consequence="Model choices update backend and model together; context buttons only change visible history.",
            action=(
                "Choose below or use "
                "<code>/wrap backend=&lt;backend&gt; model=&lt;model&gt; context=3</code>."
            ),
        )

    def _wrapper_status_text(self, state: dict, slots: dict) -> str:
        cfg = load_wrapper_config(state)
        visible_slots = visible_wrapper_slots(slots)
        lines = [
            card_title("🎭", "Wrapper configuration"),
            "",
            f"<b>Current</b> · core <code>{html.escape(cfg.core_backend)} / {html.escape(cfg.core_model)}</code>",
            f"<b>Wrapper</b> · <code>{html.escape(cfg.wrapper_backend)} / {html.escape(cfg.wrapper_model)}</code>",
            f"<b>Context window</b> · <code>{cfg.context_window}</code> visible turns",
            f"<b>Slots</b> · <code>{len(visible_slots)}</code> configured",
            "",
            "Model changes apply immediately in Wrapper mode; persona/style slot changes persist.",
            "",
            "<b>PERSONA / STYLE SLOTS</b>",
        ]
        if visible_slots:
            for key in sorted(visible_slots, key=lambda value: (not str(value).isdigit(), int(value) if str(value).isdigit() else str(value))):
                lines.append(f"• <code>{html.escape(str(key))}</code>: {html.escape(str(visible_slots[key]))}")
        else:
            lines.append("• none")
        lines.extend(
            [
                "",
                "Choose a model page below or use <code>/wrapper set &lt;slot&gt; &lt;text&gt;</code>.",
            ]
        )
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
                parse_mode="HTML",
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
                parse_mode="HTML",
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
                    parse_mode="HTML",
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

        if action in {"delivery", "threshold"}:
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
            self.backend_manager.update_audit_blocks(audit=audit_block)
            result_key = {"delivery": "delivery", "threshold": "severity_threshold"}[action]
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

    def _dual_brain_config(self):
        return load_dual_brain_config(
            self.backend_manager.get_state_snapshot(),
            current_backend=getattr(self.config, "active_backend", ""),
            current_model=self.get_current_model(),
        )

    def _dual_brain_status_keyboard(self, cfg) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Left brain", callback_data="bcfg:menu:left"),
                    InlineKeyboardButton("Right brain", callback_data="bcfg:menu:right"),
                ],
                [
                    InlineKeyboardButton("Prompts", callback_data="bcfg:menu:prompts"),
                    InlineKeyboardButton(REFRESH_LABEL, callback_data="bcfg:menu:status"),
                ],
            ]
        )

    def _dual_brain_allowed_backend_ids(self) -> list[str]:
        return [
            str(backend.get("engine"))
            for backend in self.config.allowed_backends
            if backend.get("engine")
        ]

    def _dual_brain_backend_keyboard(self, cfg, *, target: str) -> InlineKeyboardMarkup:
        current_backend = cfg.left_backend if target == "left" else cfg.right_backend
        rows: list[list[InlineKeyboardButton]] = []
        engines = self._dual_brain_allowed_backend_ids()
        for i in range(0, len(engines), 2):
            row: list[InlineKeyboardButton] = []
            for engine in engines[i : i + 2]:
                label = get_backend_label(engine)
                if engine == current_backend:
                    label = selected_label(label, True)
                row.append(InlineKeyboardButton(label, callback_data=f"bcfg:backend:{target}:{engine}"))
            rows.append(row)
        rows.append([InlineKeyboardButton(BACK_LABEL, callback_data="bcfg:menu:status")])
        return InlineKeyboardMarkup(rows)

    def _dual_brain_model_keyboard(self, cfg, *, target: str, backend: str | None = None) -> InlineKeyboardMarkup:
        current_backend = cfg.left_backend if target == "left" else cfg.right_backend
        current_model = cfg.left_model if target == "left" else cfg.right_model
        selected_backend = backend or current_backend
        models = self._get_available_models_for(selected_backend)
        rows: list[list[InlineKeyboardButton]] = []
        for index, model in enumerate(models):
            active = selected_backend == current_backend and model == current_model
            label = selected_label(model, active)
            rows.append([InlineKeyboardButton(label, callback_data=f"bcfg:modelidx:{target}:{selected_backend}:{index}")])
        rows.append([InlineKeyboardButton("← Back to backends", callback_data=f"bcfg:menu:{target}")])
        rows.append([InlineKeyboardButton(BACK_LABEL, callback_data="bcfg:menu:status")])
        return InlineKeyboardMarkup(rows)

    def _dual_brain_status_text(self, cfg) -> str:
        memory_prompt = "default" if cfg.left_prompt == DEFAULT_LEFT_PROMPT else "custom"
        notepad_prompt = "default" if cfg.after_action_prompt == DEFAULT_AFTER_ACTION_PROMPT else "custom"
        return setting_card(
            "🧠",
            "Dual-brain configuration",
            current=f"<b>{html.escape(str(self.backend_manager.agent_mode).upper())}</b>",
            facts=[
                f"<b>Left brain</b> · <code>{html.escape(cfg.left_backend)} / {html.escape(cfg.left_model)}</code>",
                f"<b>Right brain</b> · <code>{html.escape(cfg.right_backend)} / {html.escape(cfg.right_model)}</code>",
                f"<b>Memory briefing prompt</b> · <code>{memory_prompt}</code>",
                f"<b>Notepad update prompt</b> · <code>{notepad_prompt}</code>",
            ],
            consequence="Left-brain memory work and right-brain execution remain separate; <code>/sys</code> applies to the right brain.",
            action="Choose a brain or prompt page below. Advanced text control remains available through <code>/brain</code>.",
        )

    def _dual_brain_model_text(self, cfg, *, target: str) -> str:
        backend = cfg.left_backend if target == "left" else cfg.right_backend
        model = cfg.left_model if target == "left" else cfg.right_model
        label = "Left brain" if target == "left" else "Right brain"
        return setting_card(
            "🧠",
            f"{label} backend",
            current=f"<code>{html.escape(backend)} / {html.escape(model)}</code>",
            facts=[f"<b>Target</b> · <code>{target}</code> brain"],
            consequence="Choose a backend first; the next page shows its available HASHI models.",
            action=f"Choose below or use <code>/brain {target} backend=&lt;backend&gt; model=&lt;model&gt;</code>.",
        )

    def _dual_brain_backend_model_text(self, cfg, *, target: str, backend: str) -> str:
        current_backend = cfg.left_backend if target == "left" else cfg.right_backend
        current_model = cfg.left_model if target == "left" else cfg.right_model
        label = "Left brain" if target == "left" else "Right brain"
        active = "current" if backend == current_backend else "not current"
        return setting_card(
            "🧠",
            f"{label} model",
            current=f"<code>{html.escape(current_backend)} / {html.escape(current_model)}</code>",
            facts=[
                f"<b>Selected backend</b> · <code>{html.escape(backend)}</code>",
                f"<b>Backend state</b> · <code>{active.upper()}</code>",
            ],
            consequence="Choosing a model saves the backend and model together for this brain.",
            action="Choose a model below.",
        )

    def _dual_brain_prompts_text(self, cfg) -> str:
        def section(title: str, command_target: str, value: str) -> str:
            text = value or "(empty)"
            max_chars = 1100
            if len(text) > max_chars:
                text = (
                    text[:max_chars]
                    + f"\n... [truncated; use /brain prompt {command_target} show for full text]"
                )
            return f"<b>{title}</b> · <code>{len(value or '')}</code> chars\n<pre>{html.escape(text)}</pre>"

        return (
            f"{card_title('🧠', 'Dual-brain prompts')}\n\n"
            "<b>Current</b> · memory briefing and notepad update prompts\n"
            "<b>Scope</b> · left-brain pre-turn memory and post-turn continuity\n\n"
            f"{section('Memory briefing prompt', 'memory', cfg.left_prompt)}\n\n"
            f"{section('Notepad update prompt', 'notepad', cfg.after_action_prompt)}\n\n"
            "<b>Use</b>\n"
            "<code>/brain prompt memory &lt;text&gt;</code> · replace the briefing prompt\n"
            "<code>/brain prompt notepad &lt;text&gt;</code> · replace the update prompt\n"
            "<code>/brain prompt memory|notepad show|clear</code> · inspect or clear\n\n"
            "Aliases <code>left</code> and <code>after</code> remain supported."
        )

    async def cmd_brain(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        ensure_dual_brain_observer(self.workspace_dir)
        self.reload_post_turn_observers()
        cfg = self._dual_brain_config()
        args = [a.strip() for a in (context.args or []) if a.strip()]
        if not args or args[0].lower() in {"status", "menu", "config"}:
            await self._reply_text(
                update,
                self._dual_brain_status_text(cfg),
                parse_mode="HTML",
                reply_markup=self._dual_brain_status_keyboard(cfg),
            )
            return

        action = args[0].lower()
        if action in {"prompts", "prompt"} and len(args) == 1:
            await self._reply_text(
                update,
                self._dual_brain_prompts_text(cfg),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(BACK_LABEL, callback_data="bcfg:menu:status")]]),
            )
            return

        if action in {"left", "right"}:
            values = _parse_key_values(args[1:])
            backend = values.get("backend") or (args[1] if len(args) > 1 and "=" not in args[1] else "")
            model = values.get("model") or (args[2] if len(args) > 2 and "=" not in args[2] else "")
            if not backend or not model:
                await self._reply_text(update, f"Usage: /brain {action} backend=<backend> model=<model>")
                return
            backend = backend.strip().lower()
            model = self._normalize_wrapper_model(backend, model.strip())
            error = self._validate_wrapper_backend_model(backend, model)
            if error:
                await self._reply_text(update, error)
                return
            block = (
                dual_brain_block_with(cfg, left_backend=backend, left_model=model)
                if action == "left"
                else dual_brain_block_with(cfg, right_backend=backend, right_model=model)
            )
            self.backend_manager.update_dual_brain_block(block)
            if action == "right" and self._is_dual_brain_mode():
                switch_ok, switch_message = await self._activate_wrapper_core_backend(
                    update.effective_chat.id,
                    backend=backend,
                    model=model,
                )
                if not switch_ok:
                    await self._reply_text(update, f"Right brain saved but active backend switch failed: {switch_message}")
                    return
            await self._reply_text(update, f"{action.title()} brain model saved: `{backend}` / `{model}`", parse_mode="Markdown")
            return

        if action == "prompt":
            if len(args) < 3:
                await self._reply_text(update, "Usage: /brain prompt memory|notepad <text|show|clear>")
                return
            target = args[1].lower()
            prompt_aliases = {
                "left": "left",
                "memory": "left",
                "briefing": "left",
                "after": "after_action",
                "after_action": "after_action",
                "notepad": "after_action",
                "update": "after_action",
            }
            key = prompt_aliases.get(target)
            if key is None:
                await self._reply_text(update, "Prompt target must be memory or notepad. Use /sys for right-brain execution instructions.")
                return
            sub = args[2].lower()
            current = {
                "left": cfg.left_prompt,
                "after_action": cfg.after_action_prompt,
            }[key]
            if sub == "show":
                await self._reply_text(update, current or "(empty)", parse_mode=None)
                return
            new_prompt = "" if sub == "clear" else " ".join(args[2:]).strip()
            block = dual_brain_block_with(
                cfg,
                left_prompt=new_prompt if key == "left" else None,
                after_action_prompt=new_prompt if key == "after_action" else None,
            )
            self.backend_manager.update_dual_brain_block(block)
            await self._reply_text(update, f"Dual-brain {key} prompt updated ({len(new_prompt)} chars).")
            return

        await self._reply_text(
            update,
            "Usage: /brain [status|left backend=<backend> model=<model>|right backend=<backend> model=<model>|prompt memory|notepad <text|show|clear>]",
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
                    parse_mode="HTML",
                    reply_markup=self._audit_core_keyboard(cfg),
                )
            elif data == "acfg:menu:auditmodel":
                await query.edit_message_text(
                    self._audit_auditor_text(cfg),
                    parse_mode="HTML",
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

    async def callback_brain_config(self, update: Update, context: Any):
        query = update.callback_query
        if not self._is_authorized_user(query.from_user.id):
            return
        data = query.data or ""
        try:
            ensure_dual_brain_observer(self.workspace_dir)
            self.reload_post_turn_observers()
            cfg = self._dual_brain_config()
            if data == "bcfg:menu:status":
                await query.edit_message_text(
                    self._dual_brain_status_text(cfg),
                    parse_mode="HTML",
                    reply_markup=self._dual_brain_status_keyboard(cfg),
                )
            elif data == "bcfg:menu:left":
                await query.edit_message_text(
                    self._dual_brain_model_text(cfg, target="left"),
                    parse_mode="HTML",
                    reply_markup=self._dual_brain_backend_keyboard(cfg, target="left"),
                )
            elif data == "bcfg:menu:right":
                await query.edit_message_text(
                    self._dual_brain_model_text(cfg, target="right"),
                    parse_mode="HTML",
                    reply_markup=self._dual_brain_backend_keyboard(cfg, target="right"),
                )
            elif data == "bcfg:menu:prompts":
                await query.edit_message_text(
                    self._dual_brain_prompts_text(cfg),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(BACK_LABEL, callback_data="bcfg:menu:status")]]),
                )
            elif data.startswith("bcfg:backend:"):
                parts = data.split(":", 3)
                if len(parts) != 4:
                    await query.answer("Invalid backend selection.", show_alert=True)
                    return
                _, _, target, backend = parts
                if target not in {"left", "right"}:
                    await query.answer("Invalid brain target.", show_alert=True)
                    return
                backend = backend.strip()
                if not self._allowed_wrapper_engine(backend):
                    await query.answer(f"Backend not allowed: {backend}", show_alert=True)
                    return
                if not self._get_available_models_for(backend):
                    await query.answer(f"No registered models for {backend}.", show_alert=True)
                    return
                await query.edit_message_text(
                    self._dual_brain_backend_model_text(cfg, target=target, backend=backend),
                    parse_mode="HTML",
                    reply_markup=self._dual_brain_model_keyboard(cfg, target=target, backend=backend),
                )
            elif data.startswith("bcfg:modelidx:"):
                parts = data.split(":", 4)
                if len(parts) != 5:
                    await query.answer("Invalid model selection.", show_alert=True)
                    return
                _, _, target, backend, raw_index = parts
                if target not in {"left", "right"}:
                    await query.answer("Invalid brain target.", show_alert=True)
                    return
                models = self._get_available_models_for(backend)
                try:
                    model = models[int(raw_index)]
                except (ValueError, IndexError):
                    await query.answer("Unknown model choice.", show_alert=True)
                    return
                error = self._validate_wrapper_backend_model(backend, model)
                if error:
                    await query.answer(error, show_alert=True)
                    return
                block = (
                    dual_brain_block_with(cfg, left_backend=backend, left_model=model)
                    if target == "left"
                    else dual_brain_block_with(cfg, right_backend=backend, right_model=model)
                )
                self.backend_manager.update_dual_brain_block(block)
                if target == "right" and self._is_dual_brain_mode():
                    switch_ok, switch_message = await self._activate_wrapper_core_backend(
                        query.message.chat_id,
                        backend=backend,
                        model=model,
                    )
                    if not switch_ok:
                        await query.answer(f"Saved, but switch failed: {switch_message}", show_alert=True)
                refreshed = self._dual_brain_config()
                await query.edit_message_text(
                    self._dual_brain_backend_model_text(refreshed, target=target, backend=backend),
                    parse_mode="HTML",
                    reply_markup=self._dual_brain_model_keyboard(refreshed, target=target, backend=backend),
                )
            else:
                await query.answer("Unknown dual-brain control.", show_alert=True)
                return
        except Exception as e:
            self.error_logger.error(f"callback_brain_config error: {e}", exc_info=True)
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
                    parse_mode="HTML",
                    reply_markup=self._wrapper_core_keyboard(cfg),
                )
            elif data == "wcfg:menu:wrap":
                await query.edit_message_text(
                    self._wrapper_wrap_text(cfg),
                    parse_mode="HTML",
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
        await runtime_model_selection.callback_model(self, update, context)

    async def cmd_mode(self, update: Update, context: Any):
        await runtime_mode.cmd_mode(self, update, context)

    async def cmd_privacy(self, update: Update, context: Any):
        await runtime_privacy.cmd_privacy(self, update, context)

    async def callback_privacy(self, update: Update, context: Any):
        await runtime_privacy.callback_privacy(self, update, context)

    async def cmd_workzone(self, update: Update, context: Any):
        await runtime_workzone.cmd_workzone(self, update, context)

    async def cmd_new(self, update: Update, context: Any):
        await runtime_session.cmd_new(self, update, context)

    async def cmd_fresh(self, update: Update, context: Any):
        await runtime_session.cmd_fresh(self, update, context)

    async def cmd_sessions(self, update: Update, context: Any):
        await runtime_session.cmd_sessions(self, update, context)

    async def cmd_use(self, update: Update, context: Any):
        await runtime_session.cmd_use(self, update, context)

    async def cmd_current(self, update: Update, context: Any):
        await runtime_session.cmd_current(self, update, context)

    async def cmd_archive(self, update: Update, context: Any):
        await runtime_session.cmd_archive(self, update, context)

    async def cmd_promote(self, update: Update, context: Any):
        await runtime_session.cmd_promote(self, update, context)

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

    async def cmd_notepad(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            return
        workspace = self._notepad_workspace(update)
        args = [str(arg) for arg in (context.args or [])]
        action = (args[0].strip().lower() if args else "show")
        if action in {"show", "status", "view", "today"}:
            text, markup = self._notepad_view_payload(workspace)
            await self._reply_text(
                update,
                text,
                parse_mode="HTML",
                reply_markup=markup,
            )
            return

        if action == "carryover":
            text, markup = self._notepad_carryover_payload(workspace)
            await self._reply_text(update, text, parse_mode="HTML", reply_markup=markup)
            return

        if action == "history":
            text, markup = self._notepad_history_payload(workspace)
            await self._reply_text(update, text, parse_mode="HTML", reply_markup=markup)
            return

        if action == "find":
            query_text = self._notepad_command_tail(update, context, action)
            if not query_text:
                await self._reply_text(
                    update,
                    self._notepad_help_text("find"),
                    parse_mode="HTML",
                    reply_markup=self._notepad_back_keyboard(),
                )
                return
            text, markup = self._notepad_find_payload(query_text, workspace)
            await self._reply_text(update, text, parse_mode="HTML", reply_markup=markup)
            return

        if action == "compact":
            compact_memory_plus(workspace)
            text, markup = self._notepad_view_payload(workspace)
            await self._reply_text(
                update,
                "✅ Memory+ card compacted and de-duplicated.\n\n" + text,
                parse_mode="HTML",
                reply_markup=markup,
            )
            return

        if action in {"edit", "add", "append"}:
            text = self._notepad_command_tail(update, context, action)
            if not text:
                await self._reply_text(
                    update,
                    self._notepad_help_text("edit"),
                    parse_mode="HTML",
                    reply_markup=self._notepad_back_keyboard(),
                )
                return
            path = append_memory_plus_manual_note(workspace, text)
            await self._reply_text(
                update,
                f"✅ Memory+ notepad updated.\nPath: <code>{html.escape(str(path))}</code>",
                parse_mode="HTML",
                reply_markup=self._notepad_keyboard(),
            )
            return

        if action == "replace":
            text = self._notepad_command_tail(update, context, action)
            if not text:
                await self._reply_text(
                    update,
                    self._notepad_help_text("replace"),
                    parse_mode="HTML",
                    reply_markup=self._notepad_back_keyboard(),
                )
                return
            path = replace_memory_plus_notepad(workspace, text)
            await self._reply_text(
                update,
                f"✅ Memory+ notepad replaced.\nPath: <code>{html.escape(str(path))}</code>",
                parse_mode="HTML",
                reply_markup=self._notepad_keyboard(),
            )
            return

        if action == "clear":
            path = clear_memory_plus_notepad(workspace)
            await self._reply_text(
                update,
                f"🧹 Memory+ notepad cleared for today.\nPath: <code>{html.escape(str(path))}</code>",
                parse_mode="HTML",
                reply_markup=self._notepad_keyboard(),
            )
            return

        await self._reply_text(
            update,
            self._notepad_help_text("menu"),
            parse_mode="HTML",
            reply_markup=self._notepad_keyboard(),
        )

    def _notepad_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Today", callback_data="npad:refresh"),
                    InlineKeyboardButton("Carryover", callback_data="npad:carryover"),
                ],
                [
                    InlineKeyboardButton("History", callback_data="npad:history"),
                    InlineKeyboardButton("Find", callback_data="npad:help_find"),
                ],
                [
                    InlineKeyboardButton("Add note", callback_data="npad:help_edit"),
                    InlineKeyboardButton("Compact", callback_data="npad:compact"),
                ],
                [
                    InlineKeyboardButton("Replace today", callback_data="npad:help_replace"),
                    InlineKeyboardButton("Clear today", callback_data="npad:clear_confirm"),
                ],
            ]
        )

    def _notepad_back_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton(BACK_LABEL, callback_data="npad:refresh")]])

    def _notepad_clear_confirm_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Clear today's notepad", callback_data="npad:clear_now")],
                [InlineKeyboardButton("← Keep notepad", callback_data="npad:refresh")],
            ]
        )

    def _notepad_workspace(self, update: Any | None = None) -> Path:
        try:
            return runtime_session.current_session_workspace(self, update)
        except (AttributeError, runtime_session.SessionNotFound):
            return self.workspace_dir

    def _notepad_view_payload(
        self, workspace: Path | None = None
    ) -> tuple[str, InlineKeyboardMarkup]:
        workspace = workspace or self._notepad_workspace()
        view = read_memory_plus_notepad(workspace)
        status = get_memory_plus_status(workspace)
        status["enabled"] = is_memory_plus_enabled(self.workspace_dir)
        body = view.body.strip()
        if not body:
            body = "(empty)"
        max_body_chars = 2400
        truncated = len(body) > max_body_chars
        if truncated:
            body = body[:max_body_chars].rstrip() + "\n[clipped]"
        header = [
            card_title("📝", "Memory+ today"),
            "",
            f"<b>Current</b> · <code>{'EMPTY' if view.is_empty else 'ACTIVE'}</code>",
            f"<b>Continuity</b> · <code>{'ON' if status['enabled'] else 'OFF'}</code>",
            f"<b>Agent</b> · <code>{html.escape(self.name)}</code>",
            f"<b>Date</b> · <code>{html.escape(view.date or 'unknown')}</code>",
            f"<b>Size</b> · <code>{view.today_chars}</code> chars",
            f"<b>Open items</b> · <code>{view.open_items_count}</code>",
            f"<b>History</b> · <code>{view.history_count}</code> days",
            "",
            "This compact card is read-only background, not a queue of instructions.",
        ]
        if truncated:
            header.append(f"• Showing first <code>{max_body_chars}</code> chars")
        return "\n".join(header) + "\n\n<pre>" + html.escape(body) + "</pre>", self._notepad_keyboard()

    def _notepad_carryover_payload(
        self, workspace: Path | None = None
    ) -> tuple[str, InlineKeyboardMarkup]:
        view = read_memory_plus_notepad(workspace or self._notepad_workspace())
        body = view.carryover.strip() or "(none)"
        return (
            f"{card_title('🌙', 'Memory+ carryover')}\n\n"
            f"<b>Current</b> · <code>{'EMPTY' if body == '(none)' else 'AVAILABLE'}</code>\n"
            f"<b>Agent</b> · <code>{html.escape(self.name)}</code>\n"
            "Only a high-level previous-work passage is carried across midnight.\n\n"
            f"<pre>{html.escape(body)}</pre>",
            self._notepad_keyboard(),
        )

    def _notepad_history_payload(
        self, workspace: Path | None = None
    ) -> tuple[str, InlineKeyboardMarkup]:
        rows = list_memory_plus_history(
            workspace or self._notepad_workspace(), limit=10
        )
        lines = [
            card_title("🗂️", "Memory+ history"),
            "",
            f"<b>Current</b> · <code>{len(rows)}</code> archived days",
            f"<b>Agent</b> · <code>{html.escape(self.name)}</code>",
            "",
            "History is indexed by date. Full archives are loaded only when requested.",
            "",
        ]
        if not rows:
            lines.append("(no archived days)")
        for row in rows:
            summary = " · ".join(str(item) for item in (row.get("summary") or [])[:2])
            lines.append(
                f"• <code>{html.escape(str(row.get('date') or 'unknown'))}</code>"
                + (f" · {html.escape(summary)}" if summary else "")
            )
            if row.get("archive"):
                lines.append(f"  <code>{html.escape(str(row['archive']))}</code>")
        return "\n".join(lines), self._notepad_keyboard()

    def _notepad_find_payload(
        self, query_text: str, workspace: Path | None = None
    ) -> tuple[str, InlineKeyboardMarkup]:
        rows = search_memory_plus_history(
            workspace or self._notepad_workspace(), query_text
        )
        lines = [
            card_title("🔎", "Find continuity"),
            "",
            f"<b>Current</b> · <code>{len(rows)}</code> matches",
            f"<b>Query</b> · <code>{html.escape(query_text)}</code>",
            "",
        ]
        if not rows:
            lines.append("No matching archived continuity was found.")
        for row in rows:
            lines.extend(
                [
                    f"<b>{html.escape(row['date'])}</b> · {html.escape(row['excerpt'])}",
                    f"<code>{html.escape(row['path'])}</code>",
                    "",
                ]
            )
        return "\n".join(lines).rstrip(), self._notepad_keyboard()

    def _notepad_help_text(self, action: str) -> str:
        if action == "edit":
            return (
                f"{card_title('📝', 'Add notepad note')}\n\n"
                "<b>Current</b> · <b>READY</b>\n\n"
                "This appends to today's Memory+ notepad without replacing automatic notes.\n\n"
                "<b>Example</b>\n"
                "<code>/notepad edit Dad prefers short operational summaries.</code>\n\n"
                "Use the Back button to return without changing the notepad."
            )
        if action == "replace":
            return (
                f"{card_title('📝', 'Replace notepad')}\n\n"
                "<b>Current</b> · <b>READY</b>\n\n"
                "This replaces today's continuity body, so use it when the current notepad is wrong or noisy.\n\n"
                "<b>Example</b>\n"
                "<code>/notepad replace - Manual: Dad prefers jasmine rice and avoids eggplant.</code>\n\n"
                "Use the Back button to return without changing the notepad."
            )
        if action == "find":
            return (
                f"{card_title('🔎', 'Find continuity')}\n\n"
                "<b>Current</b> · <b>READY</b>\n\n"
                "Only matching daily checkpoints are returned; full history is not injected.\n\n"
                "<b>Example</b>\n"
                "<code>/notepad find backend effort menu</code>\n\n"
                "Use the Back button to return without searching."
            )
        return (
            f"{card_title('📝', 'Memory+ notepad controls')}\n\n"
            "<b>Current</b> · <b>READY</b>\n\n"
            "<code>/notepad today</code> · current compact work card\n"
            "<code>/notepad carryover</code> · previous-day handover\n"
            "<code>/notepad history</code> · archived-day index\n"
            "<code>/notepad find &lt;text&gt;</code> · search archived checkpoints\n"
            "<code>/notepad edit &lt;text&gt;</code> · append a manual note\n"
            "<code>/notepad replace &lt;text&gt;</code> · replace today's body\n"
            "<code>/notepad compact</code> · de-duplicate and enforce limits\n"
            "<code>/notepad clear</code> · clear today's body"
        )

    def _notepad_command_tail(self, update: Update, context: Any, action: str) -> str:
        raw = getattr(getattr(update, "message", None), "text", "") or ""
        if raw:
            parts = raw.split(None, 2)
            if len(parts) >= 3 and parts[1].strip().lower() == action:
                return parts[2].strip()
        args = [str(arg) for arg in (context.args or [])]
        if args and args[0].strip().lower() == action:
            return " ".join(args[1:]).strip()
        return " ".join(args).strip()

    async def callback_notepad(self, update: Update, context: Any):
        query = update.callback_query
        if not self._is_authorized_user(query.from_user.id):
            return
        data = query.data or ""
        action = data.split(":", 1)[1] if ":" in data else ""
        workspace = self._notepad_workspace(update)
        if action == "refresh":
            text, markup = self._notepad_view_payload(workspace)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        elif action == "carryover":
            text, markup = self._notepad_carryover_payload(workspace)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        elif action == "history":
            text, markup = self._notepad_history_payload(workspace)
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        elif action == "compact":
            compact_memory_plus(workspace)
            text, markup = self._notepad_view_payload(workspace)
            await query.edit_message_text(
                "✅ Compacted.\n\n" + text,
                parse_mode="HTML",
                reply_markup=markup,
            )
        elif action == "memory_on":
            from orchestrator.fresh_context import resume_automatic_context

            set_memory_plus_enabled(self.workspace_dir, True)
            resume_automatic_context(self)
            self.reload_post_turn_observers()
            text, markup = self._notepad_view_payload(workspace)
            await query.edit_message_text(
                "✅ Memory+ continuity enabled.\n\n" + text,
                parse_mode="HTML",
                reply_markup=markup,
            )
        elif action == "memory_off":
            set_memory_plus_enabled(self.workspace_dir, False)
            self.reload_post_turn_observers()
            text, markup = self._notepad_view_payload(workspace)
            await query.edit_message_text(
                "⏸️ Memory+ continuity paused; all files were preserved.\n\n" + text,
                parse_mode="HTML",
                reply_markup=markup,
            )
        elif action == "help_find":
            await query.edit_message_text(
                self._notepad_help_text("find"),
                parse_mode="HTML",
                reply_markup=self._notepad_back_keyboard(),
            )
        elif action == "help_edit":
            await query.edit_message_text(
                self._notepad_help_text("edit"),
                parse_mode="HTML",
                reply_markup=self._notepad_back_keyboard(),
            )
        elif action == "help_replace":
            await query.edit_message_text(
                self._notepad_help_text("replace"),
                parse_mode="HTML",
                reply_markup=self._notepad_back_keyboard(),
            )
        elif action == "clear_confirm":
            await query.edit_message_text(
                confirm_card(
                    "⚠️",
                    "Clear notepad",
                    target=f"<code>{html.escape(self.name)}</code> · today",
                    consequence="This removes today's Memory+ context layer for this agent.",
                ),
                parse_mode="HTML",
                reply_markup=self._notepad_clear_confirm_keyboard(),
            )
        elif action == "clear_now":
            clear_memory_plus_notepad(workspace)
            text, markup = self._notepad_view_payload(workspace)
            await query.edit_message_text("🧹 Cleared.\n\n" + text, parse_mode="HTML", reply_markup=markup)
        else:
            await query.answer("Unknown notepad action.", show_alert=True)
            return
        await query.answer()

    async def cmd_wipe(self, update: Update, context: Any):
        await runtime_workspace.cmd_wipe(self, update, context)

    async def cmd_reset(self, update: Update, context: Any):
        await runtime_workspace.cmd_reset(self, update, context)

    async def cmd_clear(self, update: Update, context: Any):
        await runtime_workspace.cmd_clear(self, update, context)

    async def cmd_stop(self, update: Update, context: Any):
        await runtime_control.cmd_stop(self, update, context)

    async def cmd_steer(self, update: Update, context: Any):
        await runtime_control.cmd_steer(self, update, context)

    async def cmd_focus(self, update: Update, context: Any):
        await runtime_control.cmd_focus(self, update, context)

    async def cmd_recall(self, update: Update, context: Any):
        await runtime_control.cmd_recall(self, update, context)

    async def cmd_retry(self, update: Update, context: Any):
        await runtime_control.cmd_retry(self, update, context)

    async def cmd_resend(self, update: Update, context: Any):
        await runtime_control.cmd_resend(self, update, context)

    # ------------------------------------------------------------------
    # /remote — one-click Hashi Remote start/stop
    # ------------------------------------------------------------------

    def _remote_state_root(self) -> Path:
        bridge_home = getattr(self.global_config, "bridge_home", None)
        if bridge_home:
            try:
                return Path(bridge_home).expanduser().resolve()
            except Exception:
                pass
        return Path(self.global_config.project_root).expanduser().resolve()

    def _remote_config_snapshot(self) -> dict[str, Any]:
        root = Path(self.global_config.project_root).expanduser().resolve()
        state_root = self._remote_state_root()
        config_path = root / "remote" / "config.yaml"
        agents_path = state_root / "agents.json"
        instances_path = state_root / "instances.json"
        data: dict[str, Any] = {}
        try:
            data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        server = data.get("server") or {}
        discovery = data.get("discovery") or {}
        configured_port = server.get("port") or DEFAULT_HASHI_REMOTE_PORT
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
        ports: list[int] = []
        claim = read_runtime_claim(state_root)
        if claim:
            try:
                ports.append(int(claim.get("port") or 0))
            except Exception:
                pass
        try:
            ports.append(int(configured_port or DEFAULT_HASHI_REMOTE_PORT))
        except Exception:
            ports.append(DEFAULT_HASHI_REMOTE_PORT)
        ports = [port for index, port in enumerate(ports) if port > 0 and port not in ports[:index]]
        return {
            "root": root,
            "state_root": state_root,
            "port": ports[0],
            "ports": ports,
            "use_tls": bool(server.get("use_tls", True)),
            "backend": str(discovery.get("backend") or "lan"),
        }

    def _remote_urls(self, path: str) -> list[str]:
        cfg = self._remote_config_snapshot()
        schemes = ("https", "http") if cfg["use_tls"] else ("http", "https")
        normalized_path = path if str(path).startswith("/") else f"/{path}"
        urls: list[str] = []
        for port in cfg.get("ports") or [cfg["port"]]:
            for host in local_http_hosts():
                for scheme in schemes:
                    urls.append(f"{scheme}://{host}:{int(port)}{normalized_path}")
        return urls

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
        agents = len(props.get("remote_agents") or [])
        last_handshake = html.escape(self._format_remote_age(props.get("last_handshake_at")))
        last_seen_ok = html.escape(self._format_remote_age(props.get("last_seen_ok")))
        lines = [f"{presence} <b>{instance_id}</b>"]
        if presence != "🔴 offline":
            endpoint_lines = list(self._render_remote_peer_endpoints(peer))
            if endpoint_lines:
                endpoint_lines[0] += f"  ·  agents: <code>{agents}</code>"
            else:
                endpoint_lines = [f"agents: <code>{agents}</code>"]
            lines.extend(endpoint_lines)
        if last_seen_ok != "n/a":
            lines.append(f"seen: <code>{last_seen_ok}</code>")
        elif last_handshake != "n/a" and state not in {"unknown", "self"}:
            lines.append(f"last handshake: <code>{last_handshake}</code>")
        if presence != "🔴 offline":
            last_error = html.escape(str(props.get("last_error") or "").strip())
            if last_error:
                lines.append(f"error: <code>{last_error}</code>")
            refresh_error = html.escape(str(props.get("last_refresh_error") or "").strip())
            if refresh_error:
                lines.append(f"refresh: <code>{refresh_error}</code>")
        return lines

    def _load_remote_instances(self) -> dict[str, dict[str, Any]]:
        path = self._remote_state_root() / "instances.json"
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
            line = f"addr: <code>local {html.escape(route_host)}:{html.escape(route_port)}</code>"
            if network_host:
                line += f"  ·  <code>lan {html.escape(network_host)}:{html.escape(route_port)}</code>"
            return [line]

        if network_hosts and route_host not in network_hosts and route_host not in {"?", ""}:
            return [
                f"addr: <code>{html.escape(route_host)}:{html.escape(route_port)}</code>",
                f"lan: <code>{html.escape(network_hosts[0])}:{html.escape(route_port)}</code>",
            ]

        return [f"addr: <code>{html.escape(route_host)}:{html.escape(route_port)}</code>"]
    async def cmd_remote(self, update: Update, context: Any):
        await runtime_remote.cmd_remote(self, update, context)

    async def cmd_wol(self, update: Update, context: Any):
        from orchestrator import runtime_wol

        await runtime_wol.cmd_wol(self, update, context)

    # ------------------------------------------------------------------
    # /long ... /end multimodal batching
    # ------------------------------------------------------------------

    async def cmd_long(self, update: Update, context: Any):
        """Start collecting text and media for one request."""
        await runtime_long.cmd_long(self, update, context)

    async def cmd_end(self, update: Update, context: Any):
        """Submit the collected /long batch as one request."""
        await runtime_long.cmd_end(self, update, context)

    async def _long_buffer_timeout(self):
        """Safety timeout: auto-submit the collected batch after 5 minutes."""
        await runtime_long.long_buffer_timeout(self)

    async def handle_message(self, update: Update, context: Any):
        if not self._is_authorized_user(update.effective_user.id):
            self.logger.warning(f"Ignored message from unauthorized user ID: {update.effective_user.id}")
            return
        if not await FlexibleAgentRuntime._telegram_channel_allowed(self, update, source_channel="telegram"):
            return
        self._record_active_chat(update)
        if self._should_redirect_after_transfer():
            await self._reply_text(update, self._transfer_redirect_text())
            return
        text = update.message.text
        # /long is scoped to the chat that started it.
        if runtime_long.collect_text(self, update.effective_chat.id, text):
            return
        if await runtime_scheduler_recovery.handle_reply(self, text=text, chat_id=update.effective_chat.id):
            return
        _print_user_message(self.name, text)
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
                    idle_s = int(backend._last_activity_age())
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
                if isinstance(exc, RetryAfter):
                    await telegram_delivery_failover.handle_retry_after(
                        self,
                        exc=exc,
                        chat_id=chat_id,
                        request_id=request_id,
                        purpose="placeholder_status",
                    )
                    return
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
        display_state=None,
    ):
        """
        Temporary verbose progress card.  It consumes progress and tool events
        only; provider reasoning and answer text are deliberately excluded.
        """
        if placeholder is None or not self.telegram_connected:
            return

        from adapters.stream_events import (
            KIND_REVIEW,
            KIND_TESTING,
            KIND_TOOL_START,
            KIND_VALIDATION,
        )

        buffer: list[str] = []
        MAX_LINES = 10
        MAX_MSG_LEN = 3800
        display_policy = telegram_stream_policy.get_display_policy(self)
        MIN_EDIT_INTERVAL = display_policy.edit_interval_s
        HEARTBEAT_INTERVAL = display_policy.heartbeat_interval_s
        MAX_EDITS = display_policy.max_edits_per_request
        last_edit_at = 0.0
        started = time.monotonic()
        last_heartbeat_at = started
        dirty = False
        edit_attempts = 0
        display_disabled = False
        current_message = placeholder
        engine = getattr(self.config, "active_backend", "unknown")

        ICONS = {
            "tool_start": "🔧",
            "tool_end": "  →",
            "file_read": "📂",
            "file_edit": "📝",
            "shell_exec": "🔧",
            "progress": "📊",
            "review": "🧐",
            "validation": "✅",
            "testing": "🧪",
            "error": "❌",
        }

        def _build_display() -> str:
            elapsed = max(0, int(time.monotonic() - started))
            return _streaming_status_to_html(
                self.name,
                engine,
                elapsed,
                buffer[-MAX_LINES:],
                max_message_len=MAX_MSG_LEN,
            )

        async def _rollover_placeholder(text: str) -> bool:
            nonlocal current_message, last_edit_at, dirty, edit_attempts, display_disabled
            try:
                current_message = await self.app.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode="HTML",
                    disable_notification=disable_notification(self),
                )
                if display_state is not None:
                    display_state.current_message = current_message
                    display_state.message_ids.append(current_message.message_id)
                    display_state.rollover_count += 1
                edit_attempts = 0
                last_edit_at = time.monotonic()
                dirty = False
                self.telegram_logger.info(
                    f"Rolled over streaming display for {request_id} "
                    f"(messages={len(display_state.message_ids) if display_state is not None else 'unknown'}, "
                    f"max_edits_per_message={MAX_EDITS})"
                )
                return True
            except Exception as exc:
                display_disabled = True
                dirty = False
                if isinstance(exc, RetryAfter):
                    await telegram_delivery_failover.handle_retry_after(
                        self,
                        exc=exc,
                        chat_id=chat_id,
                        request_id=request_id,
                        purpose="streaming_display_rollover",
                    )
                self.telegram_logger.warning(
                    f"Streaming display rollover failed for {request_id}: {exc}"
                )
                return False

        async def _edit_placeholder():
            nonlocal last_edit_at, dirty, edit_attempts, display_disabled
            if display_disabled:
                dirty = False
                return
            if MAX_EDITS <= 0:
                display_disabled = True
                dirty = False
                self.telegram_logger.info(
                    f"Streaming display disabled by edit budget for {request_id} "
                    f"(attempts={edit_attempts}, max_edits={MAX_EDITS})"
                )
                return
            text = _build_display()
            if edit_attempts >= MAX_EDITS:
                await _rollover_placeholder(text)
                return
            edit_attempts += 1
            try:
                await self.app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=current_message.message_id,
                    text=text,
                    parse_mode="HTML",
                )
                last_edit_at = time.monotonic()
                dirty = False
            except Exception as exc:
                if isinstance(exc, RetryAfter):
                    display_disabled = True
                    await telegram_delivery_failover.handle_retry_after(
                        self,
                        exc=exc,
                        chat_id=chat_id,
                        request_id=request_id,
                        purpose="streaming_display",
                    )
                    dirty = False
                    return
                if "429" in str(exc) or "RetryAfter" in str(exc):
                    display_disabled = True
                    dirty = False
                    self.telegram_logger.warning(
                        f"Streaming display disabled for {request_id}: {exc}"
                    )
                elif "message to edit not found" in str(exc).lower() or "message is not modified" in str(exc).lower():
                    last_edit_at = time.monotonic()
                    dirty = False
                else:
                    last_edit_at = time.monotonic()
                    dirty = False
                    self.telegram_logger.warning(
                        f"Streaming display edit failed for {request_id}: {exc}"
                    )

        while not stop_event.is_set():
            event_task = asyncio.create_task(event_queue.get())
            stop_task = asyncio.create_task(stop_event.wait())
            done = set()
            try:
                done, _pending = await asyncio.wait(
                    {event_task, stop_task},
                    timeout=MIN_EDIT_INTERVAL,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                pending = {event_task, stop_task} - done
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

            if stop_task in done and stop_task.result():
                if event_task in done:
                    event_task.result()
                break

            if event_task in done:
                event = event_task.result()
                icon = ICONS.get(event.kind, "•")
                if event.kind == KIND_TOOL_START:
                    # Replace last tool_start line if consecutive (avoids fragmented JSON input spam)
                    if buffer and buffer[-1].startswith("🔧"):
                        buffer[-1] = f"{icon} {event.summary[:100]}"
                    else:
                        buffer.append(f"{icon} {event.summary[:100]}")
                elif event.kind in {KIND_REVIEW, KIND_VALIDATION, KIND_TESTING}:
                    buffer.append(f"{icon} {event.summary[:240]}")
                else:
                    buffer.append(f"{icon} {event.summary[:100]}")

                if len(buffer) > MAX_LINES * 2:
                    buffer[:] = buffer[-MAX_LINES:]

                dirty = True
            else:
                now = time.monotonic()
                if (now - last_heartbeat_at) >= HEARTBEAT_INTERVAL:
                    heartbeat = f"📊 Still working... ({int(now - started)}s)"
                    if buffer and buffer[-1].startswith("📊 Still working..."):
                        buffer[-1] = heartbeat
                    else:
                        buffer.append(heartbeat)
                    last_heartbeat_at = now
                    dirty = True

            now = time.monotonic()
            if dirty and (now - last_edit_at) >= MIN_EDIT_INTERVAL:
                await _edit_placeholder()

        if buffer:
            elapsed = max(0, int(time.monotonic() - started))
            buffer.append(f"✅ Done ({elapsed}s)")
            await _edit_placeholder()

    def _make_stream_callback(self, event_queue: asyncio.Queue | None = None,
                              think_buffer: list | None = None,
                              audit_collector: AuditTelemetryCollector | None = None):
        """Present explicit owners; retain legacy kind routing for old adapters."""
        from adapters.stream_events import (
            DELIVERY_REASONING,
            DELIVERY_TECHNICAL,
            KIND_COMMENTARY,
            KIND_ERROR,
            KIND_FILE_EDIT,
            KIND_FILE_READ,
            KIND_PROGRESS,
            KIND_SHELL_EXEC,
            KIND_THINKING,
            KIND_TOOL_END,
            KIND_TOOL_START,
            legacy_delivery_class,
        )
        verbose_kinds = {
            KIND_ERROR,
            KIND_FILE_EDIT,
            KIND_FILE_READ,
            KIND_PROGRESS,
            KIND_SHELL_EXEC,
            KIND_TOOL_END,
            KIND_TOOL_START,
        }
        _engine = self.config.active_backend
        _chunk_target = 100
        _chunk_hard_limit = 150
        _chunk_endings = ("。", "！", "？", "\n")
        async def _callback(event):
            if audit_collector is not None:
                # Best-effort hot path: audit telemetry must not disrupt stream delivery.
                with suppress(Exception):
                    await audit_collector.record(event)
            explicit_owner = str(getattr(event, "delivery_class", "") or "")
            owner = explicit_owner or legacy_delivery_class(event.kind)
            if (
                event_queue is not None
                and owner == DELIVERY_TECHNICAL
                and event.kind in verbose_kinds
            ):
                try:
                    event_queue.put_nowait(event)
                except asyncio.QueueFull:
                    self.logger.debug(f"Stream event queue full, dropping: {event.summary[:40]!r}")
            # Always track thinking volume for token estimation (CLI backends only;
            # OpenRouter gets real counts from response.usage)
            if (
                owner == DELIVERY_REASONING
                and event.kind == KIND_THINKING
                and _engine != "openrouter-api"
            ):
                raw = event.summary or ""
                if raw and raw not in ("Thinking...",):
                    self._thinking_chars_this_req += len(raw)
                detail = event.detail or ""
                if detail.startswith("thinking_chars="):
                    with suppress(Exception):
                        value = detail.split("=", 1)[1].split(";", 1)[0]
                        self._thinking_chars_this_req += max(0, int(value))
            if think_buffer is not None:
                if not explicit_owner and event.kind == KIND_COMMENTARY:
                    # Commentary is already a complete model-authored update.
                    # Preserve it verbatim instead of folding it into the short
                    # provider-reasoning chunk accumulator.
                    if self._openrouter_think_chunk:
                        think_buffer.append(self._openrouter_think_chunk)
                        self._openrouter_think_chunk = ""
                    commentary = (event.summary or "").strip()
                    if commentary:
                        think_buffer.append(commentary)
                    return
                if owner != DELIVERY_REASONING or event.kind != KIND_THINKING:
                    return
                # Provider deltas already encode their own word boundaries.
                # Never trim, deduplicate, or invent separators between them.
                raw_delta = getattr(event, "raw_delta", "")
                if raw_delta:
                    self._openrouter_think_chunk += raw_delta
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
        lines = self._think_buffer[:]
        self._think_buffer.clear()
        text = "\n".join(lines)
        # Console
        _print_thinking(self.name, text)
        # Transcript (for workbench polling) — always write, even if Telegram disconnected
        self.handoff_builder.append_transcript("thinking", f"💭 {text}", "think")
        # Telegram — skip if not connected
        if not self.telegram_connected:
            return
        _think_raw = f"💭 {text}"
        _think_msg = _md_to_html(_think_raw)
        if len(_think_msg) > 3800:
            # Long Codex commentary is intentionally not clipped. Reuse the
            # normal Telegram chunker so every character reaches the user.
            await self.send_long_message(chat_id, _think_raw, purpose="think")
            return
        try:
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=_think_msg,
                parse_mode="HTML",
                disable_notification=not self._notify_enabled,
            )
        except Exception as e:
            if "ConnectError" in type(e).__name__ or "ConnectError" in str(e):
                await asyncio.sleep(2)
                try:
                    await self.app.bot.send_message(
                        chat_id=chat_id,
                        text=_think_msg,
                        parse_mode="HTML",
                        disable_notification=not self._notify_enabled,
                    )
                except Exception as e2:
                    self.telegram_logger.warning(f"Failed to send thinking message (retry): {e2}")
            else:
                self.telegram_logger.warning(f"Failed to send thinking message: {e}")

    async def _thinking_flush_loop(self, chat_id: int, stop_event: asyncio.Event):
        """Periodically flush accumulated thinking traces every 6 seconds."""
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=6)
                await self._flush_thinking(chat_id)
                break  # stop_event was set
            except asyncio.TimeoutError:
                pass  # 6s elapsed — flush
            await self._flush_thinking(chat_id)

    def _wrapper_enabled(self) -> bool:
        return runtime_wrapper.wrapper_enabled(self)

    def _wrapper_visible_context(
        self, context_window: int, item: QueuedRequest | None = None
    ) -> list[dict[str, str]]:
        return runtime_wrapper.wrapper_visible_context(self, context_window, item=item)

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

    def _load_last_text_from_transcript(self, role: str) -> str | None:
        """Read the last transcript message for commands such as /say."""
        try:
            path = getattr(self, "transcript_log_path", None)
            if path is None or not path.exists():
                return None
            last_text = None
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    if entry.get("role") == role and entry.get("text"):
                        last_text = entry["text"]
            return last_text
        except Exception:
            return None

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

    async def _send_meter_cost_tail(self, item: QueuedRequest) -> None:
        """Send the per-turn cost tail after the answer is confirmed delivered.

        Uses request-local ``meter_at_start`` so a mid-flight toggle never changes
        an in-progress turn.  Never writes to memory/transcript/wrapper and is
        skipped for silent, non-Telegram, transfer-buffered, or undelivered
        turns.
        """
        request_meta = runtime_pipeline.request_meta_for(self, item.request_id)
        if request_meta.get("meter_at_start") is not True:
            return
        if getattr(item, "silent", False) or not getattr(item, "deliver_to_telegram", True):
            return
        if self._should_buffer_during_transfer(item.request_id):
            return
        receipt_registry = getattr(self, "_meter_receipt_by_id", None)
        receipt = (
            receipt_registry.get(item.request_id)
            if isinstance(receipt_registry, dict)
            else None
        )
        if receipt is None:
            return
        try:
            from tools.meter_cost import format_cost_tail

            text = format_cost_tail(receipt)
        except Exception:
            self.logger.exception("meter cost tail formatting failed")
            return
        try:
            await self.send_long_message(
                chat_id=item.chat_id,
                text=text,
                request_id=item.request_id,
                purpose="meter-cost",
            )
        except Exception:
            # A failed cost tail must never break the turn.
            self.logger.exception("meter cost tail delivery failed")

    async def _send_meditation_cost_tail(
        self, job: dict[str, Any]
    ) -> bool | None:
        """Send the async Meditation cost tail after the Habit meditation ends.

        Gated on the frozen ``meter_at_start`` (not ``/verbose``), and kept as
        its own short message so it never enters memory / transcript / wrapper /
        voice / HChat content.  ``task_total_usd`` is the foreground receipt for
        this turn plus the Meditation cost, when the foreground receipt is still
        available.
        """
        meter = job.get("meter") if isinstance(job.get("meter"), dict) else {}
        notification = (
            meter.get("notification")
            if isinstance(meter.get("notification"), dict)
            else (
                job.get("notification")
                if isinstance(job.get("notification"), dict)
                else {}
            )
        )
        if notification.get("meter_at_start") is not True:
            return True
        raw_items = meter.get("line_items")
        if not isinstance(raw_items, list) or not raw_items:
            return True
        try:
            from tools.meter_cost import (
                UsageReceipt,
                format_meditation_cost_tail,
                line_item_from_dict,
            )

            line_items = [
                line_item_from_dict(item)
                for item in raw_items
                if isinstance(item, dict)
            ]
            if not line_items:
                return True
            receipt = UsageReceipt(
                request_id=str(job.get("request_id") or ""),
                parent_request_id="",
                line_items=line_items,
            )
            foreground = self._meter_receipt_by_id.get(str(job.get("request_id") or ""))
            task_total = None
            if (
                foreground is not None
                and receipt.cost_usd is not None
                and getattr(foreground, "cost_usd", None) is not None
            ):
                task_total = round(
                    float(receipt.cost_usd)
                    + float(foreground.cost_usd),
                    6,
                )
            text = format_meditation_cost_tail(receipt, task_total_usd=task_total)
        except Exception:
            self.logger.exception("meditation cost tail formatting failed")
            return False
        chat_id = notification.get("chat_id")
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            return True
        try:
            result = await self.send_long_message(
                chat_id=chat_id,
                text=text,
                request_id=job.get("request_id"),
                purpose="meditation-cost",
            )
            delivered = bool(
                isinstance(result, tuple) and len(result) >= 2 and int(result[1]) > 0
            )
            if delivered:
                receipt_registry = getattr(self, "_meter_receipt_by_id", None)
                if isinstance(receipt_registry, dict):
                    receipt_registry.pop(str(job.get("request_id") or ""), None)
            return True if delivered else None
        except Exception:
            self.logger.exception("meditation cost tail delivery failed")
            return False

    def _audit_enabled(self) -> bool:
        return runtime_audit.audit_enabled(self)

    def _audit_visible_context(
        self, context_window: int, item: QueuedRequest | None = None
    ) -> list[dict[str, str]]:
        return runtime_audit.audit_visible_context(self, context_window, item=item)

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
        background_request_ids = getattr(self, "_background_request_ids", None)
        if not isinstance(background_request_ids, set):
            background_request_ids = set()
            self._background_request_ids = background_request_ids
        background_request_ids.add(item.request_id)

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
            await runtime_delivery_order.complete_turn(self, item.request_id)
            getattr(self, "_background_request_ids", set()).discard(item.request_id)
            registry = getattr(self, "_request_meta_by_id", None)
            if isinstance(registry, dict):
                registry.pop(item.request_id, None)
            return

        receipt_text = ""
        receipt_response = None
        receipt_error = ""
        receipt_delivered = False
        receipt_disposition = "background_transport_not_attempted"
        receipt_chunk_count = 0
        receipt_error_type = ""
        try:
            await runtime_delivery_order.wait_for_turn(self, item.request_id)
            await runtime_background_status.wait_for_delivery(item)
            if task.cancelled():
                receipt_error = "background_task_cancelled"
                self._mark_error(f"Background task cancelled: {item.summary}")
                self.logger.warning(f"Background task {item.request_id} was cancelled.")
                is_bridge_request = item.source.startswith("bridge:") or item.source.startswith("bridge-transfer:")
                self._notify_right_brain_interrupted(
                    item,
                    item.prompt,
                    is_bridge_request=is_bridge_request,
                    reason="background_cancelled",
                    error="background_task_cancelled",
                )
                await self._notify_request_listeners(
                    item.request_id,
                    {
                        "request_id": item.request_id,
                        "success": False,
                        "text": None,
                        "error": "background_task_cancelled",
                        "source": item.source,
                        "summary": item.summary,
                        **runtime_pipeline.request_context_warning_fields(
                            self, item.request_id
                        ),
                    },
                )
                _elapsed, chunk_count = await self.send_long_message(
                    item.chat_id,
                    f"⚠️ Background task [{item.summary}] was cancelled before completing.",
                    request_id=item.request_id,
                    purpose="bg-cancelled",
                )
                receipt_delivered = chunk_count > 0
                return

            exc = task.exception()
            if exc:
                receipt_error = str(exc)
                self._mark_error(str(exc))
                self.error_logger.error(f"Background task {item.request_id} raised: {exc}")
                is_bridge_request = item.source.startswith("bridge:") or item.source.startswith("bridge-transfer:")
                self._notify_right_brain_interrupted(
                    item,
                    item.prompt,
                    is_bridge_request=is_bridge_request,
                    reason="background_error",
                    error=str(exc),
                )
                await self._notify_request_listeners(
                    item.request_id,
                    {
                        "request_id": item.request_id,
                        "success": False,
                        "text": None,
                        "error": str(exc),
                        "source": item.source,
                        "summary": item.summary,
                        **runtime_pipeline.request_context_warning_fields(
                            self, item.request_id
                        ),
                    },
                )
                _elapsed, chunk_count = await self.send_long_message(
                    item.chat_id,
                    f"⚠️ Background task error ({self.config.active_backend}): {exc}",
                    request_id=item.request_id,
                    purpose="bg-error",
                )
                receipt_delivered = chunk_count > 0
                return

            response = task.result()
            recovered = await runtime_pipeline.recover_typed_context_capacity_rejection(
                self,
                item,
                response,
                on_stream_event=None,
            )
            if recovered is not None:
                response, _recovered_prompt = recovered
            receipt_response = response

            if response.is_success and response.text:
                display_text = self._strip_transfer_accept_prefix(item, response.text)
                self._mark_success()
                visible_text, wrapper_result = await self._apply_wrapper_to_visible_text(item, display_text or response.text)
                receipt_text = visible_text
                runtime_retry.clear_completed_interrupted_task(self, item)
                safe_core_raw = extract_memory_plus_update_details(response.text).visible_text
                self._append_core_transcript(
                    item,
                    core_raw=safe_core_raw,
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
                        **runtime_pipeline.request_context_warning_fields(
                            self, item.request_id
                        ),
                        **self._wrapper_listener_fields(safe_core_raw, visible_text, wrapper_result),
                    },
                )
                is_bridge_request = item.source.startswith("bridge:") or item.source.startswith("bridge-transfer:")
                self._notify_right_brain_completed(
                    item,
                    item.prompt,
                    visible_text,
                    is_bridge_request=is_bridge_request,
                    completion_path="background",
                )
                if self._should_buffer_during_transfer(item.request_id):
                    self._record_suppressed_transfer_result(item, success=True, text=visible_text)
                    receipt_disposition = "buffered_during_transfer"
                    return
                runtime_retry.remember_output(self, item, visible_text)
                try:
                    from tools.token_tracker import estimate_tokens, record_audit_event, record_usage
                    import hashlib as _hashlib
                    _meter_line_items = runtime_pipeline._meter_line_items_from_response(response)
                    if response.usage:
                        # Real usage from API/CLI backend
                        _bg_input_tok = response.usage.input_tokens
                        _bg_output_tok = response.usage.output_tokens
                        _bg_thinking_tok = response.usage.thinking_tokens
                        _bg_tok_source = "api"
                        _meter_receipt = record_usage(
                            self.workspace_dir,
                            model=self.get_current_model(),
                            backend=self.config.active_backend,
                            input_tokens=_bg_input_tok,
                            output_tokens=_bg_output_tok,
                            thinking_tokens=_bg_thinking_tok,
                            session_id=self.session_id_dt,
                            cost_usd=getattr(response, "cost_usd", None),
                            request_id=item.request_id,
                            phase="background",
                            engine=self.config.active_backend,
                            line_items=_meter_line_items,
                            token_source="provider",
                        )
                    else:
                        # CLI backend: estimate from full assembled prompt (includes history)
                        fallback_input = estimate_tokens(self._get_system_prompt_text()) + estimate_tokens(item.prompt)
                        _bg_input_tok = self._last_full_prompt_tokens or fallback_input
                        _bg_output_tok = estimate_tokens(visible_text)
                        _bg_thinking_tok = self._thinking_chars_this_req // 4
                        _bg_tok_source = "estimated"
                        _meter_receipt = record_usage(
                            self.workspace_dir,
                            model=self.get_current_model(),
                            backend=self.config.active_backend,
                            input_tokens=_bg_input_tok,
                            output_tokens=_bg_output_tok,
                            thinking_tokens=_bg_thinking_tok,
                            session_id=self.session_id_dt,
                            request_id=item.request_id,
                            phase="background",
                            engine=self.config.active_backend,
                            line_items=_meter_line_items,
                        )
                    runtime_pipeline.remember_meter_receipt(
                        self, item.request_id, _meter_receipt
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
                        **runtime_pipeline.skill_usage_audit_fields(item),
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
                if item.source.lower() in {"document", "photo", "voice", "audio", "video", "sticker", "multimodal"}:
                    memory_user_text = f"[{item.source}] {item.summary}"
                is_bridge_request = item.source.startswith("bridge:") or item.source.startswith("bridge-transfer:")
                if item.source not in {"startup", "system", SESSION_RESET_SOURCE}:
                    memory_assistant_text = self._core_memory_assistant_text(response.text, visible_text, wrapper_result)
                    runtime_session.record_working_exchange(
                        self,
                        item,
                        user_text=memory_user_text,
                        assistant_text=memory_assistant_text,
                        assistant_source=self.config.active_backend,
                    )
                    self._schedule_post_turn_observers(
                        item,
                        memory_user_text,
                        memory_assistant_text,
                        is_bridge_request=is_bridge_request,
                    )
                    if not is_bridge_request:
                        try:
                            from orchestrator.context_compaction import (
                                estimate_tokens,
                                schedule_post_turn,
                            )

                            request_tokens = getattr(
                                self,
                                "_context_compaction_prompt_tokens",
                                {},
                            )
                            prompt_tokens = int(
                                request_tokens.get(item.request_id) or 0
                            )
                            if prompt_tokens > 0:
                                schedule_post_turn(
                                    self,
                                    request_ref=item.request_id,
                                    prompt_tokens=(
                                        prompt_tokens
                                        + estimate_tokens(memory_assistant_text)
                                    ),
                                    chat_id=item.chat_id,
                                    deliver_to_telegram=bool(
                                        item.deliver_to_telegram
                                    ),
                                )
                        except Exception as exc:
                            self.logger.warning(
                                "Post-turn context compaction scheduling failed safely "
                                "for %s: %s: %s",
                                item.request_id,
                                type(exc).__name__,
                                exc,
                            )
                handoff_builder = runtime_session.session_handoff_builder(
                    self, item=item
                )
                handoff_builder.append_transcript("user", item.prompt, item.source)
                handoff_builder.append_transcript("assistant", visible_text, item.source)
                handoff_builder.refresh_recent_context()
                self.project_chat_logger.log_exchange(item.prompt, visible_text, item.source)
                _print_final_response(self.name, visible_text)
                total_s = runtime_pipeline.queued_elapsed_s(item)
                await self._send_wrapper_verbose_trace(item, safe_core_raw, visible_text, wrapper_result)
                her_delivery = runtime_pipeline._her_v2_delivery_metadata(response)
                if her_delivery.get("final_already_delivered"):
                    send_elapsed_s, chunk_count = 0.0, 0
                    receipt_delivered = True
                    receipt_disposition = "initial_resolution_delivered"
                else:
                    send_elapsed_s, chunk_count = await self.send_long_message(
                        chat_id=item.chat_id,
                        text=visible_text,
                        request_id=item.request_id,
                        purpose="bg-response",
                    )
                    receipt_delivered = chunk_count > 0
                    receipt_disposition = (
                        "transport_delivered"
                        if receipt_delivered
                        else "transport_returned_no_receipt"
                    )
                receipt_chunk_count = chunk_count
                await self._send_voice_reply(item.chat_id, visible_text, item.request_id)
                if receipt_delivered:
                    await self._send_meter_cost_tail(item)
                self._schedule_audit_followup(
                    item,
                    core_raw=safe_core_raw,
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
                receipt_error = err_msg
                self._mark_error(err_msg)
                is_bridge_request = item.source.startswith("bridge:") or item.source.startswith("bridge-transfer:")
                self._notify_right_brain_interrupted(
                    item,
                    item.prompt,
                    is_bridge_request=is_bridge_request,
                    reason="background_backend_error",
                    error=err_msg,
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
                        **runtime_pipeline.request_context_warning_fields(
                            self, item.request_id
                        ),
                    },
                )
                if self._should_buffer_during_transfer(item.request_id):
                    self._record_suppressed_transfer_result(item, success=False, error=err_msg)
                    return
                self.error_logger.error(f"Background task {item.request_id} failed: {err_msg}")
                clipped = err_msg if len(err_msg) <= 3000 else err_msg[:2800].rstrip() + "\n\n[truncated]"
                _elapsed, chunk_count = await self.send_long_message(
                    item.chat_id,
                    f"⚠️ Background task error ({self.config.active_backend}): {clipped}",
                    request_id=item.request_id,
                    purpose="bg-error",
                )
                receipt_delivered = chunk_count > 0

        except Exception as e:
            receipt_error = str(e)
            receipt_error_type = type(e).__name__
            if not receipt_delivered:
                receipt_disposition = "transport_exception"
            self._mark_error(str(e))
            self.error_logger.exception(
                f"Unhandled error in _on_background_complete for {item.request_id}: {e}"
            )
        finally:
            if receipt_response is not None:
                await runtime_pipeline.record_her_v2_transport_receipt(
                    self,
                    item,
                    receipt_response,
                    delivered=receipt_delivered,
                    disposition=receipt_disposition,
                    chunk_count=receipt_chunk_count,
                    completion_path="background",
                    error_type=receipt_error_type,
                )
            runtime_cross_session.record_turn_result(
                self, item, assistant_text=receipt_text, response=receipt_response,
                error=receipt_error, delivered=receipt_delivered, completion_path="background",
            )
            getattr(self, "_background_request_ids", set()).discard(item.request_id)
            registry = getattr(self, "_request_meta_by_id", None)
            if isinstance(registry, dict):
                registry.pop(item.request_id, None)
            runtime_pipeline.clear_context_compaction_request_state(
                self,
                item.request_id,
            )
            await runtime_delivery_order.complete_turn(self, item.request_id)

    async def process_queue(self):
        await runtime_lifecycle.process_queue(self)

    def get_bot_commands(self):
        return runtime_command_binding.get_flexible_bot_commands(self)

    async def shutdown(self):
        await runtime_lifecycle.shutdown(self)
