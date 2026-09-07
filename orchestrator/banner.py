"""HASHI startup animation with a platform-safe static fallback.

Interactive startup plays the HASHI animation by default.  The static header
is a technical fallback for non-interactive or cursor-control-incompatible
terminals; presentation failure must never govern runtime availability.

Sequence
  Phase 1 — kanji build:  木 (wood) + 喬 (tall) → 橋 (bridge) with glow
  Phase 2 — scramble:     HASHI logo fills with half-width katakana, then
                          each line decrypts left→right into block art
  Phase 2.5 — poem:       logo possession → kanji evaporate → poem crystallises
                          to the right of the logo
  Phase 3 — status table: agents · workbench · api gateway · whatsapp
  Phase 4 — agent results: waits for any stragglers, then prints ✓/✗ per agent
"""

from __future__ import annotations

import itertools
import json
import os
import random
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from orchestrator.path_presentation import display_user_path
from orchestrator.terminal_console import (
    ConsoleWriteResult,
    prepare_terminal_animation,
    record_output_exception,
    safe_print,
)

PRODUCT_NAME = "HASHI"
PRODUCT_DESCRIPTION = "Professional Agentic AI System"
PRODUCT_ENGINE = "Powered by HER-V2 - Flexible with CLI backends"
PRODUCT_CREDIT = "Designed by Barry Li"
_STARTUP_RECORD_FILENAME = "startup_presentation.jsonl"
_startup_record_lock = threading.RLock()


@dataclass(frozen=True)
class AgentBannerStatus:
    name: str
    state: str
    telegram_connected: bool | None = None


@dataclass(frozen=True)
class ServiceBannerStatus:
    name: str
    url: str


def service_banner_statuses(services: dict) -> list[ServiceBannerStatus]:
    """Map stable service identifiers to user-facing labels in the renderer."""
    rows = []
    for key, label in (("workbench", "Backend API"), ("api_gateway", "API Gateway")):
        url = str((services.get(key) or {}).get("base_url") or "").strip()
        if url:
            rows.append(ServiceBannerStatus(name=label, url=url))
    return rows


@dataclass(frozen=True)
class StartupAnimationResult:
    """Observable outcome of the best-effort startup animation."""

    completed: bool
    attempted: bool
    reason: str
    sink: str | None = None


class _StartupAnimationOutputError(RuntimeError):
    """Raised internally after every available terminal sink rejected output."""


def _plain_field(value: object, *, fallback: str = "-") -> str:
    """Remove terminal controls while retaining ordinary path characters."""

    cleaned = "".join(
        char if char >= " " and char != "\x7f" else "?"
        for char in str(value or "")
    ).strip()
    return cleaned or fallback


def _safe_service_url(value: object) -> str:
    """Keep a service origin while dropping credentials, query, and fragment."""

    cleaned = _plain_field(value)
    try:
        parsed = urlsplit(cleaned)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "-"
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
        return urlunsplit((parsed.scheme, netloc, parsed.path or "", "", ""))
    except (TypeError, ValueError):
        return "-"


def _delivery_record(result: ConsoleWriteResult) -> dict[str, object]:
    return {
        "delivered": result.success,
        "sink": result.sink,
        "failed_attempts": len(result.failures),
    }


def _record_startup_presentation(
    audit_root: object | None,
    *,
    section: str,
    payload: dict[str, object],
    delivery: ConsoleWriteResult,
) -> None:
    """Append a deliberately content-bounded startup presentation record."""

    if audit_root is None:
        return
    root = Path(str(audit_root)).expanduser()
    if not root.is_absolute():
        return
    path = root.resolve() / "logs" / _STARTUP_RECORD_FILENAME
    record = {
        "version": 1,
        "recorded_at": datetime.now(UTC).isoformat(),
        "event": "startup_presentation",
        "section": section,
        **payload,
        "delivery": _delivery_record(delivery),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(record, ensure_ascii=True, sort_keys=True)
        with _startup_record_lock, path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded + "\n")
            handle.flush()
    except Exception as exc:
        record_output_exception(
            purpose="startup_presentation_audit",
            sink="startup_presentation_log",
            error=exc,
        )


