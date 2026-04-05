from __future__ import annotations
import re
import json
import time
import asyncio
import inspect
import logging
import hashlib
from contextlib import suppress
from datetime import datetime, timedelta
from dataclasses import dataclass
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update, constants
from telegram.error import NetworkError as TelegramNetworkError, TimedOut as TelegramTimedOut
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from adapters.base import BaseBackend
from orchestrator.agent_fyi import build_agent_fyi_primer
from orchestrator.bridge_memory import BridgeMemoryStore, BridgeContextAssembler, SysPromptManager
from orchestrator.handoff_builder import HandoffBuilder
from orchestrator.habits import HabitStore
from orchestrator.media_utils import is_image_file, normalize_image_file
from orchestrator.parked_topics import ParkedTopicStore
from orchestrator.skill_manager import SkillDefinition, SkillManager
from orchestrator.voice_manager import VoiceManager

AVAILABLE_GEMINI_MODELS = [
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]

AVAILABLE_OPENROUTER_MODELS = [
    "deepseek/deepseek-v3.2-exp",
    "moonshotai/kimi-k2.5",
    "google/gemini-3.1-flash-lite-preview",
]

AVAILABLE_CLAUDE_MODELS = [
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-haiku-4-5",
]

CLAUDE_MODEL_ALIASES = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
    "haiku": "claude-haiku-4-5",
}

AVAILABLE_CLAUDE_EFFORTS = ["low", "medium", "high"]

AVAILABLE_CODEX_MODELS = [
    "gpt-5.3-codex",
    "gpt-5.4",
    "gpt-5.2-codex",
    "gpt-5.1-codex-max",
    "gpt-5.2",
    "gpt-5.1-codex-mini",
]

AVAILABLE_CODEX_EFFORTS = ["low", "medium", "high", "extra_high"]


@dataclass
class QueuedRequest:
    request_id: str
    chat_id: int
    prompt: str
    source: str
    summary: str
    created_at: str
    silent: bool = False
    is_retry: bool = False
    deliver_to_telegram: bool = True
    active_habits: list[dict] | None = None
    skip_memory_injection: bool = False


def _safe_excerpt(text: str, limit: int = 160) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _md_to_html(text: str) -> str:
    code_blocks = []

    def _save_code_block(match):
        code_blocks.append(match.group(2))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    text = re.sub(r"```(\w*)\n?([\s\S]*?)```", _save_code_block, text)

    inline_codes = []

    def _save_inline_code(match):
        inline_codes.append(match.group(1))
        return f"\x00INLINE{len(inline_codes) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", _save_inline_code, text)

    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"(?<![A-Za-z0-9*])\*([^*<>]+?)\*(?![A-Za-z0-9*])", r"<i>\1</i>", text)

    for i, code in enumerate(code_blocks):
        safe = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00CODEBLOCK{i}\x00", f"<pre>{safe}</pre>")
    for i, code in enumerate(inline_codes):
        safe = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00INLINE{i}\x00", f"<code>{safe}</code>")
    return text


def _print_user_message(agent_name: str, text: str, media_tag: str = ""):
    """Print incoming user message in a restrained cyan on the console."""
    if not text:
        return
    prefix = f"[{media_tag}] " if media_tag else ""
    line = f"\033[38;5;117m[User] >> {prefix}{text}\033[0m"
    try:
        print(line, flush=True)
    except (UnicodeEncodeError, OSError):
        safe = line.encode("utf-8", errors="backslashreplace").decode("utf-8", errors="replace")
        print(safe, flush=True)


def _print_thinking(agent_name: str, text: str):
    """Print thinking trace in dim graphite on the console."""
    if not text:
        return
    c_dim = "\033[38;5;240m"
    c_reset = "\033[0m"
    line = f"{c_dim}[{agent_name}] 💭 {text}{c_reset}"
    try:
        print(line, flush=True)
    except (UnicodeEncodeError, OSError):
        safe = line.encode("utf-8", errors="backslashreplace").decode("utf-8", errors="replace")
        print(safe, flush=True)


def _print_final_response(agent_name: str, text: str):
    if not text:
        return
    c_reset = "\033[0m"
    c_green = "\033[38;5;114m"
    c_border = "\033[38;5;242m"
    line = "-" * 50
    for part in (
        f"\n{c_border}{line}\n",
        f"[{agent_name}] final response:{c_reset}\n",
        f"{c_green}{text}{c_reset}\n",
        f"{c_border}{line}{c_reset}\n\n",
    ):
        try:
            print(part, end="", flush=True)
        except (UnicodeEncodeError, OSError):
            safe = part.encode("utf-8", errors="backslashreplace").decode("utf-8", errors="replace")
            print(safe, end="", flush=True)


def resolve_authorized_telegram_ids(extra: dict | None, global_authorized_id: int) -> tuple[int, ...]:
    """Resolve a stable allowlist of Telegram user IDs.

    - If agent config `extra.authorized_telegram_ids` exists, parse it.
    - Otherwise fall back to the global authorized_id.

    This is used by FlexRuntime to prevent accidental spam/routing mistakes.
    """
    candidates: list[int] = []
    raw = (extra or {}).get("authorized_telegram_ids")
    if raw is not None:
        if isinstance(raw, (list, tuple)):
            candidates.extend(raw)
        else:
            candidates.append(raw)

    ids: list[int] = []
    for item in candidates:
        if item is None:
            continue
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        if value not in ids:
            ids.append(value)

    if not ids and global_authorized_id:
        ids.append(global_authorized_id)

    return tuple(ids)


