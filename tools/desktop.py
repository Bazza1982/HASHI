"""
Desktop computer-use tools for HASHI — wraps usecomputer (MIT) + xdotool.

Provides screenshot, mouse control, keyboard input, and scroll actions
against a Linux X11 virtual desktop (Xvfb / XRDP session).

Works fully when the Windows host is locked — the X11 display runs
independently inside WSL2/Linux kernel space.

Binary resolution order for usecomputer:
  1. HASHI_USECOMPUTER_BIN env var
  2. <project_root>/tools/bin/usecomputer
  3. ~/ComputerUseTestWSL/bin/usecomputer  (dev install location)
  4. `which usecomputer` (system PATH)

DISPLAY resolution order:
  1. HASHI_DESKTOP_DISPLAY env var
  2. DISPLAY env var if already set to :10+ (XRDP/Xvfb session)
  3. :10  (default XRDP display)
  4. :0   (WSLg fallback)
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import tempfile
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger("Tools.Desktop")


# ---------------------------------------------------------------------------
# Binary / display detection
# ---------------------------------------------------------------------------

def _display_number(display: str) -> int | None:
    try:
        return int(str(display).strip().lstrip(":").split(".")[0])
    except (ValueError, IndexError):
        return None


def _display_socket_exists(display: str) -> bool:
    number = _display_number(display)
    if number is None:
        return False
    return Path(f"/tmp/.X11-unix/X{number}").exists()


def _list_available_displays() -> list[str]:
    displays: list[str] = []
    x11_dir = Path("/tmp/.X11-unix")
    if not x11_dir.is_dir():
        return displays
    for entry in x11_dir.iterdir():
        if not entry.name.startswith("X"):
            continue
        suffix = entry.name[1:]
        if suffix.isdigit():
            displays.append(f":{suffix}")
    return sorted(displays, key=lambda value: _display_number(value) or -1)


@lru_cache(maxsize=1)
def _find_usecomputer() -> str | None:
    # 1. explicit env override
    if env := os.environ.get("HASHI_USECOMPUTER_BIN"):
        found = env if Path(env).is_file() else None
        logger.debug("usecomputer: env override → %s", found)
        return found

    # 2. vendored alongside this module
    vendored = Path(__file__).parent / "bin" / "usecomputer"
    if vendored.is_file():
        logger.debug("usecomputer: using vendored binary at %s", vendored)
        return str(vendored)

    # 3. dev install location from testing
    dev = Path.home() / "ComputerUseTestWSL" / "bin" / "usecomputer"
    if dev.is_file():
        logger.debug("usecomputer: using dev-install binary at %s", dev)
        return str(dev)

    # 4. system PATH
    found = shutil.which("usecomputer")
    logger.debug("usecomputer: PATH lookup → %s", found)
    return found


@lru_cache(maxsize=1)
def _find_xdotool() -> str | None:
    found = shutil.which("xdotool")
    logger.debug("xdotool: PATH lookup → %s", found)
    return found


def _get_display() -> str:
    # explicit override
    if env := os.environ.get("HASHI_DESKTOP_DISPLAY"):
        logger.debug("display: env override → %s", env)
        return env

    current = os.environ.get("DISPLAY", "")

    # Prefer explicit live XRDP/Xvfb displays over WSLg :0 if available.
    current_num = _display_number(current) if current else None
    if current and current_num is not None and current_num >= 10 and _display_socket_exists(current):
        logger.debug("display: using current DISPLAY %s (>=:10 and live)", current)
        return current

    available = _list_available_displays()
    xrdp_like = [item for item in available if (_display_number(item) or -1) >= 10]
    if xrdp_like:
        resolved = xrdp_like[-1]
        logger.debug("display: using highest live XRDP/Xvfb display %s", resolved)
        return resolved

    if current and _display_socket_exists(current):
        logger.debug("display: using current DISPLAY %s (live socket)", current)
        return current

    if available:
        resolved = available[-1]
        logger.debug("display: falling back to highest live display %s", resolved)
        return resolved

    resolved = current or ":0"
    logger.debug("display: falling back to %s", resolved)
    return resolved


async def _run(cmd: list[str], display: str, timeout: int = 15) -> tuple[int, str, str]:
    env = {**os.environ, "DISPLAY": display}
    logger.debug("desktop cmd: %s (DISPLAY=%s)", cmd, display)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        rc = proc.returncode or 0
        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        if rc != 0:
            logger.warning("desktop cmd exit %d: %s | stderr: %s", rc, cmd[0], err or out)
        else:
            logger.debug("desktop cmd OK: %s", cmd[0])
        return rc, out, err
    except asyncio.TimeoutError:
        logger.error("desktop cmd timed out after %ds: %s", timeout, cmd)
        return 1, "", f"command timed out after {timeout}s"
    except FileNotFoundError as e:
        logger.error("desktop cmd not found: %s — %s", cmd[0], e)
        return 1, "", str(e)


async def _best_effort_reset_desktop_input_state(display: str) -> None:
    xdotool = _find_xdotool()
    if not xdotool:
        logger.debug("desktop input-state reset skipped: xdotool not available")
        return

    reset_cmds = [
        [xdotool, "keyup", "Shift_L"],
        [xdotool, "keyup", "Shift_R"],
        [xdotool, "keyup", "Control_L"],
        [xdotool, "keyup", "Control_R"],
        [xdotool, "keyup", "Alt_L"],
        [xdotool, "keyup", "Alt_R"],
        [xdotool, "keyup", "Super_L"],
        [xdotool, "keyup", "Super_R"],
        [xdotool, "mouseup", "1"],
        [xdotool, "mouseup", "2"],
        [xdotool, "mouseup", "3"],
    ]
    for cmd in reset_cmds:
        await _run(cmd, display, timeout=5)


def _xdotool_button(button: str) -> str:
    mapping = {"left": "1", "middle": "2", "right": "3"}
    return mapping.get(button, "1")


def _parse_xdotool_shell(output: str) -> dict[str, int]:
    parsed: dict[str, int] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        try:
            parsed[key] = int(value)
        except ValueError:
            continue
    return parsed


async def _get_active_window(display: str) -> dict | None:
    xdotool = _find_xdotool()
    if not xdotool:
        return None

    rc, out, _ = await _run([xdotool, "getactivewindow"], display, timeout=5)
    if rc != 0 or not out.strip():
        return None
    window_id = out.strip().splitlines()[-1].strip()

    rc_name, name_out, _ = await _run([xdotool, "getwindowname", window_id], display, timeout=5)
    rc_pid, pid_out, _ = await _run([xdotool, "getwindowpid", window_id], display, timeout=5)
    rc_geo, geo_out, _ = await _run([xdotool, "getwindowgeometry", "--shell", window_id], display, timeout=5)
    geometry = _parse_xdotool_shell(geo_out) if rc_geo == 0 else {}
    return {
        "id": int(window_id) if window_id.isdigit() else window_id,
        "title": name_out.strip() if rc_name == 0 else "",
        "pid": int(pid_out.strip()) if rc_pid == 0 and pid_out.strip().isdigit() else None,
        "x": geometry.get("x"),
        "y": geometry.get("y"),
        "width": geometry.get("width"),
        "height": geometry.get("height"),
    }


async def _list_windows(display: str) -> list[dict]:
    xdotool = _find_xdotool()
    if not xdotool:
        return []

    rc, out, err = await _run([xdotool, "search", "--onlyvisible", "--name", ".+"], display, timeout=10)
    if rc != 0:
        lowered = (err or out).lower()
        if "failed to find window" in lowered or "no such window" in lowered:
            return []
        return []

    windows: list[dict] = []
    seen: set[str] = set()
    active = await _get_active_window(display)
    for raw_id in out.splitlines():
        window_id = raw_id.strip()
        if not window_id or window_id in seen:
            continue
        seen.add(window_id)
        rc_name, name_out, _ = await _run([xdotool, "getwindowname", window_id], display, timeout=5)
        rc_pid, pid_out, _ = await _run([xdotool, "getwindowpid", window_id], display, timeout=5)
        rc_geo, geo_out, _ = await _run([xdotool, "getwindowgeometry", "--shell", window_id], display, timeout=5)
        geometry = _parse_xdotool_shell(geo_out) if rc_geo == 0 else {}
        windows.append(
            {
                "id": int(window_id) if window_id.isdigit() else window_id,
                "title": name_out.strip() if rc_name == 0 else "",
                "pid": int(pid_out.strip()) if rc_pid == 0 and pid_out.strip().isdigit() else None,
                "x": geometry.get("x"),
                "y": geometry.get("y"),
                "width": geometry.get("width"),
                "height": geometry.get("height"),
                "is_active": bool(active and str(active.get("id")) == window_id),
            }
        )
    return windows


async def _get_display_info(display: str, uc: str | None) -> dict:
    info: dict[str, object] = {
        "display": display,
        "display_socket_live": _display_socket_exists(display),
        "available_displays": _list_available_displays(),
        "usecomputer": uc,
        "xdotool": _find_xdotool(),
    }
    if uc:
        rc, out, _ = await _run([uc, "mouse", "position", "--json"], display)
        info["mouse_position"] = json.loads(out) if rc == 0 and out else None
        rc, out, _ = await _run([uc, "display", "list", "--json"], display)
        info["displays"] = json.loads(out) if rc == 0 and out else None
    else:
        info["mouse_position"] = None
        info["displays"] = None
    info["active_window"] = await _get_active_window(display)
    return info


# ---------------------------------------------------------------------------
# desktop_screenshot
# ---------------------------------------------------------------------------

async def execute_desktop_screenshot(args: dict) -> str:
    logger.info("desktop_screenshot display=%s annotate=%s", args.get("display"), args.get("annotate", False))
    uc = _find_usecomputer()
    if not uc:
        return (
            "Error: usecomputer binary not found. "
            "Install with: npm install --prefix ~/ComputerUseTestWSL usecomputer"
        )

    display = args.get("display") or _get_display()
    annotate = bool(args.get("annotate", False))
    save_path = args.get("save_path", "")

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        tmp = tf.name

    try:
        cmd = [uc, "screenshot", tmp, "--json"]
        if annotate:
            cmd.append("--annotate")

        rc, out, err = await _run(cmd, display)
        if rc != 0:
            return f"Error: screenshot failed (exit {rc}): {err or out}"

        png = Path(tmp)
        if not png.exists() or png.stat().st_size == 0:
            return f"Error: screenshot produced empty file. stderr: {err}"

        # optionally copy to user-specified path
        if save_path:
            dest = Path(save_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(png.read_bytes())
            saved_note = f"\nSaved to: {dest}"
        else:
            saved_note = ""

        b64 = base64.b64encode(png.read_bytes()).decode()
        size_kb = png.stat().st_size // 1024

        # parse metadata from usecomputer JSON output
        meta = out or "{}"
        extra = await _get_display_info(display, uc)
        return (
            f"Screenshot OK — DISPLAY={display}, {size_kb}KB\n"
            f"Metadata: {meta}\n"
            f"Desktop info: {json.dumps(extra, ensure_ascii=False)}{saved_note}\n"
            f"data:image/png;base64,{b64}"
        )
    finally:
        try:
            Path(tmp).unlink(missing_ok=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# desktop_mouse_move
# ---------------------------------------------------------------------------

async def execute_desktop_mouse_move(args: dict) -> str:
    logger.info("desktop_mouse_move x=%s y=%s", args.get("x"), args.get("y"))
    display = args.get("display") or _get_display()
    x = args.get("x")
    y = args.get("y")
    if x is None or y is None:
        return "Error: x and y are required"

    xdotool = _find_xdotool()
    if xdotool:
        rc, out, err = await _run([xdotool, "mousemove", "--sync", str(x), str(y)], display)
        await _best_effort_reset_desktop_input_state(display)
        if rc != 0:
            return f"Error: mouse move failed: {err or out}"
        return f"Mouse moved to ({x}, {y}) on DISPLAY={display} via xdotool"

    uc = _find_usecomputer()
    if not uc:
        return "Error: neither xdotool nor usecomputer found."

    rc, out, err = await _run([uc, "mouse", "move", "-x", str(x), "-y", str(y)], display)
    await _best_effort_reset_desktop_input_state(display)
    if rc != 0:
        return f"Error: mouse move failed: {err or out}"
    return f"Mouse moved to ({x}, {y}) on DISPLAY={display}"


# ---------------------------------------------------------------------------
# desktop_click
# ---------------------------------------------------------------------------

async def execute_desktop_click(args: dict) -> str:
    logger.info("desktop_click x=%s y=%s button=%s count=%s", args.get("x"), args.get("y"), args.get("button","left"), args.get("count",1))
    display = args.get("display") or _get_display()
    x = args.get("x")
    y = args.get("y")
    button = args.get("button", "left")
    count = int(args.get("count", 1))

    if x is None or y is None:
        return "Error: x and y are required"

    xdotool = _find_xdotool()
    if xdotool:
        rc, out, err = await _run([xdotool, "mousemove", "--sync", str(x), str(y)], display)
        if rc == 0:
            rc, out, err = await _run(
                [xdotool, "click", "--repeat", str(max(count, 1)), _xdotool_button(str(button))],
                display,
            )
        await _best_effort_reset_desktop_input_state(display)
        if rc != 0:
            return f"Error: click failed: {err or out}"
        return f"Clicked ({x}, {y}) button={button} count={count} on DISPLAY={display} via xdotool"

    uc = _find_usecomputer()
    if not uc:
        return "Error: neither xdotool nor usecomputer found."

    cmd = [uc, "click", "-x", str(x), "-y", str(y), "--button", button, "--count", str(count)]
    rc, out, err = await _run(cmd, display)
    await _best_effort_reset_desktop_input_state(display)
    if rc != 0:
        return f"Error: click failed: {err or out}"
    return f"Clicked ({x}, {y}) button={button} count={count} on DISPLAY={display}"


# ---------------------------------------------------------------------------
# desktop_type  (uses xdotool for full Unicode/space/special-char support)
# ---------------------------------------------------------------------------

async def execute_desktop_type(args: dict) -> str:
    text = args.get("text", "")
    logger.info("desktop_type len=%d preview=%r", len(text), text[:40])
    display = args.get("display") or _get_display()
    text = args.get("text", "")
    if not text:
        return "Error: text is required"

    delay_ms = int(args.get("delay_ms", 30))

    # prefer xdotool (handles all chars including space, dash, unicode)
    xdotool = _find_xdotool()
    if xdotool:
        rc, out, err = await _run(
            [xdotool, "type", "--delay", str(delay_ms), "--", text],
            display,
        )
        await _best_effort_reset_desktop_input_state(display)
        if rc == 0:
            return f"Typed {len(text)} chars via xdotool on DISPLAY={display}"
        # xdotool failed — fall through to usecomputer

    # fallback: usecomputer type (works for alphanumeric, may fail on space/dash)
    uc = _find_usecomputer()
    if not uc:
        return "Error: neither xdotool nor usecomputer found."

    rc, out, err = await _run([uc, "type", text], display)
    await _best_effort_reset_desktop_input_state(display)
    if rc != 0:
        return f"Error: type failed (xdotool unavailable, usecomputer): {err or out}"
    return f"Typed {len(text)} chars via usecomputer on DISPLAY={display}"


# ---------------------------------------------------------------------------
# desktop_key  (key press / combo via usecomputer press)
# ---------------------------------------------------------------------------

async def execute_desktop_key(args: dict) -> str:
    logger.info("desktop_key key=%r", args.get("key"))
    display = args.get("display") or _get_display()
    key = args.get("key", "")
    if not key:
        return "Error: key is required (e.g. 'ctrl+s', 'alt+F4', 'Return')"

    xdotool = _find_xdotool()
    if xdotool:
        rc, out, err = await _run([xdotool, "key", "--clearmodifiers", key], display)
        await _best_effort_reset_desktop_input_state(display)
        if rc != 0:
            return f"Error: key press failed: {err or out}"
        return f"Pressed '{key}' on DISPLAY={display} via xdotool"

    uc = _find_usecomputer()
    if not uc:
        return "Error: neither xdotool nor usecomputer found."

    rc, out, err = await _run([uc, "press", key], display)
    await _best_effort_reset_desktop_input_state(display)
    if rc != 0:
        return f"Error: key press failed: {err or out}"
    return f"Pressed '{key}' on DISPLAY={display}"


# ---------------------------------------------------------------------------
# desktop_scroll
# ---------------------------------------------------------------------------

async def execute_desktop_scroll(args: dict) -> str:
    logger.info("desktop_scroll direction=%s amount=%s at=(%s,%s)", args.get("direction","down"), args.get("amount",3), args.get("x"), args.get("y"))
    display = args.get("display") or _get_display()
    direction = args.get("direction", "down")
    amount = int(args.get("amount", 3))
    x = args.get("x")
    y = args.get("y")

    if direction not in ("up", "down", "left", "right"):
        return "Error: direction must be one of: up, down, left, right"

    xdotool = _find_xdotool()
    if xdotool:
        if x is not None and y is not None:
            rc, out, err = await _run([xdotool, "mousemove", "--sync", str(x), str(y)], display)
            if rc != 0:
                await _best_effort_reset_desktop_input_state(display)
                return f"Error: scroll failed: {err or out}"
        button_map = {"up": "4", "down": "5", "left": "6", "right": "7"}
        rc, out, err = await _run(
            [xdotool, "click", "--repeat", str(max(amount, 1)), button_map[direction]],
            display,
        )
        await _best_effort_reset_desktop_input_state(display)
        if rc != 0:
            return f"Error: scroll failed: {err or out}"
        return f"Scrolled {direction} x{amount} on DISPLAY={display} via xdotool"

    uc = _find_usecomputer()
    if not uc:
        return "Error: neither xdotool nor usecomputer found."

    cmd = [uc, "scroll", direction, str(amount)]
    if x is not None and y is not None:
        cmd += ["--at", f"{x},{y}"]

    rc, out, err = await _run(cmd, display)
    await _best_effort_reset_desktop_input_state(display)
    if rc != 0:
        return f"Error: scroll failed: {err or out}"
    return f"Scrolled {direction} x{amount} on DISPLAY={display}"


# ---------------------------------------------------------------------------
# desktop_info
# ---------------------------------------------------------------------------

async def execute_desktop_info(args: dict) -> str:
    uc = _find_usecomputer()
    display = args.get("display") or _get_display()
    info = await _get_display_info(display, uc)
    lines = [
        f"DISPLAY: {display}",
        f"display_socket_live: {info['display_socket_live']}",
        f"available_displays: {json.dumps(info['available_displays'], ensure_ascii=False)}",
    ]

    if not uc:
        lines.append("usecomputer: NOT FOUND")
        lines.append("Install: npm install --prefix ~/ComputerUseTestWSL usecomputer")
    else:
        lines.append(f"usecomputer: {uc}")
        lines.append(f"mouse_position: {json.dumps(info['mouse_position'], ensure_ascii=False)}")
        lines.append(f"displays: {json.dumps(info['displays'], ensure_ascii=False)}")

    lines.append(f"xdotool: {info['xdotool'] or 'NOT FOUND'}")
    lines.append(f"active_window: {json.dumps(info['active_window'], ensure_ascii=False)}")
    return "\n".join(lines)


async def execute_desktop_window_list(args: dict) -> str:
    display = args.get("display") or _get_display()
    title_contains = str(args.get("title_contains", "") or "").lower()
    windows = await _list_windows(display)
    if title_contains:
        windows = [item for item in windows if title_contains in str(item.get("title", "")).lower()]
    return json.dumps({"display": display, "windows": windows}, ensure_ascii=False, indent=2)


async def execute_desktop_window_focus(args: dict) -> str:
    display = args.get("display") or _get_display()
    xdotool = _find_xdotool()
    if not xdotool:
        return "Error: xdotool not found."

    window_id = args.get("window_id")
    title_contains = str(args.get("title_contains", "") or "").lower()

    target_id: str | None = None
    if window_id is not None:
        target_id = str(int(window_id))
    elif title_contains:
        for item in await _list_windows(display):
            if title_contains in str(item.get("title", "")).lower():
                target_id = str(item["id"])
                break
    else:
        return "Error: window_id or title_contains is required"

    if not target_id:
        return "Error: desktop window focus failed: target window not found"

    rc, out, err = await _run([xdotool, "windowactivate", "--sync", target_id], display)
    if rc != 0:
        return f"Error: desktop window focus failed: {err or out}"
    focused = await _get_active_window(display)
    return (
        f"Focused window id={focused.get('id')} title={focused.get('title', '')} on DISPLAY={display}"
        if focused
        else f"Focused window id={target_id} on DISPLAY={display}"
    )