def show_startup_header(
    *,
    instance_name: object,
    instance_path: object,
    platform_name: str | None = None,
    distro_name: str | None = None,
    audit_root: object | None = None,
    include_branding: bool = True,
    styled: bool = False,
) -> ConsoleWriteResult:
    """Print the immutable identity portion of the startup summary."""

    name = _plain_field(instance_name, fallback="HASHI")
    location = display_user_path(
        str(instance_path or ""),
        platform_name=platform_name,
        distro_name=distro_name,
    )
    location = _plain_field(location)
    identity_lines = (
        f"Instance    {name}",
        f"Location    {location}",
        "",
    )
    if styled:
        identity_lines = (
            f"{_c(241)}Instance    {_R}{_c(111)}{name}{_R}",
            f"{_c(241)}Location    {_R}{_c(250)}{location}{_R}",
            "",
        )
    lines = identity_lines
    if include_branding:
        lines = (
            PRODUCT_NAME,
            PRODUCT_DESCRIPTION,
            PRODUCT_ENGINE,
            PRODUCT_CREDIT,
            "",
            *identity_lines,
        )
    result = safe_print(
        "\n".join(lines),
        end="\n",
        purpose="startup_banner_header",
    )
    _record_startup_presentation(
        audit_root,
        section="identity",
        payload={"instance": name, "location": location},
        delivery=result,
    )
    return result


def show_startup_status(
    *,
    agents: list[AgentBannerStatus],
    services: list[ServiceBannerStatus],
    instance_name: object = "HASHI",
    audit_root: object | None = None,
    styled: bool = False,
) -> ConsoleWriteResult:
    """Print verified Agent and service state after startup has settled."""

    lines = [f"{_c(75)}{_BOLD}Agents{_R}" if styled else "Agents"]
    agent_width = max((len(_plain_field(item.name)) for item in agents), default=0)
    agent_width = max(10, agent_width + 4)
    if not agents:
        lines.append("  none      OFFLINE")
    agent_records = []
    for item in agents:
        name = _plain_field(item.name)
        state = _plain_field(item.state, fallback="UNKNOWN").upper()
        if styled:
            state_colour = _c(108) if state == "ONLINE" else _c(203)
            line = (
                f"  {_c(250)}{name:<{agent_width}}{_R}"
                f"{state_colour}{_BOLD}{state}{_R}"
            )
        else:
            line = f"  {name:<{agent_width}}{state}"
        if item.telegram_connected is not None:
            telegram = "CONNECTED" if item.telegram_connected else "DISCONNECTED"
            if styled:
                telegram_colour = _c(108) if item.telegram_connected else _c(203)
                line += (
                    f" {_c(240)}| Telegram{_R} "
                    f"{telegram_colour}{telegram}{_R}"
                )
            else:
                line += f" | Telegram {telegram}"
        lines.append(line)
        agent_records.append(
            {
                "name": name,
                "state": state,
                "telegram_connected": item.telegram_connected,
            }
        )

    service_records = []
    if services:
        service_heading = f"{_c(75)}{_BOLD}Services{_R}" if styled else "Services"
        lines.extend(("", service_heading))
        service_width = max(
            13,
            max(len(_plain_field(item.name)) for item in services) + 2,
        )
        for item in services:
            name = _plain_field(item.name)
            url = _safe_service_url(item.url)
            if styled:
                lines.append(
                    f"  {_c(250)}{name:<{service_width}}{_R}{_c(111)}{url}{_R}"
                )
            else:
                lines.append(f"  {name:<{service_width}}{url}")
            service_records.append({"name": name, "url": url})
    lines.append("")
    result = safe_print(
        "\n".join(lines),
        end="\n",
        purpose="startup_banner_status",
    )
    _record_startup_presentation(
        audit_root,
        section="status",
        payload={
            "instance": _plain_field(instance_name, fallback="HASHI"),
            "agents": agent_records,
            "services": service_records,
        },
        delivery=result,
    )
    return result

_BOLD = "\033[1m"
_R    = "\033[0m"

def _c(n):     return f"\033[38;5;{n}m"
def _write(s):
    result = safe_print(str(s), end="", purpose="startup_animation_frame")
    if not result.success:
        raise _StartupAnimationOutputError("all terminal output sinks failed")
    return result
def _line(s=""):
    return _write(f"{s}\n")
def _cls():    _write("\033[2J\033[H")
def _hide():   _write("\033[?25l")
def _show():   _write("\033[?25h")