def _build_jobs_with_buttons(agent_name: str, skill_manager, filter_agent: str | None = None):
    """Build combined jobs message text and inline keyboard with run/toggle buttons.

    filter_agent: if set, only show jobs whose 'agent' field matches. None means show all.
    Returns (text: str, markup: InlineKeyboardMarkup | None).
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    import json as _json

    if skill_manager is None or not hasattr(skill_manager, "tasks_path"):
        return "No task scheduler configured.", None
    try:
        if not skill_manager.tasks_path.exists():
            data = {"heartbeats": [], "crons": []}
        else:
            data = _json.loads(skill_manager.tasks_path.read_text(encoding="utf-8"))
    except Exception:
        return "Could not read tasks.json.", None

    if filter_agent:
        title = f"<b>📋 Jobs — {filter_agent}</b>"
    else:
        title = "<b>📋 Jobs — all agents</b>"
    lines = [title]
    buttons: list = []

    # Show jobs, optionally filtered by agent
    all_jobs: list[tuple[str, dict]] = []

    for h in data.get("heartbeats", []):
        if filter_agent and h.get("agent") != filter_agent:
            continue
        interval = h.get("interval_seconds", 0)
        if interval >= 3600:
            interval_s = f"every {interval // 3600}h"
        elif interval >= 60:
            interval_s = f"every {interval // 60}m"
        else:
            interval_s = f"every {interval}s"
        status = "✅" if h.get("enabled", False) else "❌"
        owner = h.get("agent", "?")
        lines.append(f"\n{status} ⏱ <code>{h['id']}</code> — {interval_s} [{owner}]")
        note = h.get("note", "")
        if note and note != h["id"]:
            lines.append(f"   {note}")
        all_jobs.append(("heartbeat", h))

    for c in data.get("crons", []):
        if filter_agent and c.get("agent") != filter_agent:
            continue
        enabled = c.get("enabled", False)
        schedule = c.get("schedule", "")
        # Parse cron expression into human-readable label
        parts = schedule.split() if schedule else []
        if len(parts) == 5:
            minute, hour, dom, month, dow = parts
            if dom == "*" and month == "*":
                if dow == "*":
                    # Standard daily/hourly cron
                    if hour.startswith("*/"):
                        interval_h = hour[2:]
                        time_s = f"every {interval_h}h"
                    elif minute.startswith("*/"):
                        interval_m = minute[2:]
                        time_s = f"every {interval_m}m"
                    else:
                        try:
                            time_s = f"{int(hour):02d}:{int(minute):02d}"
                        except ValueError:
                            time_s = schedule
                else:
                    time_s = schedule
            else:
                time_s = schedule
        else:
            time_s = schedule or "??:??"
        freq_label = "every " if "every" in time_s else "daily "
        status = "✅" if enabled else "❌"
        owner = c.get("agent", "?")
        lines.append(f"\n{status} 📅 <code>{c['id']}</code> — {freq_label}{time_s} [{owner}]")
        note = c.get("note", "")
        if note and note != c["id"]:
            lines.append(f"   {note}")
        all_jobs.append(("cron", c))

    # Single-column layout: label row + action row per job
    for kind, job in all_jobs:
        jid = job["id"]
        enabled = job.get("enabled", False)
        toggle_mode = "off" if enabled else "on"
        toggle_label = "ON" if enabled else "OFF"
        icon = "⏱" if kind == "heartbeat" else "📅"
        short_id = jid[:22]
        buttons.append([InlineKeyboardButton(f"{icon} {short_id}", callback_data="noop")])
        buttons.append([
            InlineKeyboardButton("▶ Run", callback_data=f"skilljob:{kind}:run:{jid}:now"),
            InlineKeyboardButton(toggle_label, callback_data=f"skilljob:{kind}:toggle:{jid}:{toggle_mode}"),
            InlineKeyboardButton("📤 Transfer", callback_data=f"skilljob:{kind}:transfer:{jid}:select"),
            InlineKeyboardButton("🗑 Del", callback_data=f"skilljob:{kind}:delete:{jid}:confirm"),
        ])

    if not all_jobs:
        lines.append("\nNo jobs configured.")

    markup = InlineKeyboardMarkup(buttons) if buttons else None
    return "\n".join(lines), markup


def _build_jobs_text(agent_name: str, skill_manager) -> str:
    """Build a formatted jobs listing for a single agent."""
    import json as _json
    if skill_manager is None or not hasattr(skill_manager, "tasks_path"):
        return "No task scheduler configured."
    try:
        if not skill_manager.tasks_path.exists():
            data = {"heartbeats": [], "crons": []}
        else:
            data = _json.loads(skill_manager.tasks_path.read_text(encoding="utf-8"))
    except Exception:
        return "Could not read tasks.json."

    lines = [f"<b>Jobs for {agent_name}</b>", ""]
    found = False

    hbs = [h for h in data.get("heartbeats", []) if h.get("agent") == agent_name]
    if hbs:
        lines.append("<b>Heartbeats</b>")
        for h in hbs:
            enabled = "✓" if h.get("enabled") else "✗"
            interval = h.get("interval_seconds", 0)
            if interval >= 3600:
                interval_s = f"{interval // 3600}h"
            elif interval >= 60:
                interval_s = f"{interval // 60}m"
            else:
                interval_s = f"{interval}s"
            note = h.get("note", h.get("id", ""))
            action = h.get("action", "enqueue_prompt")
            lines.append(f"  {enabled} <code>{h['id']}</code>  every {interval_s}")
            if action != "enqueue_prompt":
                lines.append(f"      action: {action}")
            if note and note != h["id"]:
                lines.append(f"      {note}")
        lines.append("")
        found = True

    crons = [c for c in data.get("crons", []) if c.get("agent") == agent_name]
    if crons:
        lines.append("<b>Crons</b>")
        for c in crons:
            enabled = "✓" if c.get("enabled") else "✗"
            time_s = c.get("time", "??:??")
            action = c.get("action", "enqueue_prompt")
            note = c.get("note", c.get("id", ""))
            lines.append(f"  {enabled} <code>{c['id']}</code>  at {time_s}")
            if action != "enqueue_prompt":
                lines.append(f"      action: {action}")
            if note and note != c["id"]:
                lines.append(f"      {note}")
        lines.append("")
        found = True

    if not found:
        lines.append("No jobs configured for this agent.")

    return "\n".join(lines)


def _show_logo_animation():
    """Play the BRIDGE logo animation in the console (logo only, no status)."""
    from orchestrator.banner import show_startup_banner
    show_startup_banner(agent_names=[], logo_only=True)


class BridgeAgentRuntime:
    CODEX_CHUNK_LIMIT_ERROR = "Separator is not found, and chunk exceed the limit"
    CODEX_SCHEDULER_RETRY_DELAY_S = 120

    def __init__(self, name: str, backend: BaseBackend, telegram_token: str, skill_manager: SkillManager | None = None, secrets: dict | None = None):
        self.name = name
        self.backend = backend
        self.config = backend.config
        self.global_config = backend.global_config
        self.token = telegram_token
        self.secrets = secrets or {}

        self.session_started_at = datetime.now()
        self.session_id_dt = self.session_started_at.strftime("%Y-%m-%d_%H%M%S")
        self.session_dir = self.global_config.base_logs_dir / self.name / self.session_id_dt
        self.media_dir = self.global_config.base_media_dir / self.name
        self.transcript_log_path = self.config.workspace_dir / "conversation_log.jsonl"
        self.journal_dir = self.config.workspace_dir / "journals"

        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.journal_dir.mkdir(parents=True, exist_ok=True)

        self.logger = logging.getLogger(f"Runtime.{name}")
        self.telegram_logger = logging.getLogger(f"Runtime.{name}.telegram")
        self.message_logger = logging.getLogger(f"Runtime.{name}.messages")
        self.error_logger = logging.getLogger(f"Runtime.{name}.errors")
        self.maintenance_logger = logging.getLogger(f"Runtime.{name}.maintenance")
        self._setup_logging()

        self.app = ApplicationBuilder().token(self.token).get_updates_connection_pool_size(8).build()
        self.queue = asyncio.Queue()
        self.request_seq = 0
        self.process_task = None
        self.startup_success = False
        self.backend_ready = False
        self.telegram_connected = False
        self.is_generating = False
        self.last_prompt = None
        self.last_response: dict | None = None
        self.current_request_meta: dict | None = None
        self._request_audit_meta: dict[str, dict] = {}
        self.last_activity_at = datetime.now()
        self.last_success_at: datetime | None = None
        self.last_error_at: datetime | None = None
        self.last_error_summary: str | None = None
        _think_file = self.config.workspace_dir / ".think"
        self._think: bool = _think_file.exists()
        self._think_buffer: list[str] = []
        self._openrouter_think_chunk: str = ""
        self._last_openrouter_think_snippet: str | None = None
        self.is_shutting_down = False
        # Load persisted verbose preference (.verbose file presence = ON, absence = OFF)
        _verbose_file = self.config.workspace_dir / ".verbose"
        self._verbose: bool = _verbose_file.exists()
        self._scheduled_retry_tasks: set[asyncio.Task] = set()
        # Background tasks spawned when bg_mode detaches a long-running generation.
        # Tracked so shutdown() can cancel them cleanly.
        self._background_tasks: set[asyncio.Task] = set()
        self._request_listeners: dict[str, list] = {}
        self._pending_request_results: dict[str, dict] = {}
        self.skill_manager = skill_manager
        self.agent_fyi_path = self.global_config.project_root / "docs" / "AGENT_FYI.md"
        self._pending_session_primer: str | None = None
        self._pending_auto_recall_context: str | None = None
        self.runtime_session_path = self.config.workspace_dir / ".runtime_session.json"
        self.voice_manager = VoiceManager(self.config.workspace_dir, self.media_dir, ffmpeg_cmd="ffmpeg", secrets=self.secrets)
        self.memory_store = BridgeMemoryStore(self.config.workspace_dir)
        self.handoff_builder = HandoffBuilder(self.config.workspace_dir, transcript_filename="conversation_log.jsonl")
        self.parked_topics = ParkedTopicStore(self.config.workspace_dir)
        self.sys_prompt_manager = SysPromptManager(self.config.workspace_dir)
        self.context_assembler = BridgeContextAssembler(
            self.memory_store,
            self.config.system_md,
            active_skill_provider=self._get_active_skill_sections,
            sys_prompt_manager=self.sys_prompt_manager,
        )
        self.habit_store = HabitStore(
            self.config.workspace_dir,
            self.global_config.project_root,
            self.name,
            self._get_agent_class(),
        )

    def get_typing_placeholder(self) -> tuple[str, str | None]:
        extra = self.config.extra or {}
        text = extra.get("typing_message")
        parse_mode = extra.get("typing_parse_mode")
        if text:
            return text, parse_mode
        return f"{self.name} is thinking...", None

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
            self.error_logger.error(
                f"Rejected empty prompt from {source} (summary={summary!r})"
            )
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
        self.message_logger.info(
            f"Queued {item.request_id} from {source} (summary={summary!r})"
        )
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
            self.config.engine == "codex-cli"
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

    async def enqueue_startup_bootstrap(self, chat_id: int):
        if not hasattr(self.backend, "should_bootstrap_on_startup"):
            return
        if not self.backend.should_bootstrap_on_startup():
            return
        prompt = self.backend.get_startup_bootstrap_prompt()
        if not prompt:
            return
        await self.enqueue_request(chat_id, prompt, "startup", "Startup bootstrap", silent=True)

    def append_conversation_entry(self, role: str, text: str, source: str):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "role": role,
            "source": source,
            "text": text,
        }
        with open(self.transcript_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=True) + "\n")

    def get_display_name(self) -> str:
        if self.config.extra and self.config.extra.get("display_name"):
            return self.config.extra["display_name"]
        return self.name

    def get_agent_emoji(self) -> str:
        if self.config.extra and self.config.extra.get("emoji"):
            return self.config.extra["emoji"]
        return "🤖"

    def get_runtime_metadata(self) -> dict:
        return {
            "id": self.name,
            "name": self.name,
            "display_name": self.get_display_name(),
            "emoji": self.get_agent_emoji(),
            "engine": self.config.engine,
            "model": self.config.model,
            "workspace_dir": str(self.config.workspace_dir),
            "transcript_path": str(self.transcript_log_path),
            "online": bool(self.backend_ready),
            "status": self._compute_status_string(),
            "type": getattr(self.config, "type", "fixed"),
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
        return self.skill_manager.build_toggle_sections(self.config.workspace_dir)

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
        active = self.skill_manager.get_active_toggle_ids(self.config.workspace_dir)
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
        prompt = self._build_park_summary_prompt(
            context_block,
            last_user_text,
            last_assistant_text,
            title_override=title_override,
        )
        response = await self.backend.generate_response(
            prompt,
            request_id=f"park-{int(time.time())}",
            silent=True,
        )
        parsed = self._extract_json_object(response.text) if response and response.is_success else None
        if not parsed:
            parsed = fallback

        title = (title_override or parsed.get("title") or fallback["title"]).strip()
        summary_short = (parsed.get("summary_short") or fallback["summary_short"]).strip()
        summary_long = (parsed.get("summary_long") or fallback["summary_long"]).strip()
        if not title:
            title = fallback["title"]
        if not summary_short:
            summary_short = fallback["summary_short"]
        if not summary_long:
            summary_long = fallback["summary_long"]

        return {
            "title": title,
            "summary_short": summary_short,
            "summary_long": summary_long,
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
        lines.extend(
            [
                "",
                "Use /load <slot> to restore or /park delete <slot> to remove one.",
            ]
        )
        return "\n".join(lines)

    def is_idle_for_proactive_message(self, min_idle_seconds: int = 900) -> bool:
        if self.is_generating or not self.queue.empty():
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
                chat_id=self.global_config.authorized_id,
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
        proc = getattr(self.backend, "current_proc", None)
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
        active_skills = sorted(self.skill_manager.get_active_toggle_ids(self.config.workspace_dir)) if self.skill_manager else []
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
        lines = [
            f"🤖 {self.name}",
            f"⚙️ Backend: {self.config.engine} • {self.config.model}",
            f"📶 Channels: {channel_line}",
            f"📡 Runtime: {'busy' if self.is_generating else 'idle'} • queue {self.queue.qsize()} • process {self._process_info()}",
            f"🧾 Current: {current_line}",
            f"🧠 Memory: skills {', '.join(active_skills) if active_skills else 'none'} • recall {'ON' if recall_on else 'OFF'} • FYI {'armed' if self._pending_session_primer else 'clear'}",
            f"🔔 Proactive: {active_mode} • every {active_interval} • hb {heartbeat_count} • cron {cron_count}",
            f"🩺 Health: {health_line}",
            f"🕒 Activity: last success {self._format_age(self.last_success_at)} • last activity {self._format_age(self.last_activity_at)}",
        ]
        if detailed:
            lines.extend([
                "",
                f"📁 Workspace: {self.config.workspace_dir}",
                f"📝 Transcript: {self.transcript_log_path.name}",
                f"🚀 Started: {self.session_started_at.isoformat(timespec='seconds')}",
                f"🔁 Retry Cache: prompt {'yes' if self.last_prompt else 'no'} • response {'yes' if self.last_response else 'no'}",
                f"🧷 Primers: FYI {'armed' if self._pending_session_primer else 'clear'} • auto-recall {'armed' if self._pending_auto_recall_context else 'clear'}",
                f"📚 Bridge Memory: {self.memory_store.get_stats()['turns']} turns • {self.memory_store.get_stats()['memories']} memories",
                f"🔍 Verbose: {'ON' if self._verbose else 'OFF'}",
                f"💭 Think: {'ON' if self._think else 'OFF'}",
            ])
            lines.append(f"🏠 HASHI Instance: {self.global_config.project_root}")
            if self.config.engine == "openrouter-api":
                lines.append("☁️ Session Mode: stateless bridge-managed API")
            else:
                lines.append("🧩 Session Mode: stateless bridge-managed CLI")
        else:
            lines.append("")
            lines.append("Use /status full for more detail.")
        return "\n".join(lines)

    def _skill_keyboard(self) -> InlineKeyboardMarkup:
        buttons = []
        grouped = self._skills_by_type()
        active_ids = self.skill_manager.get_active_toggle_ids(self.config.workspace_dir) if self.skill_manager else set()
        for skill_type in ("action", "toggle", "prompt"):
            for skill in grouped.get(skill_type, []):
                label = skill.id
                if skill.type == "toggle":
                    label = f"{skill.id} {'ON' if skill.id in active_ids else 'OFF'}"
                buttons.append([InlineKeyboardButton(label, callback_data=f"skill:show:{skill.id}")])
        return InlineKeyboardMarkup(buttons or [[InlineKeyboardButton("No skills", callback_data="skill:noop:none")]])

    def _skill_action_keyboard(self, skill: SkillDefinition) -> InlineKeyboardMarkup:
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
        text, markup = _build_jobs_with_buttons(self.name, self.skill_manager, filter_agent=self.name)
        if hasattr(update_or_query, "edit_message_text"):
            await update_or_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        else:
            await update_or_query.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

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
            ok, text = await self.skill_manager.run_action_skill(skill, self.config.workspace_dir, args=args)
            if ok and text:
                await self.send_long_message(
                    chat_id=self.global_config.authorized_id,
                    text=text,
                    request_id=f"skill-{task_id}",
                    purpose="scheduler-skill",
                )
            elif text:
                self.error_logger.error(text)
            return
        prompt = self.skill_manager.build_prompt_for_skill(skill, args or "")
        if skill.backend and skill.backend != self.config.engine:
            self.error_logger.error(
                f"Scheduled prompt skill {skill.id} targets {skill.backend} but agent {self.name} runs {self.config.engine}."
            )
            return
        await self.enqueue_request(
            chat_id=self.global_config.authorized_id,
            prompt=prompt,
            source="scheduler-skill",
            summary=f"Skill Task [{task_id}]",
            silent=False,
        )

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
        if deliver_to_telegram and not source.startswith("bridge:"):
            self.append_conversation_entry("user", text, source)
        return await self.enqueue_request(
            self.global_config.authorized_id,
            text,
            source,
            _safe_excerpt(text),
            deliver_to_telegram=deliver_to_telegram,
        )

    async def _hchat_route_reply(self, item: QueuedRequest, response_text: str):
        """If this request was an hchat message, route the reply back to the sender."""
        import re
        match = re.match(r"^\[hchat from (\w+)\]", item.prompt)
        if not match:
            return
        sender_name = match.group(1).lower()
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            return
        for rt in getattr(orchestrator, "runtimes", []):
            if getattr(rt, "name", "") == sender_name and hasattr(rt, "enqueue_api_text"):
                try:
                    await rt.enqueue_api_text(
                        f"[hchat reply from {self.name}] {response_text}",
                        source=f"hchat-reply:{self.name}",
                        deliver_to_telegram=True,
                    )
                    self.logger.info(f"Hchat reply routed back to {sender_name}")
                except Exception as e:
                    self.logger.warning(f"Failed to route hchat reply to {sender_name}: {e}")
                return
        self.logger.warning(f"Hchat reply: sender '{sender_name}' runtime not found")

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
        transcript_text = f"[{media_kind}] {summary}"
        if deliver_to_telegram and not source.startswith("bridge:"):
            self.append_conversation_entry("user", transcript_text, source)
        return await self.enqueue_request(
            self.global_config.authorized_id,
            rendered_prompt,
            source,
            summary,
            deliver_to_telegram=deliver_to_telegram,
        )

    def export_daily_transcript(self, cutoff_dt: datetime) -> bool:
        if not self.transcript_log_path.exists():
            return False

        start_dt = cutoff_dt - timedelta(days=1)
        export_entries = []
        with open(self.transcript_log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    entry_dt = datetime.fromisoformat(entry["timestamp"])
                except Exception:
                    continue
                if start_dt <= entry_dt < cutoff_dt:
                    export_entries.append(entry)

        if not export_entries:
            return False

        journal_date = (cutoff_dt - timedelta(days=1)).date().isoformat()
        journal_path = self.journal_dir / f"{journal_date}.md"
        lines = [f"# Conversation Journal - {journal_date}", ""]
        for entry in export_entries:
            ts = datetime.fromisoformat(entry["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
            role = "User" if entry["role"] == "user" else "Agent"
            lines.append(f"## {role} - {ts}")
            lines.append("")
            lines.append(entry["text"])
            lines.append("")
        journal_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        return True

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
            idle_s: int | None = None
            events: int | None = None
            if backend is not None:
                if getattr(backend, "last_activity_at", 0) > 0:
                    idle_s = int(time.time() - backend.last_activity_at)
                line_count = getattr(backend, "output_line_count", 0)
                if line_count > 0:
                    events = line_count

            is_stuck = idle_s is not None and idle_s > IDLE_WARN_AFTER

            if self._verbose:
                # Verbose: structured detail block
                engine = getattr(self.config, "engine", "unknown")
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

        buffer: list[str] = []          # rolling display lines
        MAX_LINES = 10
        MAX_MSG_LEN = 3800              # Telegram limit is 4096; leave margin
        MIN_EDIT_INTERVAL = 2.5         # seconds between edits
        last_edit_at = 0.0
        _verbose_think_accum: str = ""   # full accumulated thinking so far
        _verbose_think_last_flush = 0.0  # time of last think display update
        started = time.time()
        dirty = False                   # True if buffer changed since last edit
        engine = getattr(self.config, "engine", "unknown")

        # Icon map for event kinds
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
                # Telegram rate limit or message deleted
                if "429" in str(exc) or "RetryAfter" in str(exc):
                    await asyncio.sleep(3)
                elif "message to edit not found" in str(exc).lower() or "message is not modified" in str(exc).lower():
                    pass
                else:
                    self.telegram_logger.warning(
                        f"Streaming display edit failed for {request_id}: {exc}"
                    )

        while not stop_event.is_set():
            # Drain all available events
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=MIN_EDIT_INTERVAL)
                icon = ICONS.get(event.kind, "•")
                # Skip text_delta floods — only keep latest
                if event.kind == KIND_TEXT_DELTA:
                    summary = event.summary[:80]
                    # Replace last text_delta line if consecutive
                    if buffer and buffer[-1].startswith("✏️"):
                        buffer[-1] = f"{icon} {summary}"
                    else:
                        buffer.append(f"{icon} {summary}")
                elif event.kind == KIND_THINKING:
                    # Accumulate ALL thinking into one growing string; update display periodically
                    _snip = (event.summary or "").strip()
                    if _snip.startswith("Thinking: "):
                        _snip = _snip[len("Thinking: "):]
                    if _snip and _snip != "Thinking...":
                        _verbose_think_accum += (" " if _verbose_think_accum else "") + _snip
                    _think_now = time.time()
                    _should_flush = (
                        len(_verbose_think_accum) >= 80
                        or (_verbose_think_accum and _verbose_think_accum[-1] in ".!?,;:")
                        or (_verbose_think_accum and (_think_now - _verbose_think_last_flush) >= 6.0)
                    )
                    if _should_flush and _verbose_think_accum:
                        # Show trailing 160 chars of all accumulated thinking so far
                        _display = _verbose_think_accum[-160:]
                        if buffer and buffer[-1].startswith("💭"):
                            buffer[-1] = f"{icon} {_display}"
                        else:
                            buffer.append(f"{icon} {_display}")
                        # Do NOT reset _verbose_think_accum — keep growing it
                        _verbose_think_last_flush = time.time()
                elif event.kind == KIND_TOOL_START:
                    # Replace last tool_start line if consecutive (avoids fragmented JSON input spam)
                    if buffer and buffer[-1].startswith("🔧"):
                        buffer[-1] = f"{icon} {event.summary[:100]}"
                    else:
                        buffer.append(f"{icon} {event.summary[:100]}")
                else:
                    buffer.append(f"{icon} {event.summary[:100]}")

                # Trim buffer
                if len(buffer) > MAX_LINES * 2:
                    buffer[:] = buffer[-MAX_LINES:]

                dirty = True
            except asyncio.TimeoutError:
                # No event within interval — show elapsed waiting indicator
                elapsed_s = int(time.time() - started)
                wait_line = f"⏳ Waiting... ({elapsed_s}s)"
                if buffer and buffer[-1].startswith("⏳"):
                    buffer[-1] = wait_line
                else:
                    buffer.append(wait_line)
                dirty = True

            # Edit if dirty and enough time passed
            now = time.time()
            if dirty and (now - last_edit_at) >= MIN_EDIT_INTERVAL:
                await _edit_placeholder()

        # Flush any remaining accumulated thinking snippet
        if _verbose_think_accum:
            _display = _verbose_think_accum[:120]
            if buffer and buffer[-1].startswith("💭"):
                buffer[-1] = f"💭 {_display}"
            else:
                buffer.append(f"💭 {_display}")
            _verbose_think_accum = ""

        # Final edit with completion indicator
        if buffer:
            elapsed = int(time.time() - started)
            buffer.append(f"✅ Done ({elapsed}s)")
            await _edit_placeholder()

    def _make_stream_callback(self, event_queue: asyncio.Queue | None = None,
                              think_buffer: list | None = None):
        """Create an async callback that puts StreamEvents into the queue
        and/or appends all events to the think buffer."""
        from adapters.stream_events import KIND_THINKING
        _logger = self.logger
        _engine = self.config.engine
        _chunk_target = 100
        _chunk_hard_limit = 150
        _chunk_endings = ("。", "！", "？", "\n")
        async def _callback(event):
            if event_queue is not None:
                try:
                    event_queue.put_nowait(event)
                except asyncio.QueueFull:
                    _logger.debug(f"Stream event queue full, dropping: {event.summary[:40]!r}")
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
                        _logger.info(
                            f"Think buffer append: kind={event.kind}, summary={self._openrouter_think_chunk[:60]!r}, buf_len={len(think_buffer)}"
                        )
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
        self.append_conversation_entry("thinking", f"💭 {text}", "think")
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
        """Periodically flush accumulated thinking traces every 60 seconds."""
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60)
                break  # stop_event was set
            except asyncio.TimeoutError:
                pass  # 60s elapsed — flush
            await self._flush_thinking(chat_id)

    # ------------------------------------------------------------------
    # Stage 4: Background-mode helpers
    # ------------------------------------------------------------------

    def _register_background_task(self, gen_task: asyncio.Task, item: "QueuedRequest") -> None:
        """Track a detached generation task and wire up its completion callback."""
        self._background_tasks.add(gen_task)

        def _on_done(task: asyncio.Task) -> None:
            self._background_tasks.discard(task)
            # Schedule the async completion handler back in the running event loop.
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(self._on_background_complete(task, item))
            except RuntimeError:
                pass  # loop closed during shutdown

        gen_task.add_done_callback(_on_done)

    async def _on_background_complete(self, task: asyncio.Task, item: "QueuedRequest") -> None:
        """Called when a background generate_response task finishes."""
        if self.is_shutting_down:
            return  # agent is going down — skip sending

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
                    f"⚠️ Background task error ({self.config.engine}): {exc}",
                    request_id=item.request_id,
                    purpose="bg-error",
                )
                return

            response = task.result()
            audit_meta = self._request_audit_meta.pop(item.request_id, None)

            if response.is_success and response.text:
                if audit_meta:
                    self._record_request_usage_and_audit(
                        item,
                        response,
                        effective_prompt=audit_meta["effective_prompt"],
                        final_prompt=audit_meta["final_prompt"],
                        prompt_audit=audit_meta["prompt_audit"],
                        tool_audit=audit_meta["tool_audit"],
                        queue_wait_s=audit_meta.get("queue_wait_s"),
                        backend_elapsed_s=(datetime.now() - datetime.fromisoformat(item.created_at)).total_seconds(),
                        detached=True,
                        background_completion=True,
                    )
                self._mark_success()
                self._record_habit_outcome(item, success=True, response_text=response.text)
                self.last_response = {
                    "chat_id": item.chat_id,
                    "text": response.text,
                    "request_id": item.request_id,
                    "responded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
                memory_user_text = item.prompt
                if item.source.lower() in {"document", "photo", "voice", "audio", "video", "sticker"}:
                    memory_user_text = f"[{item.source}] {item.summary}"
                if item.source not in {"startup", "system"}:
                    self.memory_store.record_turn("user", item.source, memory_user_text)
                    self.memory_store.record_turn("assistant", self.config.engine, response.text)
                    self.memory_store.record_exchange(memory_user_text, response.text, item.source)
                self.append_conversation_entry("assistant", response.text, self.config.engine)
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
                if audit_meta:
                    self._record_request_usage_and_audit(
                        item,
                        response,
                        effective_prompt=audit_meta["effective_prompt"],
                        final_prompt=audit_meta["final_prompt"],
                        prompt_audit=audit_meta["prompt_audit"],
                        tool_audit=audit_meta["tool_audit"],
                        queue_wait_s=audit_meta.get("queue_wait_s"),
                        backend_elapsed_s=(datetime.now() - datetime.fromisoformat(item.created_at)).total_seconds(),
                        detached=True,
                        background_completion=True,
                    )
                err_msg = response.error or "Unknown error"
                self._mark_error(err_msg)
                self._record_habit_outcome(item, success=False, error_text=err_msg)
                self.error_logger.error(f"Background task {item.request_id} failed: {err_msg}")
                clipped = err_msg if len(err_msg) <= 3000 else err_msg[:2800].rstrip() + "\n\n[truncated]"
                await self.send_long_message(
                    item.chat_id,
                    f"⚠️ Background task error ({self.config.engine}): {clipped}",
                    request_id=item.request_id,
                    purpose="bg-error",
                )

        except Exception as e:
            self._mark_error(str(e))
            self.error_logger.exception(
                f"Unhandled error in _on_background_complete for {item.request_id}: {e}"
            )

    async def send_long_message(
        self,
        chat_id: int,
        text: str,
        request_id: str | None = None,
        purpose: str = "response",
    ):
        # Guard: skip Telegram send if not connected
        if not self.telegram_connected:
            self.logger.info(
                f"Telegram disconnected — skipping send for {request_id or 'unknown'} "
                f"(purpose={purpose}, text_len={len(text)})"
            )
            return 0.0, 0

        send_started = datetime.now()
        html = _md_to_html(text)
        tg_max_len = 4096
        chunk_count = 0

        async def _send_chunk(chunk_raw: str, chunk_html: str, chunk_index: int):
            for attempt in range(3):
                try:
                    await self.app.bot.send_message(
                        chat_id=chat_id,
                        text=chunk_html,
                        parse_mode=constants.ParseMode.HTML,
                    )
                    return
                except (ConnectionError, OSError, TelegramNetworkError) as e:
                    # Network-level errors — retry after brief pause
                    if attempt < 2:
                        self.telegram_logger.warning(
                            f"Send network error for request_id={request_id or '<none>'} "
                            f"(chunk={chunk_index}, attempt={attempt + 1}/3): {e}"
                        )
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    raise
                except Exception as e:
                    self.telegram_logger.warning(
                        f"Send failed for request_id={request_id or '<none>'} "
                        f"(purpose={purpose}, chunk={chunk_index}, mode=html): {e}. Fallback to raw text."
                    )
                    await self.app.bot.send_message(chat_id=chat_id, text=chunk_raw)
                    return

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

    def _extract_task_id(self, summary: str) -> str | None:
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

    def _get_tool_audit(self) -> dict[str, int]:
        registry = getattr(self.backend, "tool_registry", None)
        if registry is None:
            return {"catalog_count": 0, "schema_chars": 0, "schema_fingerprint": "", "max_loops": 0}
        defs = []
        schema_chars = 0
        schema_fingerprint = ""
        try:
            defs = registry.get_tool_definitions()
            schema_json = json.dumps(defs, ensure_ascii=False, sort_keys=True)
            schema_chars = len(schema_json)
            schema_fingerprint = hashlib.sha1(schema_json.encode("utf-8")).hexdigest()[:16]
        except Exception:
            pass
        return {
            "catalog_count": len(defs),
            "schema_chars": schema_chars,
            "schema_fingerprint": schema_fingerprint,
            "max_loops": int(getattr(registry, "max_loops", 0) or 0),
        }

    def _build_audit_record(
        self,
        item: QueuedRequest,
        response,
        *,
        effective_prompt: str,
        final_prompt: str,
        prompt_audit: dict,
        tool_audit: dict[str, int],
        queue_wait_s: float | None = None,
        backend_elapsed_s: float | None = None,
        detached: bool = False,
        background_completion: bool = False,
    ) -> dict:
        from tools.token_tracker import estimate_tokens

        def _ascii_token_est(char_count: int) -> int:
            return max(1, int(char_count * 0.25)) if char_count else 0

        input_tokens = response.usage.input_tokens if response.usage else estimate_tokens(final_prompt)
        output_tokens = response.usage.output_tokens if response.usage else estimate_tokens(response.text or "")
        thinking_tokens = response.usage.thinking_tokens if response.usage else 0
        section_chars = {section["key"]: section["chars"] for section in prompt_audit.get("sections", [])}
        section_tokens = {
            section["key"]: section.get("tokens_est") or _ascii_token_est(section["chars"])
            for section in prompt_audit.get("sections", [])
        }
        return {
            "request_id": item.request_id,
            "agent": self.name,
            "runtime": "fixed",
            "backend": self.config.engine,
            "model": self.config.model,
            "source": item.source,
            "summary": item.summary,
            "silent": item.silent,
            "is_retry": item.is_retry,
            "success": response.is_success,
            "incremental_mode": False,
            "detached": detached,
            "background_completion": background_completion,
            "token_source": "api" if response.usage else "estimated",
            "raw_prompt_chars": len(item.prompt),
            "effective_prompt_chars": len(effective_prompt),
            "final_prompt_chars": len(final_prompt),
            "response_chars": len(response.text or ""),
            "error_chars": len(response.error or ""),
            "queue_wait_s": round(queue_wait_s, 3) if queue_wait_s is not None else None,
            "backend_elapsed_s": round(backend_elapsed_s, 3) if backend_elapsed_s is not None else None,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "thinking_tokens": thinking_tokens,
            "tool_call_count": int(getattr(response, "tool_call_count", 0) or 0),
            "tool_loop_count": int(getattr(response, "tool_loop_count", 0) or 0),
            "tool_catalog_count": tool_audit["catalog_count"],
            "tool_schema_chars": tool_audit["schema_chars"],
            "tool_schema_tokens_est": _ascii_token_est(tool_audit["schema_chars"]),
            "tool_schema_fingerprint": tool_audit.get("schema_fingerprint", ""),
            "tool_max_loops": tool_audit["max_loops"],
            "budget_applied": bool(prompt_audit.get("budget_applied")),
            "budget_limit_chars": prompt_audit.get("budget_limit_chars"),
            "context_chars_before_budget": prompt_audit.get("context_chars_before_budget", 0),
            "time_fyi_chars": prompt_audit.get("time_fyi_chars", 0),
            "context_expansion_ratio": round(len(final_prompt) / max(len(item.prompt), 1), 3),
            "context_fingerprint": prompt_audit.get("context_fingerprint", ""),
            "request_fingerprint": hashlib.sha1((item.prompt or "").encode("utf-8")).hexdigest()[:16],
            "section_chars": section_chars,
            "section_tokens_est": section_tokens,
            "section_counts": {
                section["key"]: section.get("item_count", 0)
                for section in prompt_audit.get("sections", [])
            },
        }

    def _record_request_usage_and_audit(
        self,
        item: QueuedRequest,
        response,
        *,
        effective_prompt: str,
        final_prompt: str,
        prompt_audit: dict,
        tool_audit: dict[str, int],
        queue_wait_s: float | None = None,
        backend_elapsed_s: float | None = None,
        detached: bool = False,
        background_completion: bool = False,
    ) -> None:
        try:
            from tools.token_tracker import estimate_tokens, record_audit_event, record_usage

            if response.usage:
                input_tokens = response.usage.input_tokens
                output_tokens = response.usage.output_tokens
                thinking_tokens = response.usage.thinking_tokens
            else:
                input_tokens = estimate_tokens(final_prompt)
                output_tokens = estimate_tokens(response.text or "")
                thinking_tokens = 0

            record_usage(
                self.workspace_dir,
                model=self.config.model,
                backend=self.config.engine,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                thinking_tokens=thinking_tokens,
                session_id=self.session_id_dt,
            )
            record_audit_event(
                self.workspace_dir,
                self._build_audit_record(
                    item,
                    response,
                    effective_prompt=effective_prompt,
                    final_prompt=final_prompt,
                    prompt_audit=prompt_audit,
                    tool_audit=tool_audit,
                    queue_wait_s=queue_wait_s,
                    backend_elapsed_s=backend_elapsed_s,
                    detached=detached,
                    background_completion=background_completion,
                ),
            )
        except Exception:
            pass

    async def process_queue(self):
        self.logger.info("Agent queue processor started.")
        while True:
            item = None
            try:
                item = await self.queue.get()
            except asyncio.CancelledError:
                break

            try:
                if not item.silent:
                    self.last_prompt = item
                is_bridge_request = item.source.startswith("bridge:")
                queued_at = datetime.fromisoformat(item.created_at)
                queue_wait_s = (datetime.now() - queued_at).total_seconds()
                self.logger.info(
                    f"Processing {item.request_id} via {self.config.engine} "
                    f"(source={item.source}, silent={item.silent}, prompt_len={len(item.prompt)}, "
                    f"queue_wait_s={queue_wait_s:.2f})"
                )
                self.current_request_meta = {
                    "request_id": item.request_id,
                    "source": item.source,
                    "summary": item.summary,
                    "started_at": datetime.now().isoformat(),
                }
                self.is_generating = True
                self._mark_activity()
                self._log_maintenance(
                    item,
                    "processing",
                    engine=self.config.engine,
                    silent=item.silent,
                    prompt_len=len(item.prompt),
                    queue_wait_s=f"{queue_wait_s:.2f}",
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

                    # Verbose ON + non-silent → use streaming display loop.
                    # Verbose OFF → use escalating placeholder loop.
                    # Think ON → start thinking flush loop (independent of verbose).
                    _stream_queue = None
                    _stream_callback = None
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
                                backend=self.backend,
                            )
                        )
                    else:
                        escalation_task = asyncio.create_task(
                            self._escalating_placeholder_loop(
                                item.chat_id,
                                placeholder,
                                item.request_id,
                                stop_typing,
                                backend=self.backend,
                            )
                        )
                    if self._think:
                        self._think_buffer.clear()
                        self._openrouter_think_chunk = ""
                        self._last_openrouter_think_snippet = None
                        _think_flush_task = asyncio.create_task(
                            self._thinking_flush_loop(item.chat_id, stop_typing)
                        )

                backend_started = datetime.now()
                effective_prompt = self._consume_session_primer(item)
                habit_sections, habit_ids = self._build_habit_sections(item, effective_prompt)
                self.current_request_meta["habit_ids"] = habit_ids
                prompt_payload = self.context_assembler.build_prompt_payload(
                    effective_prompt,
                    self.config.engine,
                    extra_sections=habit_sections,
                    inject_memory=not item.skip_memory_injection,
                )
                final_prompt = prompt_payload["final_prompt"]
                prompt_audit = prompt_payload["audit"]
                tool_audit = self._get_tool_audit()
                self._request_audit_meta[item.request_id] = {
                    "effective_prompt": effective_prompt,
                    "final_prompt": final_prompt,
                    "prompt_audit": prompt_audit,
                    "tool_audit": tool_audit,
                    "queue_wait_s": queue_wait_s,
                }
                if item.is_retry:
                    self.logger.warning(
                        f"Running retry for {self._extract_task_id(item.summary) or '<none>'} "
                        f"(request_id={item.request_id})."
                    )
                if self.config.engine == "openrouter-api" and hasattr(self.backend, "set_reasoning_enabled"):
                    self.backend.set_reasoning_enabled(self._think)

                # Resolve stream callback (may be None if silent or stream not needed)
                _on_stream = _stream_callback if not item.silent else None

                # --- Stage 4: background-mode detach ---
                # If background_mode is enabled in extra and the task is not silent,
                # wrap generate_response in a shielded task. If it exceeds the detach
                # threshold the task is handed off to the background; the queue loop
                # is freed immediately and the response is sent proactively when done.
                _extra = (self.config.extra or {})
                _bg_mode = _extra.get("background_mode", False) and not item.silent and item.deliver_to_telegram
                _detach_after: float = float(
                    _extra.get("background_detach_after")
                    or (_extra.get("escalation_thresholds") or [30, 60, 90, 150])[-1]
                )

                detached = False
                if _bg_mode:
                    _gen_task = asyncio.create_task(
                        self.backend.generate_response(
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
                else:
                    response = await self.backend.generate_response(
                        final_prompt, item.request_id,
                        is_retry=item.is_retry, silent=item.silent,
                        on_stream_event=_on_stream,
                    )

                if detached:
                    # Stop UI, update placeholder, hand off to background
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
                        f"(threshold={_detach_after}s, engine={self.config.engine})"
                    )
                    self._log_maintenance(item, "bg_detached", detach_after_s=_detach_after)
                    continue  # release queue slot; task runs in background

                backend_elapsed = (datetime.now() - backend_started).total_seconds()
                self.logger.info(
                    f"Backend finished {item.request_id} via {self.config.engine} "
                    f"(success={response.is_success}, elapsed_s={backend_elapsed:.2f}, "
                    f"text_len={len(response.text or '')}, error_len={len(response.error or '')}, "
                    f"final_prompt_len={len(final_prompt)})"
                )
                self._log_maintenance(
                    item,
                    "backend_finished",
                    engine=self.config.engine,
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

                if response.is_success and response.text:
                    self._record_request_usage_and_audit(
                        item,
                        response,
                        effective_prompt=effective_prompt,
                        final_prompt=final_prompt,
                        prompt_audit=prompt_audit,
                        tool_audit=tool_audit,
                        queue_wait_s=queue_wait_s,
                        backend_elapsed_s=backend_elapsed,
                    )
                    self._request_audit_meta.pop(item.request_id, None)
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
                    if item.silent:
                        continue
                    self.last_response = {
                        "chat_id": item.chat_id,
                        "text": response.text,
                        "request_id": item.request_id,
                        "responded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    }
                    memory_user_text = item.prompt
                    if item.source.lower() in {"document", "photo", "voice", "audio", "video", "sticker"}:
                        memory_user_text = f"[{item.source}] {item.summary}"
                    if item.source not in {"startup", "system"} and not is_bridge_request:
                        self.memory_store.record_turn("user", item.source, memory_user_text)
                        self.memory_store.record_turn("assistant", self.config.engine, response.text)
                        self.memory_store.record_exchange(memory_user_text, response.text, item.source)
                    if not is_bridge_request:
                        self.append_conversation_entry("assistant", response.text, self.config.engine)
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
                        f"Completed {item.request_id} delivery via {self.config.engine} "
                        f"(queue_wait_s={queue_wait_s:.2f}, backend_s={backend_elapsed:.2f}, "
                        f"telegram_send_s={send_elapsed_s:.2f}, total_s={total_elapsed_s:.2f}, "
                        f"chunks={chunk_count})"
                    )
                    self._log_maintenance(item, "send_success", text_len=len(response.text or ""))
                    # Route hchat reply back to sender if applicable
                    await self._hchat_route_reply(item, response.text)
                else:
                    self._record_request_usage_and_audit(
                        item,
                        response,
                        effective_prompt=effective_prompt,
                        final_prompt=final_prompt,
                        prompt_audit=prompt_audit,
                        tool_audit=tool_audit,
                        queue_wait_s=queue_wait_s,
                        backend_elapsed_s=backend_elapsed,
                    )
                    self._request_audit_meta.pop(item.request_id, None)
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
                    if item.silent:
                        continue
                    self.error_logger.error(
                        f"Backend error for {item.request_id} ({self.config.engine}, source={item.source}): {err_msg}"
                    )
                    if self._should_retry_codex_scheduler_failure(item, err_msg):
                        self._schedule_codex_scheduler_retry(item)
                    if not item.deliver_to_telegram:
                        continue
                    clipped = err_msg if len(err_msg) <= 3000 else err_msg[:2800].rstrip() + "\n\n[error output truncated]"
                    send_elapsed_s, chunk_count = await self.send_long_message(
                        chat_id=item.chat_id,
                        text=f"Backend Error ({self.config.engine}): {clipped}",
                        request_id=item.request_id,
                        purpose="error",
                    )
                    total_elapsed_s = (datetime.now() - queued_at).total_seconds()
                    self.logger.info(
                        f"Completed {item.request_id} error delivery via {self.config.engine} "
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
                self.error_logger.exception(f"Queue processing error for {getattr(item, 'request_id', '?')}: {e}")
            finally:
                self.is_generating = False
                self.current_request_meta = None
                if item is not None:
                    self.queue.task_done()

    async def download_media(self, file_id: str, filename: str) -> Path:
        tg_file = await self.app.bot.get_file(file_id)
        local_path = self.media_dir / filename
        await tg_file.download_to_drive(local_path)
        self.logger.info(f"Downloaded media: {local_path}")
        return local_path

    async def _handle_media_message(
        self,
        update,
        media_kind: str,
        filename: str,
        file_id: str,
        prompt: str,
        summary: str,
    ):
        if not self.backend.capabilities.supports_files:
            await update.message.reply_text(
                f"{self.config.engine} does not support {media_kind.lower()} attachments yet."
            )
            return

        _print_user_message(self.name, summary, media_tag=media_kind)
        try:
            local_path = await self.download_media(file_id, filename)
            rendered_prompt = prompt.replace("{local_path}", str(local_path))
            self.append_conversation_entry("user", f"[{media_kind}] {summary}", media_kind.lower())
            await self.enqueue_request(update.effective_chat.id, rendered_prompt, media_kind.lower(), summary)
        except Exception as e:
            self.error_logger.exception(
                f"{media_kind} handler failed for file '{filename}' (file_id={file_id}): {e}"
            )
            try:
                await update.message.reply_text(f"Failed to process {media_kind.lower()} message.")
            except Exception as notify_error:
                self.telegram_logger.warning(
                    f"Failed to notify user about {media_kind.lower()} error: {notify_error}"
                )

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
        self.telegram_logger.warning(
            f"Polling error while fetching updates: {type(error).__name__}: {err_text}"
        )
        if getattr(error, "__traceback__", None):
            self.error_logger.error(
                f"Telegram polling error: {type(error).__name__}: {err_text}",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def handle_message(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            self.logger.warning(f"Ignored message from unauthorized user ID: {update.effective_user.id}")
            return
        text = update.message.text
        _print_user_message(self.name, text)
        self._capture_followup_habit_feedback(text)
        self.append_conversation_entry("user", text, "text")
        await self.enqueue_request(update.effective_chat.id, text, "text", _safe_excerpt(text))

    async def handle_document(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
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

        await self._handle_media_message(
            update=update,
            media_kind="Document",
            filename=original_name,
            file_id=doc.file_id,
            prompt=prompt,
            summary=original_name,
        )

    async def handle_photo(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        photo = update.message.photo[-1]
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        caption = update.message.caption or ""
        prompt = "User sent a photo (saved at {local_path})."
        if caption:
            prompt += f' Caption: "{caption}"'
        prompt += " View the image and respond."
        await self._handle_media_message(
            update=update,
            media_kind="Photo",
            filename=f"photo_{ts}.jpg",
            file_id=photo.file_id,
            prompt=prompt,
            summary=caption or f"photo_{ts}.jpg",
        )

    async def handle_voice(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        voice = update.message.voice
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"voice_{ts}.ogg"
        await self._handle_voice_or_audio(update, "Voice", filename, voice.file_id)

    async def handle_audio(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        audio = update.message.audio
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        original_name = audio.file_name or f"audio_{ts}"
        caption = update.message.caption or ""
        await self._handle_voice_or_audio(update, "Audio", original_name, audio.file_id, caption=caption)

    async def _handle_voice_or_audio(self, update, media_kind: str, filename: str, file_id: str, caption: str = ""):
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
                if self.backend.capabilities.supports_files:
                    prompt = f"User sent a voice message (saved at {local_path}). Listen to the audio, transcribe it, and respond."
                    self.append_conversation_entry("user", f"[{media_kind}] {filename}", media_kind.lower())
                    await self.enqueue_request(update.effective_chat.id, prompt, media_kind.lower(), filename)
                else:
                    await update.message.reply_text(f"Failed to transcribe {media_kind.lower()} message.")
                return

            # Build prompt from transcript
            _print_user_message(self.name, transcript, media_tag="Transcription")
            prompt = f"[Voice message transcription] {transcript}"
            if caption:
                prompt += f'\nCaption: "{caption}"'

            self.telegram_logger.info(
                f"Transcribed {media_kind.lower()} ({filename}): {len(transcript)} chars"
            )
            self.append_conversation_entry("user", f"[{media_kind}] {transcript[:200]}", "voice_transcript")
            await self.enqueue_request(update.effective_chat.id, prompt, "voice_transcript", f"{media_kind}: {filename}")
        except Exception as e:
            self.error_logger.exception(f"{media_kind} voice handler failed for '{filename}': {e}")
            try:
                await update.message.reply_text(f"Failed to process {media_kind.lower()} message.")
            except Exception:
                pass

    async def handle_video(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        video = update.message.video
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        original_name = video.file_name or f"video_{ts}.mp4"
        caption = update.message.caption or ""
        prompt = f'User sent a video "{original_name}" (saved at {{local_path}}).'
        if caption:
            prompt += f' Caption: "{caption}"'
        prompt += " Watch the video and respond."
        await self._handle_media_message(
            update=update,
            media_kind="Video",
            filename=original_name,
            file_id=video.file_id,
            prompt=prompt,
            summary=original_name,
        )

    async def handle_sticker(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        sticker = update.message.sticker
        emoji = sticker.emoji or ""
        _print_user_message(self.name, emoji or "sticker", media_tag="Sticker")
        prompt = f"User sent a sticker (emoji: {emoji}). React warmly."
        await self.enqueue_request(update.effective_chat.id, prompt, "sticker", emoji or "sticker")

    def _startable_agent_keyboard(self) -> InlineKeyboardMarkup | None:
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            return None
        names = orchestrator.get_startable_agent_names(exclude_name=self.name)
        if not names:
            return None
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton(name, callback_data=f"startagent:{name}")] for name in names]
        )

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

    async def cmd_start(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            await update.message.reply_text("Dynamic lifecycle control is unavailable.")
            return
        keyboard = self._startable_agent_keyboard()
        if keyboard is None:
            await update.message.reply_text("All agents are running.")
            return
        await update.message.reply_text("Start another agent:", reply_markup=keyboard)

    async def callback_start_agent(self, update, context):
        query = update.callback_query
        if query.from_user.id != self.global_config.authorized_id:
            return
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            await query.answer("Lifecycle control unavailable", show_alert=True)
            return
        _, agent_name = (query.data or "").split(":", 1)
        await query.answer(f"Starting {agent_name}...")
        ok, message = await orchestrator.start_agent(agent_name)
        await query.edit_message_text(message, reply_markup=self._startable_agent_keyboard())

    async def callback_voice(self, update, context):
        query = update.callback_query
        if query.from_user.id != self.global_config.authorized_id:
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

    async def cmd_terminate(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            await update.message.reply_text("Dynamic lifecycle control is unavailable.")
            return
        await update.message.reply_text("Shutting down.")
        asyncio.create_task(orchestrator.stop_agent(self.name))

    async def cmd_reboot(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            await update.message.reply_text("Hot restart is unavailable.")
            return
        arg = " ".join(context.args).strip().lower() if context.args else ""
        if arg == "help":
            all_names = orchestrator.configured_agent_names()
            lines = [
                "<b>/reboot</b> — restart all running agents (same selection)",
                "<b>/reboot min</b> — restart only this bot",
                "<b>/reboot max</b> — restart all active agents",
                "<b>/reboot [number]</b> — restart a specific agent by number",
                "<b>/reboot help</b> — show this help",
                "",
                "<b>Agents:</b>",
            ]
            for i, name in enumerate(all_names, 1):
                running = name in {rt.name for rt in orchestrator.runtimes}
                marker = "●" if running else "○"
                lines.append(f"  {i}. {marker} {name}")
            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
            return
        if arg == "min":
            mode, label = "min", f"Restarting only <b>{self.name}</b>..."
        elif arg == "max":
            mode, label = "max", "Restarting all active agents..."
        elif arg.isdigit():
            num = int(arg)
            all_names = orchestrator.configured_agent_names()
            if num < 1 or num > len(all_names):
                await update.message.reply_text(f"Invalid agent number. Use 1–{len(all_names)}. /reboot help to list.")
                return
            mode, label = "number", f"Restarting agent #{num} (<b>{all_names[num - 1]}</b>)..."
        else:
            mode, label = "same", "Restarting all running agents..."
        await update.message.reply_text(label, parse_mode="HTML")
        orchestrator.request_restart(mode=mode, agent_name=self.name, agent_number=int(arg) if arg.isdigit() else None)

    async def cmd_handoff(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        if self.is_generating or not self.queue.empty():
            await update.message.reply_text("Handoff is blocked while a request is running or queued.")
            return

        await update.message.reply_text("Restoring recent bridge history into a fresh continuity prompt...")
        self.handoff_builder.refresh_recent_context()
        self.handoff_builder.build_handoff()
        prompt, exchange_count, word_count = self.handoff_builder.build_session_restore_prompt(
            max_rounds=10,
            max_words=6000,
        )
        if exchange_count <= 0:
            await update.message.reply_text("No recent bridge transcript was available for handoff.")
            return

        self._arm_session_primer(
            "This is a bridge-managed handoff restore. Review AGENT FYI, then use the recent transcript as continuity context."
        )
        if self.backend.capabilities.supports_sessions:
            await self.backend.handle_new_session()
            await self.enqueue_startup_bootstrap(update.effective_chat.id)

        await update.message.reply_text(
            f"Handoff prepared from {exchange_count} recent exchanges ({word_count} words). Restoring continuity now..."
        )
        await self.enqueue_request(
            update.effective_chat.id,
            prompt,
            "handoff",
            f"Handoff restore [{exchange_count} exchanges]",
        )

    async def cmd_ticket(self, update, context):
        """Submit an IT support ticket to Arale. Usage: /ticket <description>"""
        if update.effective_user.id != self.global_config.authorized_id:
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
            await update.message.reply_text("\n".join(lines))
            return

        # Create the ticket
        instance = detect_instance(self.global_config.project_root)
        ticket = create_ticket(
            project_root=self.global_config.project_root,
            source_agent=self.name,
            source_instance=instance,
            workspace_dir=self.config.workspace_dir,
            summary=args_text,
        )

        await update.message.reply_text(
            f"🎫 Ticket {ticket['ticket_id']} created.\n"
            f"Arale has been notified and will investigate.",
        )

        # Notify Arale via bridge (local) or hchat (cross-instance)
        notification = format_ticket_notification(ticket)
        orchestrator = getattr(self, "orchestrator", None)
        notified = False

        if orchestrator is not None:
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
                        logging.warning(f"Failed to notify arale via bridge: {e}")
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
                    logging.info(f"Ticket {ticket['ticket_id']} notified to arale via hchat.")
                else:
                    logging.warning(f"Ticket {ticket['ticket_id']} hchat delivery to arale failed.")
            except Exception as e:
                logging.warning(f"Failed to notify arale via hchat: {e}")

        if not notified:
            logging.warning(f"Ticket {ticket['ticket_id']} created but could not notify arale. She will pick it up on next patrol.")

    async def cmd_park(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        args = [a.strip() for a in (context.args or []) if a.strip()]
        if not args:
            await update.message.reply_text(self._format_parked_topics_text())
            return

        action = args[0].lower()
        if action == "delete":
            if len(args) < 2 or not args[1].isdigit():
                await update.message.reply_text("Usage: /park delete <slot>")
                return
            slot_id = int(args[1])
            removed = self.parked_topics.delete_topic(slot_id)
            if not removed:
                await update.message.reply_text(f"Parked topic [{slot_id}] was not found.")
                return
            await update.message.reply_text(f"Deleted parked topic [{slot_id}] {removed.get('title') or ''}".strip())
            return

        if action != "chat":
            await update.message.reply_text(
                "Usage:\n"
                "/park - list parked topics\n"
                "/park chat [optional title] - park the current topic\n"
                "/park delete <slot> - delete a parked topic"
            )
            return

        if self.is_generating or not self.queue.empty():
            await update.message.reply_text("Parking is blocked while a request is running or queued.")
            return

        title_override = " ".join(args[1:]).strip() or None
        await update.message.reply_text("Parking the current topic and writing a resume summary...")
        summary = await self._summarize_current_topic_for_parking(title_override=title_override)
        if not summary:
            await update.message.reply_text("No recent bridge transcript was available to park.")
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
        await update.message.reply_text(
            f"Parked as [{slot_id}] {topic['title']}\n"
            f"{topic['summary_short']}\n\n"
            f"Follow-up reminders are scheduled for this parked topic (up to 3 attempts).\n"
            f"Use /load {slot_id} to resume or /park delete {slot_id} to remove it."
        )

    async def cmd_load(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        args = [a.strip() for a in (context.args or []) if a.strip()]
        if len(args) != 1 or not args[0].isdigit():
            await update.message.reply_text("Usage: /load <slot>")
            return
        if self.is_generating or not self.queue.empty():
            await update.message.reply_text("Load is blocked while a request is running or queued.")
            return

        slot_id = int(args[0])
        topic = self.parked_topics.get_topic(slot_id)
        if not topic:
            await update.message.reply_text(f"Parked topic [{slot_id}] was not found.")
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
        await update.message.reply_text(
            f"Loading parked topic [{slot_id}] {title} and restoring continuity..."
        )
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

    async def cmd_active(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        if not self.skill_manager:
            await update.message.reply_text("Active mode is unavailable because the skill manager is not configured.")
            return

        args = [a.strip().lower() for a in (context.args or []) if a.strip()]
        if not args:
            await update.message.reply_text(self.skill_manager.describe_active_heartbeat(self.name))
            return

        mode = args[0]
        if mode == "off":
            _, message = self.skill_manager.set_active_heartbeat(self.name, enabled=False)
            await update.message.reply_text(message)
            return
        if mode != "on":
            await update.message.reply_text("Usage: /active on [minutes] | /active off")
            return

        minutes = self.skill_manager.ACTIVE_HEARTBEAT_DEFAULT_MINUTES
        if len(args) > 1:
            try:
                minutes = max(1, int(args[1]))
            except ValueError:
                await update.message.reply_text("Minutes must be a positive integer. Usage: /active on [minutes]")
                return

        _, message = self.skill_manager.set_active_heartbeat(self.name, enabled=True, minutes=minutes)
        await update.message.reply_text(message)

    async def cmd_fyi(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        prompt = self._build_fyi_request_prompt(" ".join(context.args or []))
        await update.message.reply_text("Refreshing AGENT FYI...")
        await self.enqueue_request(
            update.effective_chat.id,
            prompt,
            "fyi",
            "AGENT FYI refresh",
        )

    async def cmd_sys(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        args = [a.strip() for a in (context.args or []) if a.strip()]
        mgr = self.sys_prompt_manager

        if not args:
            await update.message.reply_text(mgr.display_all(), parse_mode="Markdown")
            return

        slot = args[0]
        if slot not in mgr.SLOTS:
            await update.message.reply_text(f"Invalid slot '{slot}'. Use 1–10.")
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
                "Usage:\n/sys — show all slots\n/sys <n> — show slot\n"
                "/sys <n> on|off|delete\n/sys <n> save <msg>\n/sys <n> replace <msg>"
            )

    async def cmd_voice(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        args = [a.strip() for a in (context.args or []) if a.strip()]
        if not args or args[0].lower() == "status":
            await update.message.reply_text(
                self.voice_manager.voice_menu_text(),
                reply_markup=self._voice_keyboard(),
            )
            return
        mode = args[0].lower()
        if mode in {"providers", "list"}:
            await update.message.reply_text(self.voice_manager.provider_hints())
            return
        if mode in {"voices", "menu"}:
            await update.message.reply_text(
                self.voice_manager.voice_menu_text(),
                reply_markup=self._voice_keyboard(),
            )
            return
        if mode == "use":
            if len(args) == 1:
                await update.message.reply_text("Usage: /voice use <alias>")
                return
            try:
                await update.message.reply_text(self.voice_manager.apply_voice_preset(args[1]))
            except Exception as e:
                await update.message.reply_text(str(e))
            return
        if mode == "provider":
            if len(args) == 1:
                await update.message.reply_text(f"Current voice provider: {self.voice_manager.get_provider_name()}")
                return
            try:
                await update.message.reply_text(self.voice_manager.set_provider(args[1]))
            except Exception as e:
                await update.message.reply_text(str(e))
            return
        if mode == "name":
            if len(args) == 1:
                await update.message.reply_text("Usage: /voice name <voice-name>")
                return
            await update.message.reply_text(self.voice_manager.set_voice_name(" ".join(args[1:])))
            return
        if mode == "rate":
            if len(args) == 1:
                await update.message.reply_text("Usage: /voice rate <integer>")
                return
            try:
                await update.message.reply_text(self.voice_manager.set_rate(int(args[1])))
            except ValueError:
                await update.message.reply_text("Voice rate must be an integer.")
            return
        if mode == "on":
            await update.message.reply_text(self.voice_manager.set_enabled(True))
            return
        if mode == "off":
            await update.message.reply_text(self.voice_manager.set_enabled(False))
            return
        await update.message.reply_text("Usage: /voice [status|on|off|voices|use <alias>|providers|provider <name>|name <voice>|rate <n>]")

    async def _invoke_prompt_skill_from_command(self, update, skill_id: str, args: list[str]):
        if not self.skill_manager:
            await update.message.reply_text("Skill system is not configured.")
            return
        skill = self.skill_manager.get_skill(skill_id)
        if skill is None:
            await update.message.reply_text(f"Unknown skill: {skill_id}")
            return
        if skill.type != "prompt":
            await update.message.reply_text(f"Skill '{skill_id}' is not a prompt skill.")
            return
        prompt_text = " ".join(args or []).strip()
        if not prompt_text:
            await update.message.reply_text(f"Usage: /{skill_id} <prompt>")
            return
        if skill.backend and skill.backend != self.config.engine:
            await update.message.reply_text(
                f"Skill '{skill.id}' targets {skill.backend}, but this agent uses {self.config.engine}."
            )
            return
        prompt = self.skill_manager.build_prompt_for_skill(skill, prompt_text)
        await update.message.reply_text(f"Running skill {skill.id}...")
        await self.enqueue_request(
            update.effective_chat.id,
            prompt,
            f"skill:{skill.id}",
            f"Skill {skill.id}",
        )

    async def cmd_debug(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        raw_args = list(context.args or [])
        args = [a.strip().lower() for a in raw_args if a.strip()]
        if args and args[0] in {"on", "off"}:
            enabled = args[0] == "on"
            if self.skill_manager:
                _, msg = self.skill_manager.set_toggle_state(self.workspace_dir, "debug", enabled=enabled)
                state_str = "ON 🔴" if enabled else "OFF"
                await update.message.reply_text(f"🐛 Debug mode: {state_str}\n{msg}")
            else:
                await update.message.reply_text("Skill manager not available.")
            return
        if not self.skill_manager:
            await update.message.reply_text("Skill system is not configured.")
            return
        skill = self.skill_manager.get_skill("debug")
        if skill is None:
            await update.message.reply_text("Unknown skill: debug")
            return
        prompt_text = " ".join(raw_args).strip()
        if not prompt_text:
            await update.message.reply_text("Usage: /debug <prompt> or /debug on|off")
            return
        prompt = self.skill_manager.build_prompt_for_skill(skill, prompt_text)
        await update.message.reply_text(f"Running skill {skill.id}...")
        await self.enqueue_request(
            update.effective_chat.id,
            prompt,
            f"skill:{skill.id}",
            f"Skill {skill.id}",
        )

    async def cmd_skill(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        if not self.skill_manager:
            await update.message.reply_text("Skill system is not configured.")
            return

        args = list(context.args or [])
        if not args:
            await update.message.reply_text("Skills", reply_markup=self._skill_keyboard())
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
            await update.message.reply_text("\n".join(lines).strip())
            return

        skill = self.skill_manager.get_skill(sub)
        if skill is None:
            await update.message.reply_text(f"Unknown skill: {sub}")
            return

        rest = " ".join(args[1:]).strip()
        if skill.id in {"cron", "heartbeat"} and not rest:
            await self._render_skill_jobs(update, skill.id)
            return

        if skill.type == "toggle":
            if rest.lower() in {"on", "off"}:
                ok, message = self.skill_manager.set_toggle_state(
                    self.config.workspace_dir,
                    skill.id,
                    enabled=(rest.lower() == "on"),
                )
                await update.message.reply_text(message, reply_markup=self._skill_action_keyboard(skill))
                return
            await update.message.reply_text(
                self.skill_manager.describe_skill(skill, self.config.workspace_dir),
                reply_markup=self._skill_action_keyboard(skill),
            )
            return

        if skill.type == "action":
            ok, message = await self.skill_manager.run_action_skill(skill, self.config.workspace_dir, args=rest)
            await self.send_long_message(
                chat_id=update.effective_chat.id,
                text=message,
                request_id=f"skill-{skill.id}",
                purpose="skill-action",
            )
            return

        if not rest:
            await update.message.reply_text(
                self.skill_manager.describe_skill(skill, self.config.workspace_dir),
                reply_markup=self._skill_action_keyboard(skill),
            )
            return

        if skill.backend and skill.backend != self.config.engine:
            await update.message.reply_text(
                f"Skill '{skill.id}' targets {skill.backend}, but this agent uses {self.config.engine}."
            )
            return

        prompt = self.skill_manager.build_prompt_for_skill(skill, rest)
        await update.message.reply_text(f"Running skill {skill.id}...")
        await self.enqueue_request(
            update.effective_chat.id,
            prompt,
            f"skill:{skill.id}",
            f"Skill {skill.id}",
        )

    async def callback_skill(self, update, context):
        query = update.callback_query
        if query.from_user.id != self.global_config.authorized_id:
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
                        self.skill_manager.describe_skill(skill, self.config.workspace_dir),
                        reply_markup=self._skill_action_keyboard(skill),
                    )
                await query.answer()
                return
            if action == "toggle" and rest:
                enabled = rest[0] == "on"
                ok, message = self.skill_manager.set_toggle_state(self.config.workspace_dir, skill.id, enabled=enabled)
                await query.edit_message_text(
                    message,
                    reply_markup=self._skill_action_keyboard(skill),
                )
                await query.answer()
                return
            if action == "run":
                ok, message = await self.skill_manager.run_action_skill(skill, self.config.workspace_dir)
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
            exported = self.export_daily_transcript(datetime.now())
            text = "Transcript exported." if exported else "No transcript entries to export."
            await self.send_long_message(
                chat_id=self.global_config.authorized_id,
                text=text,
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
                chat_id=self.global_config.authorized_id,
                text=f"Job {job.get('id')} has no prompt.",
                request_id=f"job-{job.get('id')}",
                purpose="skill-job-run",
            )
            return
        summary_prefix = "Heartbeat Task" if "interval_seconds" in job else "Cron Task"
        await self.enqueue_request(
            chat_id=self.global_config.authorized_id,
            prompt=prompt,
            source="scheduler",
            summary=f"{summary_prefix} [{job.get('id')}]",
        )

    async def cmd_help(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        lines = [
            f"Agent {self.name} Commands",
            "",
            "/help - Show this menu",
            "/start - Start another stopped agent",
            "/status [full] - View agent status",
            "/voice [status|voices|use|on|off] - Native voice replies",
            "/active [on|off] [minutes] - Proactive follow-up heartbeat",
            "/handoff - Fresh continuity restore from recent chat",
            "/park - List parked topics",
            "/park chat [title] - Park the current topic with summary + reminders",
            "/park delete <slot> - Delete a parked topic",
            "/load <slot> - Restore a parked topic into active context",
            "/fyi [prompt] - Refresh bridge environment awareness",
            "/model - View or change model",
            "/new - Start a fresh session",
            "/clear - Clear local media/history and start fresh",
            "/stop - Stop current execution and clear queued messages",
            "/terminate - Shut down this agent",
            "/retry [response|prompt] - Resend last response (default) or rerun last prompt",
            "/debug <prompt> - Run the task in strict debug mode",
            "/skill - Browse and run skills",
            "/think - Toggle thinking trace display [on|off]",
            "/verbose [on|off] - Toggle verbose long-task status display",
            "/wa_on - Start WhatsApp transport and show QR in console if needed",
            "/wa_off - Stop WhatsApp transport",
            "/wa_send <number> <message> - Send a WhatsApp message through bridge-u-f",
        ]
        if self.config.engine == "openrouter-api":
            lines.append("/credit - Show OpenRouter key balance info")
        if self.config.engine == "claude-cli":
            lines.append("/effort - View or change Claude effort level")
        if self.config.engine == "codex-cli":
            lines.append("/effort - View or change Codex reasoning level")
        await update.message.reply_text("\n".join(lines))

    async def cmd_new(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        # /new semantics (author intent): start stateless and ONLY rely on the agent's own agent.md
        # - No Bridge FYI injection
        # - No README/doc auto-reading claims
        # - No continuity restore
        self._pending_auto_recall_context = None

        if self.backend.capabilities.supports_sessions:
            await self.backend.handle_new_session()
            await update.message.reply_text("Starting a fresh session...")
        else:
            await update.message.reply_text("Starting a fresh stateless session...")

        prompt = (
            "SYSTEM: Fresh session started. Do not reference any previous chat. "
            "Follow ONLY your agent.md instructions. Ask the user what they want to do next."
        )
        await self.enqueue_request(
            update.effective_chat.id, prompt, "system", "New session",
            skip_memory_injection=True,
        )

    async def cmd_status(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        detailed = bool(context.args and context.args[0].strip().lower() in {"full", "all", "more"})
        await update.message.reply_text(self._build_status_text(detailed=detailed))
        return
        lines = [
            f"Agent: {self.name}",
            f"Engine: {self.config.engine}",
            f"Model: {self.config.model}",
            f"Workspace: {self.config.workspace_dir.name}",
            f"Verbose: {'ON 🔍' if self._verbose else 'OFF'}",
        ]
        if self.skill_manager:
            active_skills = sorted(self.skill_manager.get_active_toggle_ids(self.config.workspace_dir))
            lines.append(f"Active skills: {', '.join(active_skills) if active_skills else 'none'}")
            lines.append(self.skill_manager.describe_active_heartbeat(self.name).replace("\n", " | "))
        if self.config.engine == "openrouter-api":
            lines.append("Session mode: stateless (bridge-managed memory)")
            lines.append(f"Thinking display: {'ON — traces sent as permanent messages every ~60s' if self._think else 'OFF'}")
        elif self.config.engine == "claude-cli":
            lines.append(f"Effort: {getattr(self.backend, 'effort', 'low')}")
            lines.append("Session mode: stateless (bridge-managed memory)")
        elif self.config.engine == "codex-cli":
            lines.append(f"Effort: {self._get_current_effort()}")
            lines.append("Session mode: stateless (bridge-managed memory)")
        elif self.backend.capabilities.supports_sessions:
            lines.append("Session mode: bridge-managed")
        else:
            lines.append("Session mode: stateless (bridge-managed memory)")
        await update.message.reply_text("\n".join(lines))

    def _get_available_models(self) -> list[str]:
        if self.config.engine == "gemini-cli":
            return AVAILABLE_GEMINI_MODELS
        elif self.config.engine == "openrouter-api":
            return AVAILABLE_OPENROUTER_MODELS
        elif self.config.engine == "claude-cli":
            return AVAILABLE_CLAUDE_MODELS
        elif self.config.engine == "codex-cli":
            return AVAILABLE_CODEX_MODELS
        return []

    def _model_keyboard(self) -> InlineKeyboardMarkup:
        models = self._get_available_models()
        buttons = []
        for m in models:
            label = f">> {m}" if m == self.config.model else m
            buttons.append([InlineKeyboardButton(label, callback_data=f"model:{m}")])
        return InlineKeyboardMarkup(buttons)

    async def cmd_model(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return

        args = context.args
        if args:
            requested = args[0].strip()
            if self.config.engine == "claude-cli":
                requested = CLAUDE_MODEL_ALIASES.get(requested.lower(), requested)
            available = self._get_available_models()
            if available and requested not in available:
                await update.message.reply_text(
                    f"Unknown model: {requested}\nUse /model to see available options."
                )
                return
            self.config.model = requested
            await update.message.reply_text(f"Model switched to: {self.config.model}")
            return

        available = self._get_available_models()
        if not available:
            await update.message.reply_text(
                f"Current model: {self.config.model}\nUse /model <name> to switch."
            )
            return

        await update.message.reply_text(
            f"Current model: {self.config.model}\nSelect:",
            reply_markup=self._model_keyboard(),
        )

    def _effort_keyboard(self) -> InlineKeyboardMarkup:
        buttons = []
        current = self._get_current_effort()
        for level in self._get_available_efforts():
            label = f">> {level}" if level == current else level
            buttons.append([InlineKeyboardButton(label, callback_data=f"effort:{level}")])
        return InlineKeyboardMarkup(buttons)

    def _get_available_efforts(self) -> list[str]:
        if self.config.engine == "claude-cli":
            return AVAILABLE_CLAUDE_EFFORTS
        if self.config.engine == "codex-cli":
            return AVAILABLE_CODEX_EFFORTS
        return []

    def _get_current_effort(self) -> str:
        return getattr(self.backend, "effort", ((self.config.extra or {}).get("effort") or "medium"))

    def _set_effort(self, requested: str):
        if hasattr(self.backend, "effort"):
            self.backend.effort = requested
        if self.config.extra is None:
            self.config.extra = {}
        self.config.extra["effort"] = requested

    def _effort_name(self) -> str:
        return "Claude" if self.config.engine == "claude-cli" else "Codex"

    async def cmd_effort(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        available_efforts = self._get_available_efforts()
        if not available_efforts:
            await update.message.reply_text("Effort control is only available for Claude and Codex agents.")
            return

        args = context.args
        if args:
            requested = args[0].strip().lower()
            if requested == "extra":
                requested = "extra_high"
            if requested not in available_efforts:
                await update.message.reply_text(
                    f"Unknown effort level: {requested}\nAvailable: {', '.join(available_efforts)}"
                )
                return
            self._set_effort(requested)
            await update.message.reply_text(f"{self._effort_name()} effort switched to: {requested}")
            return

        await update.message.reply_text(
            f"Current effort: {self._get_current_effort()}\nSelect:",
            reply_markup=self._effort_keyboard(),
        )

    async def callback_model(self, update, context):
        query = update.callback_query
        if query.from_user.id != self.global_config.authorized_id:
            return
        data = query.data
        if data.startswith("model:"):
            model = data.split(":", 1)[1]
            available = self._get_available_models()
            if not available or model in available:
                self.config.model = model
                await query.edit_message_text(
                    f"Model switched to: {self.config.model}",
                    reply_markup=self._model_keyboard(),
                )
        elif data.startswith("effort:") and self._get_available_efforts():
            requested = data.split(":", 1)[1]
            if requested in self._get_available_efforts():
                self._set_effort(requested)
                await query.edit_message_text(
                    f"{self._effort_name()} effort switched to: {requested}",
                    reply_markup=self._effort_keyboard(),
                )
        await query.answer()

    async def cmd_stop(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return

        self.logger.warning(
            f"Manual stop requested for {self.name} "
            f"(queue_size={self.queue.qsize()}, engine={self.config.engine})"
        )
        await self.backend.shutdown()

        dropped = 0
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
                dropped += 1
            except asyncio.QueueEmpty:
                break

        if self.config.engine == "openrouter-api":
            await update.message.reply_text(
                f"Stopped queued work. Cleared {dropped} queued messages. Active HTTP requests may still finish."
            )
        else:
            await update.message.reply_text(f"Stopped task. Cleared {dropped} queued messages and killed active backend process tree.")

    def _load_last_text_from_transcript(self, role: str) -> str | None:
        """Read the last message of the given role from conversation_log.jsonl."""
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

    async def cmd_retry(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        args = [a.strip().lower() for a in (context.args or []) if a.strip()]
        mode = args[0] if args else "response"
        chat_id = update.effective_chat.id
        if mode in {"response", "resp"}:
            if not self.last_response:
                # Try to restore last response from transcript (survives reboot)
                transcript_text = self._load_last_text_from_transcript("assistant")
                if transcript_text:
                    await update.message.reply_text("Restoring last response from transcript...")
                    await self.send_long_message(
                        chat_id=chat_id,
                        text=transcript_text,
                        purpose="retry-response",
                    )
                    return
                # Fallback: re-run last prompt
                if self.last_prompt:
                    await update.message.reply_text("No cached response — retrying last prompt...")
                    await self.enqueue_request(
                        self.last_prompt.chat_id,
                        self.last_prompt.prompt,
                        "retry",
                        "Retry request",
                    )
                else:
                    await update.message.reply_text("Nothing to retry — no previous response or prompt.")
                return
            await update.message.reply_text("Resending last response...")
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
                    await update.message.reply_text("Restoring last prompt from transcript...")
                    await self.enqueue_request(chat_id, transcript_text, "retry", "Retry request")
                else:
                    await update.message.reply_text("No previous prompt to rerun.")
                return
            await update.message.reply_text("Retrying last prompt...")
            await self.enqueue_request(
                self.last_prompt.chat_id,
                self.last_prompt.prompt,
                "retry",
                "Retry request",
            )
            return
        await update.message.reply_text("Usage: /retry [response|prompt]")

    async def cmd_clear(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return

        cleared = 0
        if self.media_dir.exists():
            for file_path in self.media_dir.iterdir():
                if file_path.is_file():
                    try:
                        file_path.unlink()
                        cleared += 1
                    except OSError as e:
                        self.logger.warning(f"Could not delete {file_path.name}: {e}")

        await self.backend.handle_new_session()
        await update.message.reply_text(f"Cleared {cleared} media files and reset session state.")

    async def cmd_verbose(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        args = [a.strip().lower() for a in (context.args or []) if a.strip()]
        if args and args[0] in {"on", "true", "1"}:
            self._verbose = True
        elif args and args[0] in {"off", "false", "0"}:
            self._verbose = False
        else:
            # Toggle if no argument given
            self._verbose = not self._verbose
        # Persist so it survives restarts
        _verbose_file = self.config.workspace_dir / ".verbose"
        if self._verbose:
            _verbose_file.touch()
        else:
            _verbose_file.unlink(missing_ok=True)
        state = "ON 🔍" if self._verbose else "OFF"
        await update.message.reply_text(
            f"Verbose mode: {state}\n"
            f"{'Long-task placeholders will show engine, elapsed, idle time and output events.' if self._verbose else 'Placeholders will show concise status only.'}"
        )

    async def cmd_think(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        args = context.args
        if args and args[0].lower() in ("on", "true", "1"):
            self._think = True
        elif args and args[0].lower() in ("off", "false", "0"):
            self._think = False
        else:
            self._think = not self._think
        _think_file = self.config.workspace_dir / ".think"
        if self._think:
            _think_file.touch()
        else:
            _think_file.unlink(missing_ok=True)
        state = "ON 💭" if self._think else "OFF"
        await update.message.reply_text(
            f"Thinking display: {state}\n"
            f"{'Thinking traces will be sent as permanent italic messages every ~60s during generation.' if self._think else 'Thinking traces will not be displayed.'}"
        )

    async def cmd_jobs(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        arg = (context.args[0].strip().lower() if context.args else "")
        if arg == "all":
            filter_agent = None
        elif arg:
            filter_agent = arg
        else:
            filter_agent = self.name
        text, markup = _build_jobs_with_buttons(self.name, self.skill_manager, filter_agent=filter_agent)
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

    async def cmd_timeout(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        args = [a.strip() for a in (context.args or []) if a.strip()]
        backend = getattr(self, "backend", None)
        extra = {}
        if backend and hasattr(backend, "config") and backend.config.extra:
            extra = backend.config.extra

        default_idle = getattr(type(backend), "DEFAULT_IDLE_TIMEOUT_SEC", 300) if backend else 300
        default_hard = getattr(type(backend), "DEFAULT_HARD_TIMEOUT_SEC", 1800) if backend else 1800

        if not args:
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
            await update.message.reply_text(text, parse_mode="HTML")
            return

        if args[0].lower() == "reset":
            if backend and hasattr(backend, "config") and backend.config.extra:
                backend.config.extra.pop("idle_timeout_sec", None)
                backend.config.extra.pop("hard_timeout_sec", None)
                backend.config.extra.pop("process_timeout", None)
            def_idle_min = default_idle // 60
            def_hard_min = default_hard // 60
            await update.message.reply_text(
                f"⏱ Timeout reset to defaults: idle={def_idle_min} min, hard={def_hard_min} min"
            )
            return

        try:
            idle_min = int(args[0])
            if idle_min <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Usage: /timeout [minutes] [hard_minutes] | reset")
            return

        hard_min = None
        if len(args) >= 2:
            try:
                hard_min = int(args[1])
                if hard_min <= 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("Usage: /timeout [minutes] [hard_minutes] | reset")
                return

        if backend and hasattr(backend, "config"):
            if backend.config.extra is None:
                backend.config.extra = {}
            backend.config.extra["idle_timeout_sec"] = idle_min * 60
            backend.config.extra.pop("process_timeout", None)
            if hard_min is not None:
                backend.config.extra["hard_timeout_sec"] = hard_min * 60

        hard_str = f", hard={hard_min} min" if hard_min is not None else ""
        await update.message.reply_text(f"⏱ Timeout updated: idle={idle_min} min{hard_str}")

    async def cmd_hchat(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        args = [a.strip() for a in (context.args or []) if a.strip()]
        if len(args) < 2:
            await update.message.reply_text(
                "💬 Hchat — Ask this agent to compose & send a message to another agent\n\n"
                "Usage: /hchat <agent> <intent>\n"
                "       /hchat all <intent>  — broadcast to all active agents (excludes temp)\n\n"
                "Example: /hchat lily give her an update on what we've been doing\n"
                "Example: /hchat arale 告诉她新的 debug toggle 功能已完成\n"
                "Example: /hchat all 告诉大家新功能上线了\n\n"
                "Note: YOU compose the message based on context — intent is what to communicate, not the literal text."
            )
            return
        target_name = args[0].lower()
        intent = " ".join(args[1:])

        # Handle "all" target — broadcast to every active agent except temp and self
        if target_name == "all":
            import json as _json
            try:
                _cfg = _json.loads(self.global_config.config_path.read_text(encoding="utf-8-sig"))
                all_agents = [
                    a["name"] for a in _cfg.get("agents", [])
                    if a.get("is_active", True)
                    and a["name"].lower() != "temp"
                    and a["name"].lower() != self.name.lower()
                ]
            except Exception:
                all_agents = []
            if not all_agents:
                await update.message.reply_text("❌ No agents found to broadcast to.")
                return
            agent_list = ", ".join(all_agents)
            send_cmds = "\n".join(
                f'   python tools/hchat_send.py --to {a} --from {self.name} --text "<your composed message>"'
                for a in all_agents
            )
            self_prompt = (
                f"[HCHAT BROADCAST] The user wants you to send a Hchat message to ALL active agents.\n\n"
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
            await update.message.reply_text(f"📢 Broadcasting Hchat to {len(all_agents)} agents...")
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
                f"   python tools/hchat_send.py --to {target_name} --from {self.name} --text \"<your composed message>\"\n"
                f"4. Report back to the user: what you sent and a brief summary of why.\n\n"
                f"Do NOT relay the user's words literally. Compose the message yourself.\n\n"
                f"IMPORTANT: When you later receive a message starting with '[hchat reply from ...]', "
                f"just report the reply content to the user. Do NOT send another hchat message back — "
                f"the conversation ends there."
            )
            await update.message.reply_text(f"💬 Composing Hchat message to {target_name}...")

        await self.enqueue_api_text(
            self_prompt,
            source="bridge:hchat",
            deliver_to_telegram=True,
        )

    async def cmd_logo(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        import asyncio
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _show_logo_animation)
        await update.message.reply_text("Logo displayed in console.")

    async def cmd_credit(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return

        if self.config.engine != "openrouter-api" or not hasattr(self.backend, "get_key_info"):
            await update.message.reply_text("Credit info is only available for OpenRouter agents.")
            return

        key_info = await self.backend.get_key_info()
        if not key_info:
            await update.message.reply_text("Failed to fetch OpenRouter credit info.")
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

    async def cmd_wa_on(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            await update.message.reply_text("WhatsApp lifecycle control is unavailable.")
            return
        ok, message = await orchestrator.start_whatsapp_transport(persist_enabled=True)
        await update.message.reply_text(message)

    async def cmd_wa_off(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            await update.message.reply_text("WhatsApp lifecycle control is unavailable.")
            return
        ok, message = await orchestrator.stop_whatsapp_transport(persist_enabled=True)
        await update.message.reply_text(message)

    async def cmd_wa_send(self, update, context):
        if update.effective_user.id != self.global_config.authorized_id:
            return
        orchestrator = getattr(self, "orchestrator", None)
        if orchestrator is None:
            await update.message.reply_text("WhatsApp send control is unavailable.")
            return
        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text("Usage: /wa_send <+number> <message>")
            return
        phone_number = args[0].strip()
        text = " ".join(args[1:]).strip()
        if not text:
            await update.message.reply_text("Usage: /wa_send <+number> <message>")
            return
        ok, message = await orchestrator.send_whatsapp_text(phone_number, text)
        await update.message.reply_text(message)

    def get_bot_commands(self) -> list[BotCommand]:
        commands = [
            BotCommand("help", "Show help menu"),
            BotCommand("start", "Start another stopped agent"),
            BotCommand("status", "View agent status"),
            BotCommand("voice", "Toggle native voice replies"),
            BotCommand("active", "Toggle proactive heartbeat"),
            BotCommand("handoff", "Fresh continuity restore"),
            BotCommand("ticket", "Submit IT support ticket to Arale"),
            BotCommand("park", "List or save parked topics"),
            BotCommand("load", "Restore a parked topic"),
            BotCommand("fyi", "Refresh bridge environment awareness"),
            BotCommand("model", "View or change model"),
            BotCommand("new", "Start a fresh session"),
            BotCommand("clear", "Clear media/history"),
            BotCommand("stop", "Stop execution"),
            BotCommand("reboot", "Hot restart agents"),
            BotCommand("terminate", "Shut down this agent"),
            BotCommand("retry", "Resend response or rerun prompt"),
            BotCommand("debug", "Run in strict debug mode"),
            BotCommand("skill", "Browse and run skills"),
            BotCommand("think", "Toggle thinking trace display [on|off]"),
            BotCommand("verbose", "Toggle verbose long-task status [on|off]"),
            BotCommand("jobs", "Show cron and heartbeat jobs"),
            BotCommand("timeout", "View or set request timeout [minutes]"),
            BotCommand("hchat", "Send a message to another agent [agent] [message]"),
            BotCommand("logo", "Play startup animation"),
            BotCommand("wa_on", "Start WhatsApp transport"),
            BotCommand("wa_off", "Stop WhatsApp transport"),
            BotCommand("wa_send", "Send a WhatsApp message"),
        ]
        if self.config.engine == "openrouter-api":
            commands.append(BotCommand("credit", "Show OpenRouter balance"))
        if self.config.engine in {"claude-cli", "codex-cli"}:
            commands.append(BotCommand("effort", "View or change effort"))
        return commands

    def bind_handlers(self):
        self.app.add_error_handler(self.handle_telegram_error)
        self.app.add_handler(CommandHandler("help", self.cmd_help))
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("sys", self.cmd_sys))
        self.app.add_handler(CommandHandler("voice", self.cmd_voice))
        self.app.add_handler(CommandHandler("active", self.cmd_active))
        self.app.add_handler(CommandHandler("handoff", self.cmd_handoff))
        self.app.add_handler(CommandHandler("ticket", self.cmd_ticket))
        self.app.add_handler(CommandHandler("park", self.cmd_park))
        self.app.add_handler(CommandHandler("load", self.cmd_load))
        self.app.add_handler(CommandHandler("fyi", self.cmd_fyi))
        self.app.add_handler(CommandHandler("debug", self.cmd_debug))
        self.app.add_handler(CommandHandler("skill", self.cmd_skill))
        self.app.add_handler(CommandHandler("model", self.cmd_model))
        self.app.add_handler(CallbackQueryHandler(self.callback_model, pattern=r"^(model|effort):"))
        self.app.add_handler(CallbackQueryHandler(self.callback_voice, pattern=r"^voice:"))
        self.app.add_handler(CallbackQueryHandler(self.callback_start_agent, pattern=r"^startagent:"))
        self.app.add_handler(CallbackQueryHandler(self.callback_skill, pattern=r"^(skill|skilljob):"))
        self.app.add_handler(CommandHandler("new", self.cmd_new))
        self.app.add_handler(CommandHandler("clear", self.cmd_clear))
        self.app.add_handler(CommandHandler("stop", self.cmd_stop))
        self.app.add_handler(CommandHandler("terminate", self.cmd_terminate))
        self.app.add_handler(CommandHandler("reboot", self.cmd_reboot))
        self.app.add_handler(CommandHandler("retry", self.cmd_retry))
        self.app.add_handler(CommandHandler("think", self.cmd_think))
        self.app.add_handler(CommandHandler("verbose", self.cmd_verbose))
        self.app.add_handler(CommandHandler("credit", self.cmd_credit))
        self.app.add_handler(CommandHandler("effort", self.cmd_effort))
        self.app.add_handler(CommandHandler("jobs", self.cmd_jobs))
        self.app.add_handler(CommandHandler("timeout", self.cmd_timeout))
        self.app.add_handler(CommandHandler("hchat", self.cmd_hchat))
        self.app.add_handler(CommandHandler("logo", self.cmd_logo))
        self.app.add_handler(CommandHandler("wa_on", self.cmd_wa_on))
        self.app.add_handler(CommandHandler("wa_off", self.cmd_wa_off))
        self.app.add_handler(CommandHandler("wa_send", self.cmd_wa_send))
        self.app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), self.handle_message))
        self.app.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
        self.app.add_handler(MessageHandler(filters.VOICE, self.handle_voice))
        self.app.add_handler(MessageHandler(filters.AUDIO, self.handle_audio))
        self.app.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))
        self.app.add_handler(MessageHandler(filters.VIDEO, self.handle_video))
        self.app.add_handler(MessageHandler(filters.Sticker.ALL, self.handle_sticker))

    async def shutdown(self):
        self.logger.info("Initiating runtime shutdown sequence...")
        self.is_shutting_down = True
        for task in list(self._scheduled_retry_tasks):
            task.cancel()
        for task in list(self._scheduled_retry_tasks):
            with suppress(asyncio.CancelledError):
                await task
        # Cancel any in-flight background tasks (is_shutting_down suppresses notifications)
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

        await self.backend.shutdown()
        self._mark_runtime_shutdown(clean=True)

        if self.startup_success:
            for action in (self.app.updater.stop, self.app.stop, self.app.shutdown):
                try:
                    await action()
                except Exception as e:
                    self.error_logger.warning(f"Shutdown warning: {e}")
            self.logger.info("Telegram app shut down cleanly.")
