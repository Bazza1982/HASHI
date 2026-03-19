"""HASHI TUI — Textual-based terminal UI wrapping main.py."""
from __future__ import annotations

import asyncio
import os
import json
import random
import sys
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import RichLog, Input, Static
from textual.message import Message
from textual import work

from tui.api_client import TuiApiClient
from tui.onboarding import (
    load_languages, lang_code_from_file, audit_environment,
    verify_openrouter, write_config,
)


STARTUP_LOGO = [
    "  ██╗  ██╗  █████╗ ███████╗██╗  ██╗██╗",
    "  ██║  ██║ ██╔══██╗██╔════╝██║  ██║██║",
    "  ███████║ ███████║███████╗███████║██║",
    "  ██╔══██║ ██╔══██║╚════██║██╔══██║██║",
    "  ██║  ██║ ██║  ██║███████║██║  ██║██║",
    "  ╚═╝  ╚═╝ ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝",
]
STARTUP_HANKAKU = list("ｦｧｨｩｪｫｬｭｮｯｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ")
STARTUP_POEM = [
    "「橋」は「知」を繋ぎ、",
    "「知」は未来を拓く。",
    "The Bridge connects Intellect;",
    "Intellect opens the future.",
]


def markup(text: str) -> Text:
    """Render Rich markup explicitly before writing into RichLog."""
    return Text.from_markup(text)


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
        dock: bottom;
        height: 3;
        background: #0b1824;
        color: #dff6ff;
        border: solid #2a5b82;
    }
    ChatInput:focus {
        border: solid #63ffd9;
    }
    """


class FooterInfoBox(Static):
    """Footer box holding banner metadata, ticker, current status, and connected agents."""

    DEFAULT_CSS = """
    FooterInfoBox {
        height: 5;
        background: #050b12;
        color: #dff6ff;
        border: solid #2a5b82;
        padding: 0 1;
        margin-top: 1;
    }
    """

    TICKER_TEXTS = (
        "「橋」は「知」を繋ぎ、「知」は未来を拓く。",
        '"A bridge connects knowledge, and knowledge opens the future."',
        "「桥」连接知识，知识开拓未来。",
        "「橋」連接知識，知識開拓未來。",
        "「다리」는 지식을 이어주고, 지식은 미래를 열어준다。",
        "„Brücke\" verbindet Wissen, und Wissen erschließt die Zukunft.",
        "« Le pont relie la connaissance, et la connaissance ouvre l'avenir. »",
        "«Мост» соединяет знания, а знания открывают будущее.",
        ".«الجسر» يربط المعرفة، والمعرفة تفتح المستقبل",
    )

    def __init__(self, *args, **kwargs):
        super().__init__("", *args, **kwargs)
        self._content = Text("")
        self._offset = 0
        self._ticker = ""
        self._status_line = " ❌ No agent selected | API: offline"
        self._connected_line = ""

    def on_mount(self):
        separator = "  ✦  "
        self._ticker = separator.join(self.TICKER_TEXTS) + separator
        self.set_interval(0.234, self._advance)
        self._refresh_footer()

    def _advance(self):
        if not self._ticker:
            return
        self._offset = (self._offset + 1) % len(self._ticker)
        self._refresh_footer()

    def update_state(self, agent: str = "", backend: str = "", gateway_ok: bool = False, mode: str = "", agents: list[dict] | None = None, current_agent: str | None = None):
        icon = "\u2705" if gateway_ok else "\u274c"
        agent_part = f"Agent: {agent}" if agent else "No agent selected"
        backend_part = f" | Backend: {backend}" if backend else ""
        mode_part = f" | Mode: {mode}" if mode else ""
        api_part = f" | API: {'connected' if gateway_ok else 'offline'}"
        self._status_line = f" {icon} {agent_part}{backend_part}{mode_part}{api_part}"

        connected = []
        for agent_data in agents or []:
            if not agent_data.get("online"):
                continue
            agent_id = agent_data.get("id") or agent_data.get("name") or "?"
            if current_agent and agent_id == current_agent:
                continue
            display_name = agent_data.get("display_name") or agent_data.get("name") or agent_id
            connected.append(f"[{display_name} ({agent_id})]")
        self._connected_line = f"  Other connected: {' '.join(connected)}" if connected else ""
        self._refresh_footer()

    def _refresh_footer(self):
        width = max(self.size.width or 0, 20)
        stream = self._ticker
        while len(stream) < width * 2:
            stream += self._ticker
        start = self._offset
        view = stream[start:start + width]
        footer_lines = [
            "[bold #9be7ff]HASHI // CLI RETRO[/] [#71b7ff]::[/] [#63ffd9]TERMINAL WORKBENCH[/]   "
            "[#63ffd9]AUTHOR[/] Barry Li   [#71b7ff]WEBSITE[/] https://barryli.phd   [#c7ff8a]LICENSE[/] MIT",
            f"[#63ffd9]{view}[/]",
            f"[#63ffd9]{self._status_line}[/]",
        ]
        if self._connected_line:
            footer_lines.append(f"[#9be7ff]{self._connected_line}[/]")

        self.styles.height = 5 if not self._connected_line else 6
        self._content = markup(
            "\n".join(footer_lines)
        )
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
    }
    #chat-container {
        height: 1fr;
    }
    #footer-info-box {
        height: 5;
    }
    """

    BINDINGS = [
        Binding("ctrl+l", "toggle_log_pause", "Pause Log", show=True),
        Binding("ctrl+q", "quit_app", "Quit", show=True),
    ]

    def __init__(self):
        super().__init__()
        self.bridge_home = self._find_bridge_home()
        self.bridge_proc: asyncio.subprocess.Process | None = None
        self.current_agent: str | None = None
        self._chat_targets: list[str] = []
        self.current_agent_display: str = ""
        self.current_backend: str = ""
        self._agent_mode: str = ""
        self.api = TuiApiClient()
        self.gateway_ok = False
        self._log_paused = False
        self._onboarding: OnboardingPhase | None = None
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

    def compose(self) -> ComposeResult:
        with Vertical(id="main-container"):
            yield LogPanel(id="log-panel")
            with Vertical(id="chat-container"):
                yield ChatHistory(id="chat-history")
                yield ChatInput(placeholder="Type message or /to <agent> ...", id="chat-input")
            yield FooterInfoBox(id="footer-info-box")

    def on_mount(self):
        # Start the intro only after the first screen refresh so frames are visible.
        self.call_after_refresh(self._schedule_startup_sequence)

    def _schedule_startup_sequence(self):
        if self._startup_task and not self._startup_task.done():
            return
        self._startup_task = asyncio.create_task(self._run_startup_sequence())

    async def _run_startup_sequence(self):
        await asyncio.sleep(0.05)
        await self._play_log_startup_animation()
        await asyncio.sleep(1.2)
        agents_path = self.bridge_home / "agents.json"
        needs_onboarding = True
        if agents_path.exists():
            try:
                cfg = json.loads(agents_path.read_text(encoding="utf-8"))
                if cfg.get("agents"):
                    needs_onboarding = False
            except Exception:
                pass

        if needs_onboarding:
            self._start_onboarding()
        else:
            self._start_bridge()

    async def _play_log_startup_animation(self):
        log = self.query_one("#log-panel", LogPanel)

        async def show(lines: list[str], delay: float):
            log.clear()
            log.write(markup("\n".join(lines)))
            await asyncio.sleep(delay)

        kanji_frames = [
            [
                "[bold #ffcf87]木[/]  [#7fb6c7]ki  ·  wood[/]",
                "",
                "[#39566e]Waiting for the bridge to wake up...[/]",
            ],
            [
                "[bold #ffcf87]木[/]  [#7fb6c7]ki  ·  wood[/]",
                "[bold #71b7ff]喬[/]  [#7fb6c7]qiao  ·  tall[/]",
                "",
                "[#39566e]The pieces are lining up...[/]",
            ],
            [
                "[bold #ffcf87]木[/]  [#7fb6c7]ki  ·  wood[/]  [#7fb6c7]+[/]  [bold #71b7ff]喬[/]  [#7fb6c7]qiao  ·  tall[/]",
                "",
                "[bold #63ffd9]橋[/]  [#9be7ff]hashi  ·  bridge[/]",
            ],
        ]
        for frame in kanji_frames:
            await show(frame, 0.45)

        for reveal in range(7):
            progress = reveal / 6
            lines = ["[#39566e]Decrypting startup banner...[/]", ""]
            for idx, line in enumerate(STARTUP_LOGO):
                built = []
                cutoff = int(len(line) * progress)
                for pos, ch in enumerate(line):
                    if ch == " ":
                        built.append(" ")
                    elif pos <= cutoff:
                        built.append(ch)
                    else:
                        built.append(random.choice(STARTUP_HANKAKU))
                color = ["#71b7ff", "#7dc6ff", "#87d4ff", "#92e1ff", "#9eeed8", "#c7ff8a"][idx]
                lines.append(f"[bold {color}]{''.join(built)}[/]")
            await show(lines, 0.16)

        final_logo = ["", *[f"[bold {color}]{line}[/]" for color, line in zip(
            ["#71b7ff", "#7dc6ff", "#87d4ff", "#92e1ff", "#9eeed8", "#c7ff8a"],
            STARTUP_LOGO,
        )]]
        await show(final_logo, 0.25)

        for step in range(6):
            poem_lines = []
            for poem in STARTUP_POEM:
                resolved = []
                for idx, ch in enumerate(poem):
                    if ch == " " or idx / max(len(poem), 1) <= step / 5:
                        resolved.append(ch)
                    else:
                        resolved.append(random.choice(STARTUP_HANKAKU))
                poem_lines.append(f"[#9be7ff]{''.join(resolved)}[/]")
            await show(
                final_logo
                + [
                    "",
                    "[#63ffd9]Universal Flexible Safe AI Agents[/]  [#7fb6c7]Powered by CLI backends[/]",
                    "",
                    *poem_lines,
                    "",
                    "[#7fb6c7]Barry Li[/]  [#71b7ff]https://barryli.phd[/]  [#c7ff8a]MIT[/]",
                ],
                0.14,
            )

        await show(
            final_logo
            + [
                "",
                "[#63ffd9]Universal Flexible Safe AI Agents[/]  [#7fb6c7]Powered by CLI backends[/]",
                "",
                "[#ffdf6b]「橋」は「知」を繋ぎ、[/]",
                "[#71b7ff]「知」は未来を拓く。[/]",
                "[#9fb3c8]The Bridge connects Intellect;[/]",
                "[#dff6ff]Intellect opens the future.[/]",
                "",
                "[#7fb6c7]Barry Li[/]  [#71b7ff]https://barryli.phd[/]  [#c7ff8a]MIT[/]",
                "",
                "[#39566e]Startup animation loaded into HASHI Log[/]",
            ],
            1.0,
        )

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
            self.query_one("#chat-input", ChatInput).placeholder = "Type message or /to <agent> ..."
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
            self.query_one("#chat-input", ChatInput).placeholder = "Type message or /to <agent> ..."
            self.query_one("#chat-history", ChatHistory).border_title = "Chat"
            self._start_bridge()

    # ── Bridge subprocess ───────────────────────────────────────────────

    def _start_bridge(self):
        self._launch_bridge_task()

    @work()
    async def _launch_bridge_task(self):
        log = self.query_one("#log-panel", LogPanel)
        self._attached_log_path = self._resolve_attach_log_path()

        # If API is already up (HASHI already running), just attach — don't start a new process
        if await self.api.health():
            self._write_log_line("[TUI] HASHI is already running — attaching to existing instance.")
            self._start_attached_log_follow()
            self.gateway_ok = True
            await self._load_agents()
            self._start_polling()
            self._update_status_bar()
            return

        self._write_log_line("[TUI] Starting HASHI main process...")

        self.bridge_proc = await asyncio.create_subprocess_exec(
            sys.executable, str(self.bridge_home / "main.py"),
            "--api-gateway",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(self.bridge_home),
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
            except Exception:
                break
        self._write_log_line("[TUI] Bridge process exited.")

    def _resolve_attach_log_path(self) -> Path | None:
        for candidate in (
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
            except Exception:
                pass
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
                self._write_log_line("[TUI] API Gateway connected.")
                await self._load_agents()
                self._start_polling()
                self._update_status_bar()
                return
            await asyncio.sleep(1)
        self._write_log_line("[TUI] Warning: API Gateway not reachable after 60s. Chat disabled.")
        self._update_status_bar()

    async def _load_agents(self):
        agents = await self.api.list_agents()
        self._agents_cache = agents
        self._update_status_bar()
        if agents and not self.current_agent:
            # Auto-select first active agent
            for a in agents:
                if a.get("is_active") or a.get("online"):
                    self._select_agent(a)
                    break
            if not self.current_agent and agents:
                self._select_agent(agents[0])

    def _select_agent(self, agent_data: dict):
        self.current_agent = agent_data.get("name", "")
        self._chat_targets = [self.current_agent] if self.current_agent else []
        self.current_agent_display = agent_data.get("display_name", self.current_agent)
        self.current_backend = agent_data.get("active_backend", agent_data.get("engine", ""))
        self._agent_mode = agent_data.get("mode", "flex")
        self.api.reset_offset(self.current_agent)
        chat = self.query_one("#chat-history", ChatHistory)
        emoji = agent_data.get("emoji", "")
        chat.border_title = f"Chat \u2014 {emoji} {self.current_agent_display} ({self.current_agent})"
        self._update_status_bar()
        # Load recent transcript
        self._load_initial_transcript()

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
            chat.write(markup(f"[bold #71b7ff]You:[/] {text}"))
        elif role == "assistant":
            chat.write(markup(f"[bold #63ffd9]{prefix}:[/] {text}"))

    @work()
    async def _load_initial_transcript(self):
        if not self.current_agent:
            return
        messages = await self.api.get_recent_transcript(self.current_agent, limit=20)
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
                try:
                    self._agent_refresh_tick = (self._agent_refresh_tick + 1) % 5
                    if self._agent_refresh_tick == 0:
                        self._agents_cache = await self.api.list_agents()
                        self._update_status_bar()
                    poll_targets = self._chat_targets[:]
                    if self.current_agent and self.current_agent not in poll_targets:
                        poll_targets.append(self.current_agent)
                    for agent in poll_targets:
                        messages = await self.api.poll_transcript(agent)
                        for msg in messages:
                            if msg.get("role") == "assistant":
                                self._render_transcript_message(msg)
                except Exception:
                    pass
            await asyncio.sleep(1.0)

    # ── Input handling ──────────────────────────────────────────────────

    async def on_input_submitted(self, event: Input.Submitted):
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
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
        if normalized == "/quit":
            await self._shutdown()
            return
        if normalized == "/log":
            self.action_toggle_log_pause()
            return

        # Everything else → send to agent
        if not self.gateway_ok:
            chat = self.query_one("#chat-history", ChatHistory)
            chat.write(markup("[#ff7a7a]API Gateway not connected. Chat unavailable.[/]"))
            return

        chat = self.query_one("#chat-history", ChatHistory)
        chat.write(markup(f"[bold cyan]You:[/] {normalized}"))

        if self.current_agent_display == "ALL":
            # Broadcast to all active agents
            self._send_broadcast(normalized)
            return

        if not self.current_agent:
            chat.write(markup("[yellow]No agent selected. Use /to <name> first.[/]"))
            return
        else:
            self._send_message(normalized, self.current_agent)

    @work()
    async def _send_message(self, text: str, agent: str):
        result = await self.api.send_chat(agent, text)
        if not result.get("ok", True) and "error" in result:
            chat = self.query_one("#chat-history", ChatHistory)
            chat.write(markup(f"[red]Error ({agent}): {result['error']}[/]"))

    @work()
    async def _send_broadcast(self, text: str):
        agents = await self.api.list_agents()
        for a in agents:
            if a.get("is_active") or a.get("online"):
                await self.api.send_chat(a["name"], text)

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

    # ── Status bar ──────────────────────────────────────────────────────

    def _update_status_bar(self):
        bar = self.query_one("#footer-info-box", FooterInfoBox)
        agent = self.current_agent_display or (self.current_agent or "")
        bar.update_state(agent, self.current_backend, self.gateway_ok, self._agent_mode, self._agents_cache, self.current_agent)

    # ── Actions ─────────────────────────────────────────────────────────

    def action_toggle_log_pause(self):
        self._log_paused = not self._log_paused
        log = self.query_one("#log-panel", LogPanel)
        log.border_title = "HASHI Log" + (" [PAUSED]" if self._log_paused else "")

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