_LOGO = [
    "  ██╗  ██╗  █████╗ ███████╗██╗  ██╗██╗",
    "  ██║  ██║ ██╔══██╗██╔════╝██║  ██║██║",
    "  ███████║ ███████║███████╗███████║██║",
    "  ██╔══██║ ██╔══██║╚════██║██╔══██║██║",
    "  ██║  ██║ ██║  ██║███████║██║  ██║██║",
    "  ╚═╝  ╚═╝ ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝",
]
_NROWS   = len(_LOGO)
_GRAD    = [69, 75, 111, 135, 133, 99]
_SPIN    = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_HANKAKU = list("ｦｧｨｩｪｫｬｭｮｯｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ")

# full-width chars keep column alignment with kanji (each = 2 terminal cols)
_FW_KANA  = list("アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワン")
_FW_KANJI = list("橋木喬水火山空海時光風雷電影鉄道城夢力波炎氷")
_FW_ALL   = _FW_KANA + _FW_KANJI
_ASCII_SCRAMBLE = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789#$%&*+-=<>")

def _glyph_profile() -> str:
    profile = os.environ.get("BRIDGE_BANNER_GLYPH_PROFILE", "full").strip().lower()
    if profile in {"latin", "latin-safe", "wsl-safe", "no-cjk"}:
        return "latin"
    return "full"


def _full_glyphs_enabled() -> bool:
    return _glyph_profile() == "full"


def _glyph_symbols(full_glyphs: bool | None = None) -> dict[str, str]:
    if full_glyphs is None:
        full_glyphs = _full_glyphs_enabled()
    if full_glyphs:
        return {
            "online": "✓",
            "local": "⚡",
            "connecting": "⠙",
            "failed": "✗",
            "inactive": "•",
            "ellipsis": "…",
        }
    return {
        "online": "OK",
        "local": "!!",
        "connecting": "..",
        "failed": "XX",
        "inactive": "--",
        "ellipsis": "...",
    }


def _stdout_looks_unicode_safe() -> bool:
    if os.environ.get("BRIDGE_FORCE_ASCII_BANNER") == "1":
        return False

    # Interactive Windows rendering uses WriteConsoleW, so its Unicode safety
    # does not depend on a legacy code page or an opt-in environment variable.
    if os.name == "nt":
        return True

    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    if "utf" not in encoding:
        return False

    try:
        import locale
        preferred = (locale.getpreferredencoding(False) or "").lower()
        if "utf" in preferred:
            return True
    except Exception:
        pass

    return os.environ.get("LANG", "").lower().endswith("utf-8")


def _show_ascii_startup_banner(
    agent_names: list,
    boot_state: dict | None = None,
    boot_reason: dict | None = None,
    workbench_port: int | None = None,
    wa_enabled: bool = False,
    api_gateway_enabled: bool = False,
    skipped_agents: list | None = None,
    logo_only: bool = False,
    inactive_agents: list | None = None,
    startup_progress: dict | None = None,
) -> None:
    _line()
    _line(f"  {PRODUCT_NAME}")
    _line(f"  {PRODUCT_DESCRIPTION}")
    _line(f"  {PRODUCT_ENGINE}")
    _line()
    _line(f"  {PRODUCT_CREDIT}")
    _line()

    if logo_only:
        return

    _line(f"  agents      {len(agent_names)} active")
    _line(f"  workbench   :{workbench_port}" if workbench_port else "  workbench   disabled")
    _line(f"  api gateway {'enabled' if api_gateway_enabled else 'disabled'}")
    _line(f"  whatsapp    {'enabled' if wa_enabled else 'disabled'}")

    if skipped_agents:
        _line()
        _line("  skipped (backend unavailable):")
        for name, reason in skipped_agents:
            _line(f"    x {name}: {reason}")

    if boot_state is not None:
        last_line = ""
        while not all(
            state in ("online", "local", "failed")
            for state in boot_state.values()
        ):
            progress = startup_progress or {}
            completed = int(progress.get("completed") or 0)
            total = int(progress.get("total") or len(agent_names))
            percent = int(progress.get("percent") or 0)
            agent_percent = int(progress.get("agent_percent") or 0)
            elapsed = float(progress.get("elapsed_seconds") or 0.0)
            phase = str(progress.get("phase") or "starting").replace("_", " ")
            line = (
                f"  startup {percent}% | agents {completed}/{total} "
                f"({agent_percent}%) | "
                f"{phase} | {elapsed:.1f}s"
            )
            if line != last_line:
                _write(f"\r\033[K{line}")
                last_line = line
            time.sleep(0.12)
        if last_line:
            _write("\r\033[K")

        _line()
        for name in agent_names:
            state = boot_state.get(name, "pending")
            if state == "online":
                _line(f"  ok  {name}")
            elif state == "local":
                _line(f"  !!  {name} (local mode)")
            elif state == "failed":
                _line(f"  xx  {name} failed")
            elif state == "connecting":
                reason = (boot_reason or {}).get(name, "")
                suffix = f" ({reason})" if reason else ""
                _line(f"  ..  {name} still connecting{suffix}")
            else:
                _line(f"  --  {name} pending")
        
        for name in (inactive_agents or []):
            _line(f"  --  {name} (inactive)")
    else:
        _line()
        _line(f"  queued {len(agent_names)} agent(s)")

    _line()
    _line("  starting up")
    _line()


