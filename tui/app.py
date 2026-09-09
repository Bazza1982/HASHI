"""HASHI TUI — Textual-based terminal UI wrapping main.py."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.suggester import SuggestFromList
from textual.widgets import Input, RichLog, Static

from orchestrator.command_specs import COMMAND_SPECS
from orchestrator.runtime_defaults import DEFAULT_WORKBENCH_LOCALHOST_URL
from tui.api_client import TUI_TERMINAL_RUN_STATES, TuiApiClient, run_failure_text
from tui.instances import InstanceResolver, InstanceTarget, load_launch_instance
from tui.light_onboarding import LightOnboardingPhase, is_onboarding_complete
from tui.onboarding import (
    audit_environment,
    lang_code_from_file,
    load_languages,
    verify_openrouter,
    write_config,
)
from tui.sounds import play_message_sound

logger = logging.getLogger(__name__)

STARTUP_LOGO = (
    "  ██╗  ██╗  █████╗ ███████╗██╗  ██╗██╗",
    "  ██║  ██║ ██╔══██╗██╔════╝██║  ██║██║",
    "  ███████║ ███████║███████╗███████║██║",
    "  ██╔══██║ ██╔══██║╚════██║██╔══██║██║",
    "  ██║  ██║ ██║  ██║███████║██║  ██║██║",
    "  ╚═╝  ╚═╝ ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝",
)

TUI_COMMAND_HELP = {
    "help": ("查看 TUI 与 Agent 命令", "Show TUI and Agent commands"),
    "to": ("切换聊天目标", "Change the chat target"),
    "instance": ("查看或切换 HASHI 实例", "List or switch HASHI instances"),
    "layout": ("调整日志与聊天布局", "Resize log and chat panes"),
    "log": ("显示、隐藏或暂停主机日志", "Show, hide, or pause the host log"),
    "agents": ("查看可用 Agent", "List available Agents"),
    "clear": ("清空当前 TUI 显示", "Clear the current TUI view"),
    "quit": ("退出 TUI", "Exit the TUI"),
    "tui": ("设置 TUI 语言及客户端选项", "Set TUI language and client options"),
}
TUI_DISCOVERY_COMMANDS = ("/help", "/to", "/mode", "/model", "/backend")


def markup(text: str) -> Text:
    """Render Rich markup explicitly before writing into RichLog."""
    return Text.from_markup(text)


def chat_message_renderable(role: str, prefix: str, body: str) -> Group:
    """Build one safe chat entry, rendering assistant content as Markdown."""

    header = Text()
    if str(role).strip().casefold() == "assistant":
        header.append(f"{prefix}:", style="bold #63ffd9")
        content = Markdown(
            str(body),
            code_theme="monokai",
            hyperlinks=True,
        )
    else:
        header.append(f"{prefix}:", style="bold #71b7ff")
        # User input is literal text.  In particular, square brackets must not
        # be interpreted as Rich markup tags.
        content = Text(str(body))
    return Group(header, content)


# ── Widgets ─────────────────────────────────────────────────────────────────

class LogPanel(RichLog):
    """Upper panel — streams stdout from the bridge subprocess."""
    DEFAULT_CSS = """
    LogPanel {
        height: 1fr;
        background: #08131d;
        color: #dff6ff;
        border: solid #2a5b82;
        border-title-align: left;
        scrollbar-background: #050b12;
        scrollbar-color: #2a5b82;
        scrollbar-color-hover: #71b7ff;
        scrollbar-color-active: #63ffd9;
    }
    """

    def on_mount(self):
        self.border_title = "HASHI Log"
        self.wrap = True


class ChatHistory(RichLog):
    """Chat display area showing agent replies."""
    DEFAULT_CSS = """
    ChatHistory {
        height: 1fr;
        background: #091722;
        color: #dff6ff;
        border: solid #2a5b82;
        border-title-align: left;
        min-height: 6;
        scrollbar-background: #050b12;
        scrollbar-color: #2a5b82;
        scrollbar-color-hover: #71b7ff;
        scrollbar-color-active: #63ffd9;
    }
    """

    def on_mount(self):
        self.border_title = "Chat"
        self.wrap = True


class ChatInput(Input):
    """Single-line input for sending messages."""
    DEFAULT_CSS = """
    ChatInput {
        height: 3;
        background: #0b1824;
        color: #dff6ff;
        border: solid #2a5b82;
    }
    ChatInput:focus {
        border: solid #63ffd9;
    }
    """


class CommandPreview(Static):
    """Compact slash-command palette shown below the input."""

    DEFAULT_CSS = """
    CommandPreview {
        display: none;
        height: auto;
        max-height: 5;
        padding: 0 1;
        background: #101d28;
        color: #9be7ff;
    }
    """

    def show_matches(
        self,
        matches: list[tuple[str, str, str]],
        selected_index: int = 0,
    ):
        rows = Text()
        for index, (command, description, scope) in enumerate(matches):
            selected = index == selected_index
            rows.append("› " if selected else "  ", style="bold #63ffd9" if selected else "#39566e")
            rows.append(f"{command:<18}", style="bold #71b7ff" if selected else "#7dc6ff")
            rows.append(description, style="#dff6ff" if selected else "#9fb3c8")
            rows.append(f"  {scope}", style="dim #7fb6c7")
            if index < len(matches) - 1:
                rows.append("\n")
        self.styles.height = len(matches)
        self.update(rows)
        self.display = True

    def hide_match(self):
        self.display = False


class FooterInfoBox(Static):
    """Compact footer showing only the active connection context."""

    DEFAULT_CSS = """
    FooterInfoBox {
        height: 3;
        background: #050b12;
        color: #dff6ff;
        border: solid #2a5b82;
        padding: 0 1;
        margin-top: 0;
    }
    """

    def __init__(self, *args, **kwargs):
        super().__init__("", *args, **kwargs)
        self._content = Text("")
        self._status_line = "❌ HASHI · No agent · API offline"
        self._language = "en"

    def on_mount(self):
        self._refresh_footer()

    def update_state(
        self,
        agent: str = "",
        backend: str = "",
        gateway_ok: bool = False,
        mode: str = "",
        agents: list[dict] | None = None,
        current_agent: str | None = None,
        instance_id: str = "",
        language: str = "en",
    ):
        self._language = language
        icon = "✅" if gateway_ok else "❌"
        no_agent = "未选择 Agent" if language == "zh" else "No agent"
        parts = [icon, instance_id or "HASHI", agent or no_agent]
        if backend:
            parts.append(backend)
        if mode:
            parts.append(mode.title())
        if language == "zh":
            parts.append("API 已连接" if gateway_ok else "API 离线")
        else:
            parts.append("API connected" if gateway_ok else "API offline")
        self._status_line = " · ".join(parts)
        self._refresh_footer()

    def _refresh_footer(self):
        self._content = markup(f"[bold #63ffd9]{self._status_line}[/]")
        self.refresh()

    def render(self) -> Text:
        return self._content


# ── Onboarding Screen (inline in main app) ──────────────────────────────────

class OnboardingPhase:
    """State machine for the onboarding flow running inside the TUI."""

    PHASE_LANG = "lang"
    PHASE_DISCLAIMER = "disclaimer"
    PHASE_AUDIT = "audit"
    PHASE_OPENROUTER = "openrouter"
    PHASE_DONE = "done"

    def __init__(self, bridge_home: Path):
        self.bridge_home = bridge_home
        self.phase = self.PHASE_LANG
        self.langs = load_languages(bridge_home)
        self.selected_lang: dict | None = None
        self.l_code = "en"
        self.engine: str | None = None
        self.or_key: str | None = None

    def get_prompt_text(self) -> str:
        if self.phase == self.PHASE_LANG:
            lines = [
                "\U0001f30e HASHI \u2014 Welcome / \u3088\u3046\u3053\u305d / \u6b22\u8fce",
                "",
                "Select your language:",
            ]
            for i, lang in enumerate(self.langs, 1):
                lines.append(f"  [{i}] {lang.get('displayName', f'Language {i}')}")
            lines.append("")
            lines.append("Enter number:")
            return "\n".join(lines)

        if self.phase == self.PHASE_DISCLAIMER:
            disc_path = self.bridge_home / "onboarding" / "languages" / f"disclaimer_{self.l_code}.md"
            if not disc_path.exists():
                disc_path = self.bridge_home / "onboarding" / "languages" / "disclaimer_en.md"
            text = disc_path.read_text(encoding="utf-8") if disc_path.exists() else "(Disclaimer not found)"
            return text + '\n\nType "I AGREE" to continue:'

        if self.phase == self.PHASE_AUDIT:
            return "\U0001f50d Detecting available backends..."

        if self.phase == self.PHASE_OPENROUTER:
            return "No local CLI found. Please enter your OpenRouter API key:"

        return ""

    def handle_input(self, text: str) -> tuple[str, bool]:
        """Process user input. Returns (message_to_display, needs_more_input)."""
        if self.phase == self.PHASE_LANG:
            try:
                idx = int(text.strip()) - 1
                if 0 <= idx < len(self.langs):
                    self.selected_lang = self.langs[idx]
                    self.l_code = lang_code_from_file(self.selected_lang.get("_file", ""))
                    self.phase = self.PHASE_DISCLAIMER
                    return (f"\u2705 Language: {self.selected_lang.get('displayName', '?')}\n\n"
                            + self.get_prompt_text()), True
                return "Invalid number. Try again.", True
            except ValueError:
                return "Please enter a number.", True

        if self.phase == self.PHASE_DISCLAIMER:
            if text.strip() == "I AGREE":
                self.phase = self.PHASE_AUDIT
                return "\u2705 Accepted.\n\n" + self.get_prompt_text(), False  # auto-continue to audit
            return 'You must type "I AGREE" exactly.', True

        if self.phase == self.PHASE_OPENROUTER:
            key = text.strip()
            if not key:
                return "Key cannot be empty.", True
            if verify_openrouter(key):
                self.or_key = key
                self.engine = "openrouter-api"
                self.phase = self.PHASE_DONE
                return "\u2705 OpenRouter key verified!", False
            return "\u274c Invalid key. Try again:", True

        return "", False

    def run_audit(self) -> str:
        """Blocking call — detect CLIs. Call from a worker thread."""
        cli_name, engine = audit_environment()
        if engine:
            self.engine = engine
            self.phase = self.PHASE_DONE
            return f"\u2705 Detected: {cli_name} ({engine})"
        self.phase = self.PHASE_OPENROUTER
        return "\u26a0\ufe0f No local CLI backend found."

    def finalize(self):
        """Write agents.json + secrets.json."""
        if self.selected_lang and self.engine:
            write_config(self.bridge_home, self.engine, self.selected_lang, self.l_code, self.or_key)


# ── Main App ────────────────────────────────────────────────────────────────

class HASHITuiApp(App):
    TITLE = "HASHI \u30cf\u30b7 \u6a4b"
    CSS = """
    Screen {
        background: #050b12;
        color: #dff6ff;
    }
    #main-container {
        height: 1fr;
        padding: 0 1 1 1;
    }
    #log-panel {
        height: 1fr;
        min-height: 4;
    }
    #chat-container {
        height: 3fr;
        min-height: 10;
    }
    #footer-info-box {
        height: 3;
    }
    """

    BINDINGS = [
        Binding("ctrl+l", "toggle_log_pause", "Pause Log", show=True),
        Binding("tab", "complete_command", "Complete command", show=False, priority=True),
        Binding("ctrl+q", "quit_app", "Quit", show=True),
    ]

    def __init__(
        self,
        workbench_url: str = DEFAULT_WORKBENCH_LOCALHOST_URL,
        onboarding_mode: bool = False,
        *,
        workbench_urls: list[str] | tuple[str, ...] | None = None,
        bridge_home: Path | None = None,
        code_root: Path | None = None,
        launch_instance_id: str | None = None,
    ):
        super().__init__()
        self.bridge_home = Path(bridge_home).resolve() if bridge_home else self._find_bridge_home()
        self.code_root = Path(code_root).resolve() if code_root else Path(__file__).resolve().parent.parent
        configured_instance_id, _workbench_port = load_launch_instance(self.bridge_home)
        self.launch_instance_id = str(launch_instance_id or configured_instance_id).strip().upper()
        local_urls = list(workbench_urls or [workbench_url])
        if workbench_url not in local_urls:
            local_urls.insert(0, workbench_url)
        self.current_instance_id = self.launch_instance_id
        self.bridge_proc: asyncio.subprocess.Process | None = None
        self.current_agent: str | None = None
        self._chat_targets: list[str] = []
        self.current_agent_display: str = ""
        self.current_backend: str = ""
        self._agent_mode: str = ""
        preferences = self._load_tui_preferences()
        requested_layout = str(
            os.environ.get("HASHI_TUI_LAYOUT") or preferences.get("layout") or "chat"
        ).casefold()
        self._layout_mode = requested_layout if requested_layout in {"chat", "balanced", "compact"} else "chat"
        requested_language = str(
            os.environ.get("HASHI_TUI_LANGUAGE") or preferences.get("language") or "en"
        ).casefold()
        self._ui_language = "zh" if requested_language.startswith("zh") else "en"
        command_names = [f"/{spec.name}" for spec in COMMAND_SPECS if spec.menu_visible]
        command_names.extend(f"/{name}" for name in TUI_COMMAND_HELP)
        self._command_names = list(dict.fromkeys(command_names))
        self._current_command_match: str | None = None
        self._command_matches: list[str] = []
        self._command_match_index = 0
        self.api = TuiApiClient(
            base_url=local_urls[0],
            fallback_base_urls=local_urls[1:],
            expected_instance_id=self.launch_instance_id,
        )
        self._instance_resolver = InstanceResolver(self.bridge_home, local_urls)
        self._instance_switch_lock = asyncio.Lock()
        self._connection_generation = 0
        self.gateway_ok = False
        self._log_paused = False
        self._onboarding: OnboardingPhase | None = None
        self._light_onboarding: LightOnboardingPhase | None = None
        self._inject_wakeup: str | None = None
        self._onboarding_mode = onboarding_mode
        self._poll_task: asyncio.Task | None = None
        self._log_follow_task: asyncio.Task | None = None
        self._agents_cache: list[dict] = []
        self._attached_log_path: Path | None = None
        self._agent_refresh_tick = 0
        self._startup_task: asyncio.Task | None = None

    def _find_bridge_home(self) -> Path:
        env = os.environ.get("BRIDGE_HOME")
        if env:
            return Path(env).resolve()
        # Walk up from this file to find main.py
        candidate = Path(__file__).resolve().parent.parent
        if (candidate / "main.py").exists():
            return candidate
        return Path.cwd()

    @property
    def _preferences_path(self) -> Path:
        return self.bridge_home / "state" / "tui_preferences.json"

    def _load_tui_preferences(self) -> dict:
        try:
            data = json.loads(self._preferences_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, OSError, ValueError):
            return {}

    def _save_tui_preferences(self):
        try:
            self._preferences_path.parent.mkdir(parents=True, exist_ok=True)
            self._preferences_path.write_text(
                json.dumps(
                    {"language": self._ui_language, "layout": self._layout_mode},
                    ensure_ascii=False,
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("TUI preferences could not be saved: error=%s", exc)

    def compose(self) -> ComposeResult:
        with Vertical(id="main-container"):
            yield LogPanel(id="log-panel")
            with Vertical(id="chat-container"):
                yield ChatHistory(id="chat-history")
                yield ChatInput(
                    placeholder="Message · /help · /to <agent> · /instance",
                    suggester=SuggestFromList(self._command_names, case_sensitive=False),
                    id="chat-input",
                )
                yield CommandPreview(id="command-preview")
            yield FooterInfoBox(id="footer-info-box")

    def on_mount(self):
        self._apply_layout(self._layout_mode, persist=False)
        self._refresh_chrome()
        # Start the intro only after the first screen refresh so frames are visible.
        self.call_after_refresh(self._schedule_startup_sequence)

    def _schedule_startup_sequence(self):
        if self._startup_task and not self._startup_task.done():
            return
        self._startup_task = asyncio.create_task(self._run_startup_sequence())

    async def _run_startup_sequence(self):
        await asyncio.sleep(0.05)
        await self._play_log_startup_animation()
        await asyncio.sleep(0.1)
        agents_path = self.bridge_home / "agents.json"
        needs_onboarding = True
        if agents_path.exists():
            try:
                cfg = json.loads(agents_path.read_text(encoding="utf-8-sig"))
                if cfg.get("agents"):
                    needs_onboarding = False
            except Exception as exc:
                logger.warning("TUI startup config unreadable: path=%s error=%s", agents_path, exc)

        if self._onboarding_mode and not is_onboarding_complete(self.bridge_home):
            self._start_light_onboarding()
        elif needs_onboarding:
            self._start_onboarding()
        else:
            self._start_bridge()

    async def _play_log_startup_animation(self):
        self._render_host_header()
        await asyncio.sleep(0.2)

    def _render_host_header(self):
        log = self.query_one("#log-panel", LogPanel)
        log.clear()
        if self._layout_mode == "balanced":
            colors = ("#71b7ff", "#7dc6ff", "#87d4ff", "#92e1ff", "#9eeed8", "#c7ff8a")
            logo = "\n".join(
                f"[bold {color}]{line}[/]" for color, line in zip(colors, STARTUP_LOGO)
            )
            status = "终端已连接" if self._ui_language == "zh" else "Terminal connected"
            log.write(markup(f"{logo}\n[#9be7ff]{self.launch_instance_id} · {status}[/]"))
        else:
            status = (
                "终端已连接 · 正在准备本地服务…"
                if self._ui_language == "zh"
                else "Terminal connected · preparing local services…"
            )
            log.write(markup(
                f"[bold #63ffd9]HASHI · {self.launch_instance_id}[/]\n[#9be7ff]{status}[/]"
            ))

    # ── Onboarding ──────────────────────────────────────────────────────

    def _start_onboarding(self):
        self._onboarding = OnboardingPhase(self.bridge_home)
        chat = self.query_one("#chat-history", ChatHistory)
        chat.border_title = "Onboarding"
        chat.write(self._onboarding.get_prompt_text())
        self.query_one("#chat-input", ChatInput).placeholder = "Enter your choice..."
        self._update_status_bar()

    async def _handle_onboarding_input(self, text: str):
        chat = self.query_one("#chat-history", ChatHistory)
        ob = self._onboarding

        msg, needs_input = ob.handle_input(text)
        if msg:
            chat.write(msg)

        if ob.phase == OnboardingPhase.PHASE_AUDIT:
            # Run blocking audit in thread
            self._run_audit_in_thread()
            return

        if ob.phase == OnboardingPhase.PHASE_DONE:
            chat.write("\n\U0001f680 Creating configuration...")
            ob.finalize()
            chat.write("\u2705 Configuration created! Starting HASHI...\n")
            self._onboarding = None
            self.query_one("#chat-input", ChatInput).placeholder = "Type message, /to <agent>, or /instance ..."
            self.query_one("#chat-history", ChatHistory).border_title = "Chat"
            self._start_bridge()

    @work(thread=True)
    def _run_audit_in_thread(self):
        result = self._onboarding.run_audit()
        self.call_from_thread(self._audit_complete, result)

    def _audit_complete(self, result: str):
        chat = self.query_one("#chat-history", ChatHistory)
        chat.write(result)

        if self._onboarding.phase == OnboardingPhase.PHASE_OPENROUTER:
            chat.write("\n" + self._onboarding.get_prompt_text())
        elif self._onboarding.phase == OnboardingPhase.PHASE_DONE:
            chat.write("\n\U0001f680 Creating configuration...")
            self._onboarding.finalize()
            chat.write("\u2705 Configuration created! Starting HASHI...\n")
            self._onboarding = None
            self.query_one("#chat-input", ChatInput).placeholder = "Type message, /to <agent>, or /instance ..."
            self.query_one("#chat-history", ChatHistory).border_title = "Chat"
            self._start_bridge()

    # ── Light Onboarding (TUI_onboarding first-run flow) ─────────────────

    def _start_light_onboarding(self):
        self._light_onboarding = LightOnboardingPhase(self.bridge_home)
        chat = self.query_one("#chat-history", ChatHistory)
        chat.border_title = "Setup"
        chat.write(self._light_onboarding.get_initial_prompt())
        self.query_one("#chat-input", ChatInput).placeholder = "Enter your choice..."

    async def _handle_light_onboarding_input(self, text: str):
        chat = self.query_one("#chat-history", ChatHistory)
        lo = self._light_onboarding

        msg, needs_input = lo.handle_input(text)
        if msg:
            chat.write(msg)

        if lo.phase == LightOnboardingPhase.PHASE_APICHECK:
            self._run_light_api_check()
            return

        if lo.phase == LightOnboardingPhase.PHASE_DONE:
            self._finalize_light_onboarding()

    @work(thread=True)
    def _run_light_api_check(self):
        result, ok = self._light_onboarding.run_api_check()
        self.call_from_thread(self._light_api_check_complete, result, ok)

    def _light_api_check_complete(self, result: str, ok: bool):
        chat = self.query_one("#chat-history", ChatHistory)
        chat.write(result)
        if self._light_onboarding.phase == LightOnboardingPhase.PHASE_APIKEY_INPUT:
            pass  # wait for user input
        elif self._light_onboarding.phase == LightOnboardingPhase.PHASE_DONE:
            self._finalize_light_onboarding()

    def _finalize_light_onboarding(self):
        chat = self.query_one("#chat-history", ChatHistory)
        lo = self._light_onboarding
        lo.finalize()
        self._inject_wakeup = lo.get_wakeup_prompt()
        self._light_onboarding = None
        chat.write("✅ Setup complete! Starting HASHI...\n")
        self.query_one("#chat-input", ChatInput).placeholder = "Type message, /to <agent>, or /instance ..."
        self.query_one("#chat-history", ChatHistory).border_title = "Chat"
        self._start_bridge()

    # ── Bridge subprocess ───────────────────────────────────────────────

    def _start_bridge(self):
        self._launch_bridge_task()

    @work()
    async def _launch_bridge_task(self):
        self._attached_log_path = self._resolve_attach_log_path()

        # If API is already up (HASHI already running), just attach — don't start a new process
        if await self.api.health():
            self._write_log_line(
                f"[TUI] {self.launch_instance_id} is already running — attaching to launch instance."
            )
            self._start_attached_log_follow()
            self.gateway_ok = True
            await self._load_agents(client=self.api, generation=self._connection_generation)
            self._start_polling()
            self._update_status_bar()
            return

        attach_only = str(os.environ.get("HASHI_TUI_ATTACH_ONLY") or "").strip().casefold()
        if attach_only in {"1", "true", "yes", "on"}:
            self._write_log_line(
                f"[TUI] Waiting for the launched {self.launch_instance_id} local API."
            )
            self._start_attached_log_follow()
            await self._wait_for_api()
            return

        self._write_log_line("[TUI] Starting HASHI main process...")

        command = [
            sys.executable,
            str(self.code_root / "main.py"),
            "--bridge-home",
            str(self.bridge_home),
        ]
        enable_gateway = str(
            os.environ.get("HASHI_TUI_ENABLE_API_GATEWAY", "1")
        ).strip().casefold() not in {"0", "false", "no", "off"}
        if enable_gateway:
            command.append("--api-gateway")
        self.bridge_proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(self.code_root),
        )

        # Stream stdout to log panel
        asyncio.create_task(self._stream_logs())

        # Wait for API to become available then start polling
        await self._wait_for_api()

    async def _stream_logs(self):
        while self.bridge_proc and self.bridge_proc.returncode is None:
            try:
                line = await self.bridge_proc.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                if decoded and not self._log_paused:
                    self._write_log_line(decoded)
            except Exception as exc:
                logger.warning("TUI bridge log stream failed: error=%s", exc)
                break
        self._write_log_line("[TUI] Bridge process exited.")

    def _resolve_attach_log_path(self) -> Path | None:
        for candidate in (
            self.bridge_home / "logs" / "bridge.log",
            self.bridge_home / "bridge_launch.log",
            self.bridge_home / "logs" / "bridge_launch.log",
            self.bridge_home / "bin" / "bridge_launch.log",
        ):
            if candidate.exists():
                return candidate
        return None

    def _start_attached_log_follow(self):
        if self._log_follow_task and not self._log_follow_task.done():
            return
        self._log_follow_task = asyncio.create_task(self._follow_attached_log())

    async def _follow_attached_log(self):
        path = self._attached_log_path
        if not path:
            self._write_log_line("[TUI] No attachable log file found for the running HASHI instance.")
            return

        try:
            position = path.stat().st_size
            self._write_log_line(f"[TUI] Following launcher log from now: {path}")
        except Exception as exc:
            self._write_log_line(f"[TUI] Failed to read attach log {path}: {exc}")
            return

        while self.bridge_proc is None:
            try:
                if not path.exists():
                    await asyncio.sleep(1.0)
                    continue
                current_size = path.stat().st_size
                if current_size < position:
                    position = 0
                if current_size > position:
                    chunk, position = await asyncio.to_thread(self._read_log_chunk, path, position)
                    for line in chunk:
                        if line and not self._log_paused:
                            self._write_log_line(line)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("TUI attached log follow failed: path=%s error=%s", path, exc)
            await asyncio.sleep(1.0)

    def _read_log_chunk(self, path: Path, position: int) -> tuple[list[str], int]:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(position)
            lines = [line.rstrip() for line in fh.readlines()]
            return lines, fh.tell()

    def _write_log_line(self, text: str):
        log = self.query_one("#log-panel", LogPanel)
        log.write(Text.from_ansi(text))

    async def _wait_for_api(self):
        for attempt in range(60):
            if await self.api.health():
                self.gateway_ok = True
                self._write_log_line("[TUI] Local HASHI API connected.")
                await self._load_agents(client=self.api, generation=self._connection_generation)
                self._start_polling()
                self._update_status_bar()
                return
            await asyncio.sleep(1)
        self._write_log_line("[TUI] Warning: local HASHI API not reachable after 60s. Chat disabled.")
        self._update_status_bar()

    async def _load_agents(
        self,
        *,
        client: TuiApiClient | None = None,
        generation: int | None = None,
        agents: list[dict] | None = None,
    ):
        client = client or self.api
        generation = self._connection_generation if generation is None else generation
        agents = await client.list_agents() if agents is None else agents
        if generation != self._connection_generation or client is not self.api:
            logger.debug("Discarded stale TUI agent load: generation=%s", generation)
            return
        self._agents_cache = agents
        self._update_status_bar()
        if agents and not self.current_agent:
            # Auto-select first active agent
            for a in agents:
                if a.get("is_active") or a.get("online"):
                    self._select_agent(a, client=client, generation=generation)
                    break
            if not self.current_agent and agents:
                self._select_agent(agents[0], client=client, generation=generation)

    def _select_agent(
        self,
        agent_data: dict,
        *,
        client: TuiApiClient | None = None,
        generation: int | None = None,
    ):
        client = client or self.api
        generation = self._connection_generation if generation is None else generation
        self.current_agent = agent_data.get("name", "")
        self._chat_targets = [self.current_agent] if self.current_agent else []
        self.current_agent_display = agent_data.get("display_name", self.current_agent)
        self.current_backend = agent_data.get("active_backend", agent_data.get("engine", ""))
        self._agent_mode = agent_data.get("mode", "flex")
        client.reset_offset(self.current_agent)
        chat = self.query_one("#chat-history", ChatHistory)
        emoji = agent_data.get("emoji", "")
        location = self._location_label()
        chat_label = "聊天" if self._ui_language == "zh" else "Chat"
        chat.border_title = (
            f"{chat_label} · {emoji} {self.current_agent_display} ({self.current_agent})"
            f"@{self.current_instance_id} ({location})"
        )
        self._update_status_bar()
        # Load recent transcript
        self._load_initial_transcript(client, self.current_agent, generation)

        # First-run wakeup: send once after initial agent selection
        if self._inject_wakeup and self.current_agent:
            wakeup = self._inject_wakeup
            self._inject_wakeup = None
            self._send_wakeup(wakeup, self.current_agent, client, generation)

    @work()
    async def _send_wakeup(
        self,
        prompt: str,
        agent: str,
        client: TuiApiClient,
        generation: int,
    ):
        await asyncio.sleep(2.0)  # let bridge settle
        if generation != self._connection_generation or client is not self.api:
            logger.info("Skipped stale onboarding wakeup: agent=%s generation=%s", agent, generation)
            return
        await client.send_chat(agent, prompt)

    def _render_transcript_message(self, msg: dict):
        role = msg.get("role", "?")
        text = msg.get("text", "")
        if not text:
            return

        chat = self.query_one("#chat-history", ChatHistory)
        source = msg.get("agent") or msg.get("agent_id") or msg.get("source", "")
        prefix = self.current_agent_display

        if self.current_agent_display == "ALL" and source and source not in {"text", "api", "photo", "system"}:
            prefix = source

        if role == "user":
            chat.write(chat_message_renderable("user", "You", text))
        elif role == "assistant":
            chat.write(chat_message_renderable("assistant", prefix, text))

    @work()
    async def _load_initial_transcript(
        self,
        client: TuiApiClient,
        agent: str,
        generation: int,
    ):
        if not agent:
            return
        messages = await client.get_recent_transcript(agent, limit=20)
        if (
            generation != self._connection_generation
            or client is not self.api
            or agent != self.current_agent
        ):
            logger.debug("Discarded stale TUI transcript load: agent=%s generation=%s", agent, generation)
            return
        chat = self.query_one("#chat-history", ChatHistory)
        chat.clear()
        for msg in messages:
            self._render_transcript_message(msg)

    # ── Transcript polling ──────────────────────────────────────────────

    def _start_polling(self):
        if self._poll_task and not self._poll_task.done():
            return
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self):
        while True:
            if self.gateway_ok:
                generation = self._connection_generation
                client = self.api
                try:
                    self._agent_refresh_tick = (self._agent_refresh_tick + 1) % 5
                    if self._agent_refresh_tick == 0:
                        agents = await client.list_agents()
                        if generation == self._connection_generation and client is self.api:
                            self._agents_cache = agents
                            self._update_status_bar()
                    poll_targets = self._chat_targets[:]
                    if self.current_agent and self.current_agent not in poll_targets:
                        poll_targets.append(self.current_agent)
                    for agent in poll_targets:
                        messages = await client.poll_transcript(agent)
                        if generation != self._connection_generation or client is not self.api:
                            logger.debug("Discarded stale TUI poll result: agent=%s generation=%s", agent, generation)
                            break
                        received = False
                        for msg in messages:
                            if msg.get("role") == "assistant":
                                self._render_transcript_message(msg)
                                received = True
                        if received:
                            play_message_sound("received")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "TUI transcript polling failed: instance=%s error=%s",
                        self.current_instance_id,
                        exc,
                    )
            await asyncio.sleep(1.0)

    # ── Input handling ──────────────────────────────────────────────────

    async def on_input_submitted(self, event: Input.Submitted):
        text = event.value.strip()
        event.input.value = ""

        # Light onboarding accepts empty Enter to advance
        if self._light_onboarding:
            await self._handle_light_onboarding_input(text)
            return

        if not text:
            return
        normalized = text
        if normalized.startswith("/ "):
            normalized = "/" + normalized[2:].lstrip()

        # Onboarding mode
        if self._onboarding:
            await self._handle_onboarding_input(normalized)
            return

        # TUI local commands
        if normalized == "/to" or normalized.startswith("/to "):
            await self._handle_to(normalized)
            return
        if normalized == "/agents":
            await self._handle_agents_cmd()
            return
        if normalized == "/tui" or normalized.startswith("/tui "):
            self._handle_tui_cmd(normalized)
            return
        if normalized == "/help" or normalized.startswith("/help "):
            self._handle_help_cmd(normalized)
            return
        if normalized == "/layout" or normalized.startswith("/layout "):
            self._handle_layout_cmd(normalized)
            return
        if normalized == "/instance" or normalized.startswith("/instance "):
            await self._handle_instance_cmd(normalized)
            return
        if normalized == "/clear":
            self.query_one("#chat-history", ChatHistory).clear()
            return
        if normalized == "/quit":
            await self._shutdown()
            return
        if normalized == "/log" or normalized.startswith("/log "):
            self._handle_log_cmd(normalized)
            return

        # Everything else → send to agent
        if not self.gateway_ok:
            chat = self.query_one("#chat-history", ChatHistory)
            chat.write(markup("[#ff7a7a]Local HASHI API not connected. Chat unavailable.[/]"))
            return

        chat = self.query_one("#chat-history", ChatHistory)
        chat.write(chat_message_renderable("user", "You", normalized))

        if self.current_agent_display == "ALL":
            # Broadcast to all active agents
            play_message_sound("sent")
            self._send_broadcast(normalized, self.api, self._connection_generation)
            return

        if not self.current_agent:
            chat.write(markup("[yellow]No agent selected. Use /to <name> first.[/]"))
            return
        else:
            play_message_sound("sent")
            self._send_message(normalized, self.current_agent, self.api, self._connection_generation)

    def on_input_changed(self, event: Input.Changed):
        """Show a Codex-style palette for an incomplete slash command."""

        value = event.value.strip().casefold()
        preview = self.query_one("#command-preview", CommandPreview)
        self._current_command_match = None
        self._command_matches = []
        self._command_match_index = 0
        if not value.startswith("/") or " " in value or len(value) < 2:
            if value == "/":
                self._command_matches = [
                    command for command in TUI_DISCOVERY_COMMANDS if command in self._command_names
                ]
                self._show_command_matches()
            else:
                preview.hide_match()
            return
        self._command_matches = [
            command for command in self._command_names if command.startswith(value)
        ][:5]
        if not self._command_matches:
            preview.hide_match()
            return
        self._show_command_matches()

    def _show_command_matches(self):
        if not self._command_matches:
            self.query_one("#command-preview", CommandPreview).hide_match()
            self._current_command_match = None
            return
        self._command_match_index %= len(self._command_matches)
        self._current_command_match = self._command_matches[self._command_match_index]
        local_names = set(TUI_COMMAND_HELP)
        tui_scope = "TUI"
        agent_scope = "Agent"
        matches = [
            (
                command,
                self._command_description(command[1:]),
                tui_scope if command[1:] in local_names else agent_scope,
            )
            for command in self._command_matches
        ]
        self.query_one("#command-preview", CommandPreview).show_matches(
            matches,
            self._command_match_index,
        )

    def _command_description(self, name: str) -> str:
        local = TUI_COMMAND_HELP.get(name)
        if local:
            return local[0 if self._ui_language == "zh" else 1]
        spec = next((item for item in COMMAND_SPECS if item.name == name), None)
        if spec is None:
            return ""
        chinese = {
            "backend": "选择或查看后端",
            "language": "选择界面语言",
            "mode": "选择或查看工作模式",
            "model": "选择模型与推理强度",
            "status": "查看 Agent 状态",
            "version": "查看实际运行版本",
        }
        if self._ui_language == "zh" and name in chinese:
            return chinese[name]
        return spec.description

    def action_complete_command(self):
        input_box = self.query_one("#chat-input", ChatInput)
        if self.focused is not input_box or not self._current_command_match:
            return
        input_box.value = self._current_command_match
        input_box.cursor_position = len(input_box.value)
        self.query_one("#command-preview", CommandPreview).hide_match()

    def on_key(self, event):
        """Navigate the visible command palette without affecting ordinary input."""

        input_box = self.query_one("#chat-input", ChatInput)
        if self.focused is not input_box or not self._command_matches:
            return
        if event.key == "down":
            self._command_match_index = (self._command_match_index + 1) % len(self._command_matches)
        elif event.key == "up":
            self._command_match_index = (self._command_match_index - 1) % len(self._command_matches)
        elif event.key == "escape":
            self._command_matches = []
            self._current_command_match = None
            self.query_one("#command-preview", CommandPreview).hide_match()
            event.stop()
            event.prevent_default()
            return
        else:
            return
        self._show_command_matches()
        event.stop()
        event.prevent_default()

    def _handle_tui_cmd(self, text: str):
        chat = self.query_one("#chat-history", ChatHistory)
        parts = text.split()
        if len(parts) == 1 or (len(parts) == 2 and parts[1].casefold() in {"language", "lang"}):
            current = "中文" if self._ui_language == "zh" else "English"
            chat.write(markup(
                f"[#c7ff8a]TUI language · {current} · /tui language zh|en[/]"
            ))
            return
        if len(parts) == 3 and parts[1].casefold() in {"language", "lang"}:
            requested = parts[2].casefold()
            if requested in {"zh", "cn", "中文", "chinese"}:
                self._ui_language = "zh"
            elif requested in {"en", "english"}:
                self._ui_language = "en"
            else:
                chat.write(markup("[#ff7a7a]Use /tui language zh|en.[/]"))
                return
            self._save_tui_preferences()
            self._refresh_chrome()
            self._render_host_header()
            message = "✓ TUI 已切换为中文。" if self._ui_language == "zh" else "✓ TUI switched to English."
            chat.write(markup(f"[#63ffd9]{message}[/]"))
            return
        chat.write(markup("[#ff7a7a]Use /tui language zh|en.[/]"))

    def _handle_help_cmd(self, text: str):
        """Render TUI and common Agent commands without sending them to a model."""

        parts = text.split(maxsplit=1)
        requested = parts[1].strip().casefold() if len(parts) > 1 else ""
        language = "zh" if requested in {"zh", "中文", "chinese"} else self._ui_language
        if requested in {"en", "english"}:
            language = "en"
        chat = self.query_one("#chat-history", ChatHistory)
        if language == "zh":
            help_text = """## ❔ HASHI TUI 帮助

