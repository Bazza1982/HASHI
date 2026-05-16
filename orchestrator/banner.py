from __future__ import annotations
"""
HASHI startup animation for bridge-u-f.

When boot_state is supplied the animation runs concurrently with real agent
startups (the caller runs agents in asyncio tasks while this function runs in
a thread-pool executor).  A live status bar at the bottom of the terminal
refreshes every ~80 ms so the operator can watch each agent connect.

When boot_state is None (or stdout is not a TTY) the function is a no-op /
falls back to a simple spinner sequence.

Sequence
  Phase 1 — kanji build:  木 (wood) + 喬 (tall) → 橋 (bridge) with glow
  Phase 2 — scramble:     HASHI logo fills with half-width katakana, then
                          each line decrypts left→right into block art
  Phase 2.5 — poem:       logo possession → kanji evaporate → poem crystallises
                          to the right of the logo
  Phase 3 — status table: agents · workbench · api gateway · whatsapp
  Phase 4 — agent results: waits for any stragglers, then prints ✓/✗ per agent
"""

import os
import sys
import time
import random
import itertools
import shutil

_BOLD = "\033[1m"
_R    = "\033[0m"

def _c(n):     return f"\033[38;5;{n}m"
def _write(s): sys.stdout.write(s); sys.stdout.flush()
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

_ANIM_BUDGET = 14.0   # total seconds before we give up waiting for agents


def _stdout_looks_unicode_safe() -> bool:
    if os.environ.get("BRIDGE_FORCE_ASCII_BANNER") == "1":
        return False

    if os.name == "nt" and os.environ.get("BRIDGE_ALLOW_UNICODE_BANNER") != "1":
        return False

    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    if "utf" not in encoding:
        return False

    if os.name == "nt":
        try:
            import ctypes
            return ctypes.windll.kernel32.GetConsoleOutputCP() == 65001
        except Exception:
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
) -> None:
    print()
    print("  BRIDGE-U-F")
    print("  Universal Flexible Safe AI Agents")
    print("  Powered by CLI backends")
    print()
    print("  Designed by Barry Li")
    print("  AI tool usage")
    print("  (ASCII banner fallback: terminal is not Unicode-safe)")
    print()

    if logo_only:
        return

    print(f"  agents      {len(agent_names)} active")
    print(f"  workbench   :{workbench_port}" if workbench_port else "  workbench   disabled")
    print(f"  api gateway {'enabled' if api_gateway_enabled else 'disabled'}")
    print(f"  whatsapp    {'enabled' if wa_enabled else 'disabled'}")

    if skipped_agents:
        print()
        print("  skipped (backend unavailable):")
        for name, reason in skipped_agents:
            print(f"    x {name}: {reason}")

    if boot_state is not None:
        deadline = time.time() + _ANIM_BUDGET
        while time.time() < deadline:
            if all(state in ("online", "local", "failed") for state in boot_state.values()):
                break
            time.sleep(0.12)

        print()
        for name in agent_names:
            state = boot_state.get(name, "pending")
            if state == "online":
                print(f"  ok  {name}")
            elif state == "local":
                print(f"  !!  {name} (local mode)")
            elif state == "failed":
                print(f"  xx  {name} failed")
            elif state == "connecting":
                reason = (boot_reason or {}).get(name, "")
                suffix = f" ({reason})" if reason else ""
                print(f"  ..  {name} still connecting{suffix}")
            else:
                print(f"  --  {name} pending")
        
        for name in (inactive_agents or []):
            print(f"  --  {name} (inactive)")
    else:
        print()
        print(f"  queued {len(agent_names)} agent(s)")

    print()
    print("  starting up")
    print()


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


def _corrupt_lines(rate):
    """Return _LOGO lines with half-width katakana replacing chars at rate.
    Uses _HANKAKU (1 terminal col each) so line widths never change."""
    result = []
    for line in _LOGO:
        chars = list(line)
        for i, ch in enumerate(chars):
            if ch != ' ' and random.random() < rate:
                chars[i] = random.choice(_HANKAKU)
        result.append(''.join(chars))
    return result


# ── scramble / spinner helpers ────────────────────────────────────────────────

def _scramble_reveal(target: str, col: str, duration: float = 0.30, steps: int = 18, row: int = 0):
    chars = list(target)
    n = len(chars)
    for step in range(steps + 1):
        cutoff = int(step / steps * n)
        buf = [
            ch if i <= cutoff else (" " if ch == " " else random.choice(_HANKAKU))
            for i, ch in enumerate(chars)
        ]
        if row:
            _write(f"\033[{row};1H\033[K{_BOLD}{col}{''.join(buf)}{_R}  ")
        else:
            _write(f"\r{_BOLD}{col}{''.join(buf)}{_R}  ")
        time.sleep(duration / steps)
    if not row:
        print()