# ── atomic frame renderer ─────────────────────────────────────────────────────
# Renders logo + optional side text in a single top-to-bottom pass.
# No cursor save/restore — eliminates conflicts with _refresh().
#
# side_map: {row_index: (text, col_code)}  — rows not in the map are blank.
# Cursor must be positioned one line below the last logo row on entry;
# it will be in the same position on exit.

def _render_frame(lines, row_colors, side_map=None, logo_top=0):
    """Render logo lines with per-row colors and optional side text."""
    if side_map is None:
        side_map = {}
    if logo_top:
        _write(f"\033[{logo_top};1H")
    else:
        _write(f"\033[{_NROWS}A")
    for i, (line, col) in enumerate(zip(lines, row_colors)):
        side = f"   {side_map[i][1]}{side_map[i][0]}{_R}" if i in side_map else ""
        _write(f"\033[K{_BOLD}{_c(col)}{line}{_R}{side}\n")


def _corrupt_lines(rate, *, full_glyphs: bool | None = None):
    """Return _LOGO lines with half-width katakana replacing chars at rate.
    Uses _HANKAKU (1 terminal col each) so line widths never change."""
    if full_glyphs is None:
        full_glyphs = _full_glyphs_enabled()
    noise_chars = _HANKAKU if full_glyphs else _ASCII_SCRAMBLE
    result = []
    for line in _LOGO:
        chars = list(line)
        for i, ch in enumerate(chars):
            if ch != ' ' and random.random() < rate:
                chars[i] = random.choice(noise_chars)
        result.append(''.join(chars))
    return result


# ── scramble / spinner helpers ────────────────────────────────────────────────

def _scramble_reveal(
    target: str,
    col: str,
    duration: float = 0.30,
    steps: int = 18,
    row: int = 0,
    *,
    full_glyphs: bool | None = None,
):
    if full_glyphs is None:
        full_glyphs = _full_glyphs_enabled()
    noise_chars = _HANKAKU if full_glyphs else _ASCII_SCRAMBLE
    chars = list(target)
    n = len(chars)
    for step in range(steps + 1):
        cutoff = int(step / steps * n)
        buf = [
            ch if i <= cutoff else (" " if ch == " " else random.choice(noise_chars))
            for i, ch in enumerate(chars)
        ]
        if row:
            _write(f"\033[{row};1H\033[K{_BOLD}{col}{''.join(buf)}{_R}  ")
        else:
            _write(f"\r{_BOLD}{col}{''.join(buf)}{_R}  ")
        time.sleep(duration / steps)
    if not row:
        _line()


def _simple_resolve(
    pending: str,
    done: str,
    col: str,
    secs: float = 1.0,
    *,
    full_glyphs: bool | None = None,
):
    t  = time.time() + secs
    if full_glyphs is None:
        full_glyphs = _full_glyphs_enabled()
    symbols = _glyph_symbols(full_glyphs)
    sp = itertools.cycle(_SPIN if full_glyphs else ["|", "/", "-", "\\"])
    while time.time() < t:
        _write(f"\r  {_c(75)}{next(sp)}{_R}  {_c(244)}{pending:<44}{_R}")
        time.sleep(0.08)
    _write(f"\r  {col}{symbols['online']}{_R}  {col}{done:<44}{_R}\n")


def _timed_refresh(
    seconds: float,
    refresh,
    *,
    tick: float = 0.08,
) -> None:
    """Refresh during a delay without ever passing a negative sleep value."""

    end = time.monotonic() + max(0.0, float(seconds))
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        refresh()
        # A Windows console frame may take longer than the remaining delay.
        # Re-check after output instead of assuming time stayed positive.
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(max(0.001, tick), remaining))
    refresh()


# ── main entry point ──────────────────────────────────────────────────────────