**常用 Agent 命令**

`/backend`　选择或查看后端
`/mode`　选择或查看工作模式
`/status`　查看当前 Agent 状态
`/version [full|all]`　查看实际运行版本

**TUI 导航与布局**

`/to <agent|all>`　切换聊天目标
`/instance [名称]`　查看或切换实例
`/layout [chat|balanced|compact|reset]`　调整窗口比例
`/log [show|hide|pause]`　控制本地日志
`/clear`　清空当前显示　　`/quit`　退出

输入 `/help en` 查看英文版。这里只显示常用命令；Agent 命令仍由当前实例执行。"""
        else:
            help_text = """## ❔ HASHI TUI HELP

**Common Agent commands**

`/backend`　Select or inspect the backend
`/mode`　Select or inspect the work mode
`/status`　Show current Agent status
`/version [full|all]`　Show the running version

**TUI navigation and layout**

`/to <agent|all>`　Change the chat target
`/instance [name]`　List or switch instances
`/layout [chat|balanced|compact|reset]`　Resize the panes
`/log [show|hide|pause]`　Control the host log
`/clear`　Clear this view　　`/quit`　Exit

Use `/help zh` for Chinese. Agent commands are executed by the selected instance."""
        chat.write(chat_message_renderable("assistant", "HASHI", help_text))

    def _refresh_chrome(self):
        log = self.query_one("#log-panel", LogPanel)
        chat = self.query_one("#chat-history", ChatHistory)
        input_box = self.query_one("#chat-input", ChatInput)
        if self._ui_language == "zh":
            log.border_title = f"主机日志 · {self.launch_instance_id}（本机）"
            input_box.placeholder = "输入消息 · /help · /to <Agent> · /instance"
        else:
            log.border_title = f"Host log · {self.launch_instance_id} (local)"
            input_box.placeholder = "Message · /help · /to <agent> · /instance"
        if self.current_agent_display:
            location = self._location_label()
            chat_label = "聊天" if self._ui_language == "zh" else "Chat"
            emoji = next(
                (str(item.get("emoji") or "") for item in self._agents_cache if item.get("name") == self.current_agent),
                "",
            )
            chat.border_title = (
                f"{chat_label} · {emoji} {self.current_agent_display} ({self.current_agent})"
                f"@{self.current_instance_id} ({location})"
            )
        self._update_status_bar()

    def _location_label(self) -> str:
        local = self.current_instance_id == self.launch_instance_id
        if self._ui_language == "zh":
            return "本机" if local else "远程"
        return "local" if local else "remote"

    def _apply_layout(self, mode: str, *, persist: bool = True):
        log = self.query_one("#log-panel", LogPanel)
        chat_container = self.query_one("#chat-container", Vertical)
        if mode == "compact":
            log.display = False
            chat_container.styles.height = "1fr"
        else:
            log.display = True
            log.styles.height = "1fr"
            chat_container.styles.height = "1fr" if mode == "balanced" else "3fr"
        self._layout_mode = mode
        if mode == "balanced" and self.is_mounted:
            self._render_host_header()
        if persist:
            self._save_tui_preferences()

    def _handle_layout_cmd(self, text: str):
        chat = self.query_one("#chat-history", ChatHistory)
        parts = text.split(maxsplit=1)
        requested = parts[1].strip().casefold() if len(parts) > 1 else ""
        aliases = {"reset": "chat", "default": "chat"}
        mode = aliases.get(requested, requested)
        if not mode:
            chat.write(markup(f"[#c7ff8a]Layout · {self._layout_mode} · Use /layout chat|balanced|compact[/]"))
            return
        if mode not in {"chat", "balanced", "compact"}:
            chat.write(markup("[#ff7a7a]Unknown layout. Use /layout chat|balanced|compact|reset.[/]"))
            return
        self._apply_layout(mode)
        chat.write(markup(f"[#63ffd9]✓ Layout · {mode}[/]"))

    def _handle_log_cmd(self, text: str):
        chat = self.query_one("#chat-history", ChatHistory)
        log = self.query_one("#log-panel", LogPanel)
        parts = text.split(maxsplit=1)
        action = parts[1].strip().casefold() if len(parts) > 1 else "pause"
        if action == "show":
            self._apply_layout("chat")
            chat.write(markup("[#63ffd9]✓ Host log shown.[/]"))
        elif action == "hide":
            self._apply_layout("compact")
            chat.write(markup("[#63ffd9]✓ Host log hidden.[/]"))
        elif action == "pause":
            self.action_toggle_log_pause()
            state = "paused" if self._log_paused else "following"
            chat.write(markup(f"[#63ffd9]✓ Host log · {state}.[/]"))
        else:
            chat.write(markup("[#ff7a7a]Use /log show|hide|pause.[/]"))

    @work()
    async def _send_message(
        self,
        text: str,
        agent: str,
        client: TuiApiClient,
        generation: int,
    ):
        result = await client.send_chat(agent, text)
        if generation != self._connection_generation or client is not self.api:
            logger.info("TUI message completed on previous instance generation=%s agent=%s", generation, agent)
            return
        if not result.get("ok", True) and "error" in result:
            chat = self.query_one("#chat-history", ChatHistory)
            chat.write(Text(f"Error ({agent}): {result['error']}", style="red"))
            return

        session_id = str(result.get("session_id") or "").strip()
        run_id = str(result.get("run_id") or "").strip()
        if not session_id or not run_id or client.proxied:
            return

        while generation == self._connection_generation and client is self.api:
            status = await client.run_info(session_id, run_id)
            if generation != self._connection_generation or client is not self.api:
                return
            if not status.get("ok"):
                logger.warning(
                    "TUI could not track submitted Run: agent=%s session=%s run=%s error=%s",
                    agent,
                    session_id,
                    run_id,
                    status.get("error"),
                )
                return
            run = status.get("run")
            state = (
                str(run.get("state") or "").strip().casefold()
                if isinstance(run, dict)
                else ""
            )
            if state in TUI_TERMINAL_RUN_STATES:
                failure = run_failure_text(status)
                if failure:
                    chat = self.query_one("#chat-history", ChatHistory)
                    chat.write(Text(f"Request failed ({agent}): {failure}", style="red"))
                return
            await asyncio.sleep(0.5)

    @work()
    async def _send_broadcast(
        self,
        text: str,
        client: TuiApiClient,
        generation: int,
    ):
        agents = await client.list_agents()
        for a in agents:
            if a.get("is_active") or a.get("online"):
                await client.send_chat(a["name"], text)
        if generation != self._connection_generation:
            logger.info("TUI broadcast completed on previous instance generation=%s", generation)

    # ── /to command ─────────────────────────────────────────────────────

    async def _handle_to(self, text: str):
        chat = self.query_one("#chat-history", ChatHistory)
        parts = text.split()[1:]  # strip "/to"
        if not parts:
            chat.write(markup("[#c7ff8a]Usage: /to <agent> or /to all[/]"))
            return

        # Refresh agent list
        agents = await self.api.list_agents()
        self._agents_cache = agents
        agent_map = {a["name"]: a for a in agents}

        target = parts[0].lower()
        if target == "all":
            # Multi-cast mode — just switch display; actual sending done in send
            active_targets = [
                a["name"]
                for a in agents
                if a.get("is_active") or a.get("online")
            ]
            self.current_agent = None
            self._chat_targets = active_targets
            self.current_agent_display = "ALL"
            self.current_backend = ""
            self._agent_mode = "broadcast"
            chat.border_title = "Chat \u2014 \U0001f4e2 Broadcasting to ALL agents"
            chat.write(markup("[#63ffd9]\u2705 Broadcasting mode: messages will be sent to all active agents.[/]"))
            self._update_status_bar()
            return

        # Single or multi agent
        if target in agent_map:
            self._select_agent(agent_map[target])
            chat.write(markup(f"[#63ffd9]\u2705 Switched to {self.current_agent_display}[/]"))
        else:
            chat.write(markup(f"[#ff7a7a]Agent '{target}' not found. Use /agents to list.[/]"))

    async def _handle_agents_cmd(self):
        chat = self.query_one("#chat-history", ChatHistory)
        agents = await self.api.list_agents()
        self._agents_cache = agents
        if not agents:
            chat.write(markup("[#c7ff8a]No agents found.[/]"))
            return
        chat.write(markup("[bold #9be7ff]Available agents:[/]"))
        for a in agents:
            emoji = a.get("emoji", "")
            name = a.get("name", "?")
            display = a.get("display_name", name)
            engine = a.get("active_backend", a.get("engine", "?"))
            online = "[#63ffd9]\U0001f7e2[/]" if a.get("online") else "[#7fb6c7]\u26aa[/]"
            marker = " \u25c0" if name == self.current_agent else ""
            chat.write(markup(f"  {online} {emoji} [#dff6ff]{name}[/] ([#9be7ff]{display}[/]) [#71b7ff]\u2014[/] [#7fb6c7]{engine}[/]{marker}"))

    # ── /instance command ──────────────────────────────────────────────

    def _client_for_instance(self, target: InstanceTarget) -> TuiApiClient:
        if target.transport == "direct":
            urls = list(target.workbench_urls)
            return TuiApiClient(
                base_url=urls[0],
                fallback_base_urls=urls[1:],
                expected_instance_id=target.instance_id,
            )
        return TuiApiClient(
            remote_url=target.remote_url,
            target_instance=target.instance_id,
            expected_instance_id=target.instance_id,
        )

    async def _show_instances(self, *, refresh: bool = True) -> list[InstanceTarget]:
        chat = self.query_one("#chat-history", ChatHistory)
        targets = await self._instance_resolver.discover(refresh=refresh)
        chat.write(markup("[bold #9be7ff]HASHI instances:[/]"))
        for target in targets:
            selected = target.instance_id == self.current_instance_id
            if selected:
                icon = "[#63ffd9]●[/]"
                state = "current · connected"
            elif target.available:
                icon = "[#9be7ff]○[/]"
                state = f"available · {target.route_kind}"
            else:
                icon = "[#ff7a7a]×[/]"
                state = target.reason or "unavailable"
            chat.write(markup(f"  {icon} [#dff6ff]{target.instance_id}[/]  [#7fb6c7]{state}[/]"))
        if len(targets) == 1:
            chat.write(
                markup(
                    "[#c7ff8a]No trusted peers available. Hashi Remote must be "
                    "on and handshaken on both instances.[/]"
                )
            )
        chat.write(markup("[#c7ff8a]Use: /instance <id> · /instance current · /instance refresh[/]"))
        return targets

    async def _handle_instance_cmd(self, text: str):
        chat = self.query_one("#chat-history", ChatHistory)
        parts = text.split(maxsplit=1)
        argument = parts[1].strip() if len(parts) > 1 else ""
        if not argument or argument.lower() == "refresh":
            await self._show_instances(refresh=True)
            return

        requested = self.launch_instance_id if argument.lower() == "current" else argument.upper()
        if requested == self.current_instance_id:
            chat.write(markup(f"[#c7ff8a]Already connected to {self.current_instance_id}.[/]"))
            return

        async with self._instance_switch_lock:
            targets = await self._instance_resolver.discover(refresh=True)
            target = next((item for item in targets if item.instance_id == requested), None)
            if target is None:
                chat.write(markup(f"[#ff7a7a]Instance '{requested}' was not found through local Hashi Remote.[/]"))
                return
            if not target.available:
                chat.write(markup(f"[#ff7a7a]Cannot switch to {requested}: {target.reason or 'unavailable'}.[/]"))
                return

            candidate = self._client_for_instance(target)
            self._write_log_line(
                f"[TUI] Verifying instance switch {self.current_instance_id} -> {target.instance_id}..."
            )
            health = await candidate.health_info()
            if not health.get("ok"):
                reason = str(health.get("error") or "health check failed")
                logger.warning(
                    "TUI instance switch rolled back: from=%s target=%s reason=%s",
                    self.current_instance_id,
                    target.instance_id,
                    reason,
                )
                self._write_log_line(f"[TUI] Instance switch rejected; keeping {self.current_instance_id}: {reason}")
                chat.write(markup(f"[#ff7a7a]Switch failed: {reason}. Current connection was kept.[/]"))
                return
            agent_result = await candidate.agents_info()
            if not agent_result.get("ok") or not isinstance(agent_result.get("agents"), list):
                reason = str(agent_result.get("error") or "agent directory unavailable")
                logger.warning(
                    "TUI instance switch rolled back: from=%s target=%s reason=%s",
                    self.current_instance_id,
                    target.instance_id,
                    reason,
                )
                self._write_log_line(f"[TUI] Instance switch rejected; keeping {self.current_instance_id}: {reason}")
                chat.write(markup(f"[#ff7a7a]Switch failed: {reason}. Current connection was kept.[/]"))
                return
            agents = agent_result["agents"]

            previous_instance = self.current_instance_id
            self._connection_generation += 1
            generation = self._connection_generation
            self.api = candidate
            self.current_instance_id = target.instance_id
            self.gateway_ok = True
            self.current_agent = None
            self._chat_targets = []
            self.current_agent_display = ""
            self.current_backend = ""
            self._agent_mode = ""
            self._agents_cache = []
            self._agent_refresh_tick = 0
            chat.clear()
            location = self._location_label()
            chat_label = "聊天" if self._ui_language == "zh" else "Chat"
            chat.border_title = f"{chat_label} · {self.current_instance_id} ({location})"
            await self._load_agents(
                client=candidate,
                generation=generation,
                agents=agents,
            )
            self._update_status_bar()
            logger.info(
                "TUI instance switch committed: from=%s target=%s transport=%s",
                previous_instance,
                self.current_instance_id,
                target.transport,
            )
            self._write_log_line(
                f"[TUI] Connected to {self.current_instance_id} via "
                f"{'local Workbench' if target.transport == 'direct' else 'authenticated Hashi Remote'}."
            )
            chat.write(markup(f"[#63ffd9]✅ Connected to {self.current_instance_id}.[/]"))

    # ── Status bar ──────────────────────────────────────────────────────

    def _update_status_bar(self):
        bar = self.query_one("#footer-info-box", FooterInfoBox)
        agent = self.current_agent_display or (self.current_agent or "")
        bar.update_state(
            agent,
            self.current_backend,
            self.gateway_ok,
            self._agent_mode,
            self._agents_cache,
            self.current_agent,
            self.current_instance_id,
            self._ui_language,
        )

    # ── Actions ─────────────────────────────────────────────────────────

    def action_toggle_log_pause(self):
        self._log_paused = not self._log_paused
        log = self.query_one("#log-panel", LogPanel)
        base_title = (
            f"主机日志 · {self.launch_instance_id}（本机）"
            if self._ui_language == "zh"
            else f"Host log · {self.launch_instance_id} (local)"
        )
        paused = " [已暂停]" if self._ui_language == "zh" else " [PAUSED]"
        log.border_title = (
            base_title + (paused if self._log_paused else "")
        )

    async def action_quit_app(self):
        await self._shutdown()

    async def _shutdown(self):
        if self._log_follow_task and not self._log_follow_task.done():
            self._log_follow_task.cancel()
        if self.bridge_proc and self.bridge_proc.returncode is None:
            self._write_log_line("[TUI] Shutting down HASHI...")
            self.bridge_proc.terminate()
            try:
                await asyncio.wait_for(self.bridge_proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.bridge_proc.kill()
        self.exit()
