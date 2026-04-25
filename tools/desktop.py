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
import logging
import os
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger("Tools.Desktop")


# ---------------------------------------------------------------------------
# Binary / display detection
# ---------------------------------------------------------------------------

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


def _get_display() -> str:
    # explicit override
    if env := os.environ.get("HASHI_DESKTOP_DISPLAY"):
        logger.debug("display: env override → %s", env)
        return env

    current = os.environ.get("DISPLAY", "")
    # prefer :10+ (XRDP/Xvfb session) over :0 (WSLg)
    try:
        num = int(current.lstrip(":").split(".")[0])
        if num >= 10:
            logger.debug("display: using current DISPLAY %s (>=:10)", current)
            return current
    except (ValueError, IndexError):
        pass

    # default to :10 (XRDP convention), fall back to :0
    xrdp_sock = Path("/tmp/.X11-unix/X10")
    if xrdp_sock.exists():
        logger.debug("display: X10 socket found, using :10")
        return ":10"
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
    xdotool = shutil.which("xdotool")
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
        return (
            f"Screenshot OK — DISPLAY={display}, {size_kb}KB\n"
            f"Metadata: {meta}{saved_note}\n"
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
    uc = _find_usecomputer()
    if not uc:
        return "Error: usecomputer binary not found."

    display = args.get("display") or _get_display()
    x = args.get("x")
    y = args.get("y")
    if x is None or y is None:
        return "Error: x and y are required"

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
    uc = _find_usecomputer()
    if not uc:
        return "Error: usecomputer binary not found."

    display = args.get("display") or _get_display()
    x = args.get("x")
    y = args.get("y")
    button = args.get("button", "left")
    count = int(args.get("count", 1))

    if x is None or y is None:
        return "Error: x and y are required"

    cmd = [uc, "click", "-x", str(x), "-y", str(y),
           "--button", button, "--count", str(count)]
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
    xdotool = shutil.which("xdotool")
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
    uc = _find_usecomputer()
    if not uc:
        return "Error: usecomputer binary not found."

    display = args.get("display") or _get_display()
    key = args.get("key", "")
    if not key:
        return "Error: key is required (e.g. 'ctrl+s', 'alt+F4', 'Return')"

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
    uc = _find_usecomputer()
    if not uc:
        return "Error: usecomputer binary not found."

    display = args.get("display") or _get_display()
    direction = args.get("direction", "down")
    amount = int(args.get("amount", 3))
    x = args.get("x")
    y = args.get("y")

    if direction not in ("up", "down", "left", "right"):
        return "Error: direction must be one of: up, down, left, right"

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

    lines = [f"DISPLAY: {display}"]

    if not uc:
        lines.append("usecomputer: NOT FOUND")
        lines.append("Install: npm install --prefix ~/ComputerUseTestWSL usecomputer")
    else:
        lines.append(f"usecomputer: {uc}")

        rc, out, _ = await _run([uc, "mouse", "position", "--json"], display)
        lines.append(f"mouse_position: {out if rc == 0 else 'unavailable'}")

        rc, out, _ = await _run([uc, "display", "list", "--json"], display)
        lines.append(f"displays: {out if rc == 0 else 'unavailable'}")

    xdotool = shutil.which("xdotool")
    lines.append(f"xdotool: {xdotool or 'NOT FOUND'}")

    return "\n".join(lines)