def _show_animated_startup_banner(
    agent_names: list,
    boot_state: dict | None = None,
    workbench_port: int | None = None,
    wa_enabled: bool = False,
    api_gateway_enabled: bool = False,
    skipped_agents: list | None = None,
    logo_only: bool = False,
    inactive_agents: list | None = None,
    boot_reason: dict | None = None,
    startup_progress: dict | None = None,
) -> None:
    """
    HASHI startup animation.

    Args:
        agent_names:         agents that will be started
        boot_state:          shared dict updated by agent tasks:
                             {name: "pending"|"connecting"|"online"|"failed"}
                             When supplied, a live status bar tracks each agent.
                             When None, simple spinners are used instead.
        workbench_port:      port if Workbench is active, else None
        wa_enabled:          whether WhatsApp transport is enabled
        api_gateway_enabled: whether the API gateway is enabled
        skipped_agents:      [(name, reason)] for agents that couldn't start
        inactive_agents:     [name] for agents currently disabled
    """
    size       = shutil.get_terminal_size((80, 24))
    rows       = max(size.lines, 8)
    STATUS_ROW = rows - 1
    live       = boot_state is not None
    full_glyphs = _full_glyphs_enabled() and _stdout_looks_unicode_safe()
    symbols = _glyph_symbols(full_glyphs)
    scramble_chars = _HANKAKU if full_glyphs else _ASCII_SCRAMBLE
    poem_noise_chars = _FW_ALL if full_glyphs else _ASCII_SCRAMBLE

    # ── live status bar helpers ───────────────────────────────────────────────

    def _bar() -> str:
        progress = startup_progress or {}
        completed = int(progress.get("completed") or 0)
        total = int(progress.get("total") or len(agent_names))
        percent = int(progress.get("percent") or 0)
        elapsed = float(progress.get("elapsed_seconds") or 0.0)
        phase = str(progress.get("phase") or "starting").replace("_", " ")
        parts = []
        for n in agent_names:
            s = boot_state.get(n, "pending")
            if s == "online":
                parts.append(f"{_c(108)}{n} {symbols['online']}{_R}")
            elif s == "local":
                parts.append(f"{_c(179)}{n} {symbols['local']}{_R}")
            elif s == "connecting":
                parts.append(f"{_c(75)}{n} {symbols['connecting']}{_R}")
            elif s == "failed":
                parts.append(f"{_c(203)}{n} {symbols['failed']}{_R}")
            else:
                parts.append(f"{_c(238)}{n}{_R}")
        aggregate = (
            f"startup {percent}% · agents {completed}/{total} · "
            f"{phase} · {elapsed:.1f}s"
        )
        return f"  {_c(75)}{aggregate}{_R}  |  " + "  ".join(parts)

    def _refresh():
        if not live:
            return
        _write(f"\033[s\033[{STATUS_ROW};1H\033[2K{_bar()}\033[u")

    def _sleep(secs: float, tick: float = 0.08):
        _timed_refresh(secs, _refresh, tick=tick)

    def _clear_bar():
        _write(f"\033[s\033[{STATUS_ROW};1H\033[2K\033[u")

    _hide()
    try:
        # ── Phase 1 : bridge build ────────────────────────────────────────────
        _cls()
        _line("\n\n\n")
        pad = " " * 20

        if full_glyphs:
            _line(f"{pad}{_BOLD}{_c(215)}木{_R}  {_c(240)}ki  ·  wood{_R}")
            _sleep(1.2)

            _line(f"{pad}{_BOLD}{_c(75)}喬{_R}  {_c(240)}qiáo  ·  tall{_R}")
            _sleep(1.2)

            _line(f"{pad}{_c(240)}↓  combine{_R}")
            _sleep(0.5)

            bridge_symbol = "橋"
            bridge_caption = "はし  ·  hashi  ·  bridge"
        else:
            _line(f"{pad}{_BOLD}{_c(215)}HASHI{_R}  {_c(240)}bridge runtime{_R}")
            _sleep(1.2)

            _line(f"{pad}{_BOLD}{_c(75)}agents{_R}  {_c(240)}local orchestration{_R}")
            _sleep(1.2)

            _line(f"{pad}{_c(240)}=> connect{_R}")
            _sleep(0.5)

            bridge_symbol = "HASHI"
            bridge_caption = "hashi  ·  bridge"

        _write(f"{pad}{_BOLD}{_c(69)}{bridge_symbol}{_R}  {_c(240)}{bridge_caption}{_R}")
        time.sleep(0.15)
        for col in [69, 75, 81, 87, 93, 87, 81, 87, 93, 99, 93, 87, 81, 75]:
            _refresh()
            _write(f"\r{pad}{_BOLD}{_c(col)}{bridge_symbol}{_R}  {_c(240)}{bridge_caption}{_R}   ")
            time.sleep(0.09)
        _line("\n\n")
        _sleep(0.5)

        # ── Phase 2 : katakana scramble → HASHI block art ─────────────────────
        _cls()
        _line()
        logo_top = 2  # row 1 blank, logo starts row 2
        for idx, line in enumerate(_LOGO):
            scrambled = "  " + "".join(
                random.choice(scramble_chars) if ch != " " else " "
                for ch in line[2:]
            )
            _write(f"\033[{logo_top + idx};1H\033[K{_c(238)}{scrambled}{_R}")
            time.sleep(0.04)
        _sleep(0.3)
        for idx, (line, col) in enumerate(zip(_LOGO, _GRAD)):
            _scramble_reveal(
                line,
                _c(col),
                duration=0.30,
                row=logo_top + idx,
                full_glyphs=full_glyphs,
            )
            _refresh()
            time.sleep(0.03)

        # ── Phase 2.5 : poem — possession then evaporate & crystallise ────────
        # Only runs if terminal is wide enough for side panel.
        # Uses _render_frame() for every update — logo + side text rendered
        # atomically in one top-to-bottom pass, no cursor save/restore needed.
        term_w = max(shutil.get_terminal_size((80, 24)).columns, 80)
        if term_w >= 75:
            if full_glyphs:
                _POEM = [
                    (1, "「橋」は「知」を繋ぎ、",          _c(220), set()),
                    (2, "「知」は未来 を拓く。",            _c(75),  {4, 5}),
                    (4, "The Bridge connects Intellect;",   _c(244), set()),
                    (5, "Intellect opens the future.",      _c(247), set()),
                ]
            else:
                _POEM = [
                    (1, "The Bridge connects Intellect;",   _c(244), set()),
                    (2, "Intellect opens the future.",      _c(247), set()),
                ]

            # possession: logo corrupts, side fills with matching noise
            for frame in range(11):
                rate     = frame * 0.09
                logo_col = 202 if frame % 2 == 0 else 196
                side_map = {
                    r: (''.join(random.choice(poem_noise_chars) if ch != " " else " " for ch in txt),
                        _c(logo_col))
                    for r, txt, _, _ in _POEM
                }
                _render_frame(_corrupt_lines(min(rate, 0.92), full_glyphs=full_glyphs),
                              [logo_col] * _NROWS, side_map, logo_top=logo_top)
                time.sleep(0.08)

            time.sleep(0.18)

            # snap: logo and side both clean in one call
            _render_frame(_LOGO, list(_GRAD), logo_top=logo_top)

            # aftershock glitches (logo only, side stays blank)
            for i in range(3):
                _render_frame(_corrupt_lines(0.35 - i * 0.1, full_glyphs=full_glyphs),
                              [random.choice([220, 226, 231])] * _NROWS, logo_top=logo_top)
                time.sleep(0.05)
                _render_frame(_LOGO, list(_GRAD), logo_top=logo_top)
                time.sleep(0.06)

            time.sleep(0.25)

            # crystallise: each char resolves independently
            states = {}
            for logo_row, target, _, _ in _POEM:
                states[logo_row] = [{"ch": ch, "done": False} for ch in target]

            for step in range(30):
                progress = step / 29
                side_map = {}
                for logo_row, target, col_code, bold_idx in _POEM:
                    row_out = []
                    for i, s in enumerate(states[logo_row]):
                        if not s["done"] and random.random() < progress * 0.18:
                            s["done"] = True
                        if s["done"]:
                            if i in bold_idx:
                                row_out.append(f"{col_code}{_BOLD}{s['ch']}{_R}")
                            else:
                                row_out.append(f"{col_code}{s['ch']}{_R}")
                        else:
                            noise = random.choice(poem_noise_chars) if s["ch"] != " " else " "
                            row_out.append(f"{_c(202)}{noise}{_R}")
                    side_map[logo_row] = ("".join(row_out), "")
                _render_frame(_LOGO, list(_GRAD), side_map, logo_top=logo_top)
                time.sleep(0.055)

            # Final frame: logo clean + exact poem text, nothing else
            final_side = {}
            for r, txt, col, bold_idx in _POEM:
                if bold_idx:
                    rendered = "".join(
                        f"{col}{_BOLD}{ch}{_R}" if i in bold_idx else f"{col}{ch}{_R}"
                        for i, ch in enumerate(txt)
                    )
                    final_side[r] = (rendered, "")
                else:
                    final_side[r] = (txt, col)
            _render_frame(_LOGO, list(_GRAD), final_side, logo_top=logo_top)

            time.sleep(0.4)

        # ── position cursor below logo for subtitle ──────────────────────────
        _write(f"\033[{logo_top + _NROWS};1H")

        # ── subtitle block ────────────────────────────────────────────────────
        _BLINK = "\033[5m"
        _line(f"  {_c(75)}{_BOLD}{PRODUCT_DESCRIPTION}{_R}")
        _line(f"  {_c(244)}{_BLINK}{PRODUCT_ENGINE}{_R}")
        _line()
        _line(f"  {_c(240)}{PRODUCT_CREDIT}{_R}")

        if logo_only:
            if live:
                while not all(
                    value in ("online", "local", "failed")
                    for value in boot_state.values()
                ):
                    _sleep(0.12)
                _clear_bar()
            _line()
            time.sleep(0.3)
            return

        # ── Phase 3 : status table ────────────────────────────────────────────
        w    = min(max(shutil.get_terminal_size((80, 24)).columns, 80), 80)
        rule_char = "─" if full_glyphs else "-"
        rule = f"  {_c(239)}{rule_char * (w - 4)}{_R}"

        _line()
        _line(rule)
        _line()

        def row(label, value, vc):
            _line(f"  {_c(241)}{label:<16}{_R}{vc}{value}{_R}")

        row("agents",
            f"{len(agent_names)} active", _c(108))
        row("workbench",
            f":{workbench_port}" if workbench_port else "disabled",
            _c(108) if workbench_port else _c(238))
        row("api gateway",
            "enabled" if api_gateway_enabled else "disabled",
            _c(108) if api_gateway_enabled else _c(238))
        row("whatsapp",
            "enabled" if wa_enabled else "disabled",
            _c(108) if wa_enabled else _c(238))

        if skipped_agents:
            _line()
            _line(f"  {_c(180)}skipped  (backend unavailable):{_R}")
            for name, reason in skipped_agents:
                _line(f"  {_c(203)}  {symbols['failed']} {name:<14}{_R}{_c(240)}{reason}{_R}")
            _line(f"  {_c(240)}use /start to bring them online later{_R}")

        _line()
        _line(rule)
        _line()

        # ── Phase 4 : agent results ───────────────────────────────────────────
        if live:
            while not all(
                value in ("online", "local", "failed")
                for value in boot_state.values()
            ):
                _sleep(0.12)

            _clear_bar()

            for n in agent_names:
                s = boot_state.get(n, "pending")
                if s == "online":
                    _line(f"  {_c(108)}{symbols['online']}{_R}  {_c(108)}{n}{_R}")
                elif s == "local":
                    _line(f"  {_c(179)}{symbols['local']}{_R} {_c(179)}{n}  local mode{_R}")
                elif s == "failed":
                    _line(f"  {_c(203)}{symbols['failed']}{_R}  {_c(240)}{n}  failed{_R}")
                else:
                    reason = (boot_reason or {}).get(n, "")
                    suffix = f" ({reason})" if reason else ""
                    _line(f"  {_c(75)}{symbols['connecting']}{_R}  {_c(244)}{n}  still connecting{symbols['ellipsis']}{suffix}{_R}")
                time.sleep(0.05)
            
            for n in (inactive_agents or []):
                _line(f"  {_c(238)}{symbols['inactive']}  {n}{_R}")
                time.sleep(0.02)

            ready_count = sum(
                state in ("online", "local") for state in boot_state.values()
            )
            failed_count = sum(
                state == "failed" for state in boot_state.values()
            )
            elapsed = float(
                (startup_progress or {}).get("elapsed_seconds") or 0.0
            )
            _line()
            _line(
                f"  {_c(108)}agents {len(agent_names)}/{len(agent_names)} "
                f"(100%){_R}  {_c(244)}ready={ready_count} "
                f"failed={failed_count} elapsed={elapsed:.1f}s · "
                f"services starting{_R}"
            )
        else:
            _simple_resolve(f"loading agents{symbols['ellipsis']}",
                            f"{len(agent_names)} agents queued", _c(108), secs=0.7,
                            full_glyphs=full_glyphs)
            if workbench_port:
                _simple_resolve(f"workbench{symbols['ellipsis']}",
                                f"workbench :{workbench_port}", _c(108), secs=0.4,
                                full_glyphs=full_glyphs)
            if wa_enabled:
                _simple_resolve(f"whatsapp transport{symbols['ellipsis']}",
                                "whatsapp active", _c(108), secs=0.4,
                                full_glyphs=full_glyphs)

        _line()
        _line(f"  {_c(60)}starting up{_R}")
        _line()
        time.sleep(0.3)

    finally:
        _clear_bar()
        _show()