def _simple_resolve(pending: str, done: str, col: str, secs: float = 1.0):
    t  = time.time() + secs
    sp = itertools.cycle(_SPIN)
    while time.time() < t:
        _write(f"\r  {_c(75)}{next(sp)}{_R}  {_c(244)}{pending:<44}{_R}")
        time.sleep(0.08)
    _write(f"\r  {col}✓{_R}  {col}{done:<44}{_R}\n")


# ── main entry point ──────────────────────────────────────────────────────────

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
) -> None:
    """
    HASHI startup animation.

    Args:
        agent_names:         agents that will be started
        boot_state:          shared dict updated by agent tasks:
                             {name: "pending"|"connecting"|"online"|"failed"}
                             When supplied, a live status bar tracks each agent.
                             When None, simple spinners are used instead.
        workbench_port:      port if workbench is active, else None
        wa_enabled:          whether WhatsApp transport is enabled
        api_gateway_enabled: whether the API gateway is enabled
        skipped_agents:      [(name, reason)] for agents that couldn't start
        inactive_agents:     [name] for agents currently disabled
    """
    if not sys.stdout.isatty():
        return

    os.system("")  # enable ANSI on Windows

    if not _stdout_looks_unicode_safe():
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
        )
        return

    rows       = shutil.get_terminal_size((80, 24)).lines
    STATUS_ROW = rows - 1
    anim_start = time.time()
    live       = boot_state is not None

    # ── live status bar helpers ───────────────────────────────────────────────

    def _bar() -> str:
        parts = []
        for n in agent_names:
            s = boot_state.get(n, "pending")
            if   s == "online":     parts.append(f"{_c(108)}{n} ✓{_R}")
            elif s == "local":      parts.append(f"{_c(179)}{n} ⚡{_R}")
            elif s == "connecting": parts.append(f"{_c(75)}{n} ⠙{_R}")
            elif s == "failed":     parts.append(f"{_c(203)}{n} ✗{_R}")
            else:                   parts.append(f"{_c(238)}{n}{_R}")
        return "  " + "  ".join(parts)

    def _refresh():
        if not live:
            return
        _write(f"\033[s\033[{STATUS_ROW};1H\033[2K{_bar()}\033[u")

    def _sleep(secs: float, tick: float = 0.08):
        end = time.time() + secs
        while time.time() < end:
            _refresh()
            time.sleep(min(tick, end - time.time()))
        _refresh()

    def _clear_bar():
        _write(f"\033[s\033[{STATUS_ROW};1H\033[2K\033[u")

    _hide()
    try:
        # ── Phase 1 : kanji build ─────────────────────────────────────────────
        _cls()
        print("\n\n\n")
        pad = " " * 20

        print(f"{pad}{_BOLD}{_c(215)}木{_R}  {_c(240)}ki  ·  wood{_R}", flush=True)
        _sleep(1.2)

        print(f"{pad}{_BOLD}{_c(75)}喬{_R}  {_c(240)}qiáo  ·  tall{_R}", flush=True)
        _sleep(1.2)

        print(f"{pad}{_c(240)}↓  combine{_R}", flush=True)
        _sleep(0.5)

        _write(f"{pad}{_BOLD}{_c(69)}橋{_R}  {_c(240)}はし  ·  hashi  ·  bridge{_R}")
        time.sleep(0.15)
        for col in [69, 75, 81, 87, 93, 87, 81, 87, 93, 99, 93, 87, 81, 75]:
            _refresh()
            _write(f"\r{pad}{_BOLD}{_c(col)}橋{_R}  {_c(240)}はし  ·  hashi  ·  bridge{_R}   ")
            time.sleep(0.09)
        print("\n\n")
        _sleep(0.5)

        # ── Phase 2 : katakana scramble → HASHI block art ─────────────────────
        _cls()
        print()
        logo_top = 2  # row 1 blank, logo starts row 2
        for idx, line in enumerate(_LOGO):
            scrambled = "  " + "".join(
                random.choice(_HANKAKU) if ch != " " else " "
                for ch in line[2:]
            )
            _write(f"\033[{logo_top + idx};1H\033[K{_c(238)}{scrambled}{_R}")
            time.sleep(0.04)
        _sleep(0.3)
        for idx, (line, col) in enumerate(zip(_LOGO, _GRAD)):
            _scramble_reveal(line, _c(col), duration=0.30, row=logo_top + idx)
            _refresh()
            time.sleep(0.03)

        # ── Phase 2.5 : poem — possession then evaporate & crystallise ────────
        # Only runs if terminal is wide enough for side panel.
        # Uses _render_frame() for every update — logo + side text rendered
        # atomically in one top-to-bottom pass, no cursor save/restore needed.
        term_w = shutil.get_terminal_size((80, 24)).columns
        if term_w >= 75:
            _POEM = [
                (1, "「橋」は「知」を繋ぎ、",          _c(220), set()),
                (2, "「知」は未来 を拓く。",            _c(75),  {4, 5}),
                (4, "The Bridge connects Intellect;",   _c(244), set()),
                (5, "Intellect opens the future.",      _c(247), set()),
            ]

            # possession: logo corrupts, side fills with matching noise
            for frame in range(11):
                rate     = frame * 0.09
                logo_col = 202 if frame % 2 == 0 else 196
                side_map = {
                    r: (''.join(random.choice(_FW_ALL) for _ in range(len(txt))),
                        _c(logo_col))
                    for r, txt, _, _ in _POEM
                }
                _render_frame(_corrupt_lines(min(rate, 0.92)),
                              [logo_col] * _NROWS, side_map, logo_top=logo_top)
                time.sleep(0.08)

            time.sleep(0.18)

            # snap: logo and side both clean in one call
            _render_frame(_LOGO, list(_GRAD), logo_top=logo_top)

            # aftershock glitches (logo only, side stays blank)
            for i in range(3):
                _render_frame(_corrupt_lines(0.35 - i * 0.1),
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
                            noise = random.choice(_FW_ALL) if s["ch"] != " " else " "
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
        print(f"  {_c(75)}{_BOLD}Universal Flexible Safe AI Agents{_R}  {_c(244)}{_BLINK}Powered by CLI backends{_R}")
        print(f"  {_c(61)}ユニバーサル・フレキシブルな安全AIエージェント{_R}  {_c(250)}{_BLINK}CLIバックエンド駆動{_R}")
        print()
        print(f"  {_c(240)}デザインド・バイ・バリー・リー  エーアイ・ツール・シヨウ{_R}")
        print(f"  {_c(238)}© 2026 Barry Li  ·  {_c(245)}MIT License{_R}")

        if logo_only:
            print()
            time.sleep(0.3)
            return

        # ── Phase 3 : status table ────────────────────────────────────────────
        w    = min(shutil.get_terminal_size((80, 24)).columns, 80)
        rule = f"  {_c(239)}{'─' * (w - 4)}{_R}"

        print()
        print(rule)
        print()

        def row(label, value, vc):
            print(f"  {_c(241)}{label:<16}{_R}{vc}{value}{_R}")

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
            print()
            print(f"  {_c(180)}skipped  (backend unavailable):{_R}")
            for name, reason in skipped_agents:
                print(f"  {_c(203)}  ✗ {name:<14}{_R}{_c(240)}{reason}{_R}")
            print(f"  {_c(240)}use /start to bring them online later{_R}")

        print()
        print(rule)
        print()

        # ── Phase 4 : agent results ───────────────────────────────────────────
        if live:
            deadline = anim_start + _ANIM_BUDGET
            while time.time() < deadline:
                if all(v in ("online", "local", "failed") for v in boot_state.values()):
                    break
                _sleep(0.12)

            _clear_bar()

            for n in agent_names:
                s = boot_state.get(n, "pending")
                if s == "online":
                    print(f"  {_c(108)}✓{_R}  {_c(108)}{n}{_R}")
                elif s == "local":
                    print(f"  {_c(179)}⚡{_R} {_c(179)}{n}  local mode{_R}")
                elif s == "failed":
                    print(f"  {_c(203)}✗{_R}  {_c(240)}{n}  failed{_R}")
                else:
                    reason = (boot_reason or {}).get(n, "")
                    suffix = f" ({reason})" if reason else ""
                    print(f"  {_c(75)}⠙{_R}  {_c(244)}{n}  still connecting…{suffix}{_R}")
                sys.stdout.flush()
                time.sleep(0.05)
            
            for n in (inactive_agents or []):
                print(f"  {_c(238)}•  {n}{_R}")
                sys.stdout.flush()
                time.sleep(0.02)
        else:
            _simple_resolve("loading agents…",
                            f"{len(agent_names)} agents queued", _c(108), secs=0.7)
            if workbench_port:
                _simple_resolve("workbench…",
                                f"workbench :{workbench_port}", _c(108), secs=0.4)
            if wa_enabled:
                _simple_resolve("whatsapp transport…",
                                "whatsapp active", _c(108), secs=0.4)

        print()
        print(f"  {_c(60)}starting up{_R}")
        print()
        time.sleep(0.3)

    finally:
        _clear_bar()
        _show()