def show_startup_banner(
    agent_names: list,
    boot_state: dict | None = None,
    workbench_port: int | None = None,
    wa_enabled: bool = False,
    api_gateway_enabled: bool = False,
    skipped_agents: list | None = None,
    logo_only: bool = False,
    inactive_agents: list | None = None,
    boot_reason: dict | None = None,
    startup_progress: dict | None = None,
    *,
    audit_root: object | None = None,
    fallback_to_static: bool = True,
) -> StartupAnimationResult:
    """Play the normal startup animation, degrading only for technical limits.

    There is intentionally no preference or configuration switch that disables
    animation.  Interactive terminals always attempt it.  Capability and
    rendering failures are observable and callers may render the canonical
    static startup summary as a fallback.
    """

    capability = prepare_terminal_animation()
    if not capability.supported:
        if fallback_to_static:
            try:
                _show_ascii_startup_banner(
                    agent_names=agent_names,
                    boot_state=boot_state,
                    boot_reason=boot_reason,
                    workbench_port=workbench_port,
                    wa_enabled=wa_enabled,
                    api_gateway_enabled=api_gateway_enabled,
                    skipped_agents=skipped_agents,
                    logo_only=logo_only,
                    inactive_agents=inactive_agents,
                    startup_progress=startup_progress,
                )
            except Exception as exc:
                record_output_exception(
                    purpose="startup_animation_static_fallback",
                    sink="static_fallback",
                    error=exc,
                )
        delivery = ConsoleWriteResult(False, None)
        _record_startup_presentation(
            audit_root,
            section="animation",
            payload={
                "outcome": "technical_fallback",
                "reason": capability.reason,
            },
            delivery=delivery,
        )
        return StartupAnimationResult(
            completed=False,
            attempted=False,
            reason=capability.reason,
        )

    try:
        _show_animated_startup_banner(
            agent_names=agent_names,
            boot_state=boot_state,
            workbench_port=workbench_port,
            wa_enabled=wa_enabled,
            api_gateway_enabled=api_gateway_enabled,
            skipped_agents=skipped_agents,
            logo_only=logo_only,
            inactive_agents=inactive_agents,
            boot_reason=boot_reason,
            startup_progress=startup_progress,
        )
    except Exception as exc:
        record_output_exception(
            purpose="startup_animation",
            sink="animation_renderer",
            error=exc,
        )
        # Best effort: leave the terminal usable even if a frame failed after
        # the cursor had been hidden or moved.
        safe_print(
            f"{_R}\033[?25h\033[2J\033[H",
            end="",
            purpose="startup_animation_recovery",
        )
        if fallback_to_static:
            try:
                _show_ascii_startup_banner(
                    agent_names=agent_names,
                    boot_state=boot_state,
                    boot_reason=boot_reason,
                    workbench_port=workbench_port,
                    wa_enabled=wa_enabled,
                    api_gateway_enabled=api_gateway_enabled,
                    skipped_agents=skipped_agents,
                    logo_only=logo_only,
                    inactive_agents=inactive_agents,
                    startup_progress=startup_progress,
                )
            except Exception as fallback_exc:
                record_output_exception(
                    purpose="startup_animation_static_fallback",
                    sink="static_fallback",
                    error=fallback_exc,
                )
        reason = f"render_failed_{type(exc).__name__}"
        delivery = ConsoleWriteResult(False, None)
        _record_startup_presentation(
            audit_root,
            section="animation",
            payload={"outcome": "technical_fallback", "reason": reason},
            delivery=delivery,
        )
        return StartupAnimationResult(
            completed=False,
            attempted=True,
            reason=reason,
        )

    delivery = ConsoleWriteResult(True, capability.sink)
    _record_startup_presentation(
        audit_root,
        section="animation",
        payload={"outcome": "completed", "reason": capability.reason},
        delivery=delivery,
    )
    return StartupAnimationResult(
        completed=True,
        attempted=True,
        reason=capability.reason,
        sink=capability.sink,
    )
