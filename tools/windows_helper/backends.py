from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import tempfile
from pathlib import Path

from PIL import ImageGrab

from tools.windows_use_mcp_client import _run as run_windows_mcp
from . import win32
from .whatsapp_call_probe import probe_whatsapp_call


def find_usecomputer() -> str | None:
    env = os.environ.get("HASHI_WINDOWS_USECOMPUTER_BIN")
    if env and Path(env).exists():
        return env
    appdata = os.environ.get("APPDATA")
    if appdata:
        for candidate in (
            Path(appdata) / "npm" / "usecomputer.cmd",
            Path(appdata) / "npm" / "usecomputer.ps1",
        ):
            if candidate.exists():
                return str(candidate)
    found = shutil.which("usecomputer")
    return found


async def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


def _resolve_provider(provider: str | None, action: str) -> str:
    value = (provider or "auto").strip().lower()
    if value and value != "auto":
        return value
    preferred = {
        "screenshot": "usecomputer",
        "mouse_move": "usecomputer",
        "click": "usecomputer",
        "scroll": "usecomputer",
        "type": "usecomputer",
        "key": "usecomputer",
    }
    return preferred.get(action, "usecomputer")


async def _mcp_text(tool: str, arguments: dict) -> str:
    result = await run_windows_mcp({"tool": tool, "arguments": arguments})
    texts = [item.get("text", "").strip() for item in result.get("content", []) if item.get("type") == "text"]
    return "\n".join(part for part in texts if part).strip()


def _selector(args: dict) -> tuple[int, int, str, str]:
    return (
        int(args.get("window_id", 0) or 0),
        int(args.get("pid", 0) or 0),
        str(args.get("title_contains", "") or ""),
        str(args.get("exact_title", "") or ""),
    )


def _maybe_focus(args: dict) -> dict | None:
    if not bool(args.get("focus_first", True)):
        return None
    window_id, pid, title_contains, exact_title = _selector(args)
    if not any([window_id, pid, title_contains, exact_title]):
        return None
    target = win32.find_window(
        window_id=window_id,
        pid=pid,
        title_contains=title_contains,
        exact_title=exact_title,
    )
    if not target:
        raise RuntimeError("target window not found")
    return win32.focus_window(target)


async def execute_action(action: str, args: dict) -> str:
    provider = _resolve_provider(args.get("provider"), action)
    if action == "whatsapp_call_probe":
        result = probe_whatsapp_call(
            auto_answer=bool(args.get("auto_answer", False)),
            use_uia=bool(args.get("use_uia", True)),
        )
        return json.dumps(result, ensure_ascii=False, indent=2)

    if action == "reset_input_state":
        result = win32.reset_input_state()
        result["state"] = win32.get_input_state()
        return json.dumps(result, ensure_ascii=False, indent=2)

    if action == "window_list":
        _, pid, title_contains, _ = _selector(args)
        windows = win32.list_windows()
        if pid:
            windows = [item for item in windows if item["pid"] == pid]
        if title_contains:
            needle = title_contains.lower()
            windows = [item for item in windows if needle in item["title"].lower()]
        return json.dumps(windows, ensure_ascii=False, indent=2)

    if action == "window_focus":
        window_id, pid, title_contains, exact_title = _selector(args)
        target = win32.find_window(
            window_id=window_id,
            pid=pid,
            title_contains=title_contains,
            exact_title=exact_title,
        )
        if not target:
            return "Error: window focus failed: target window not found"
        focused = win32.focus_window(target)
        return f"Focused window id={focused.get('id')} title={focused.get('title', '')}"

    if action == "window_close":
        window_id, pid, title_contains, exact_title = _selector(args)
        target = win32.find_window(
            window_id=window_id,
            pid=pid,
            title_contains=title_contains,
            exact_title=exact_title,
        )
        if not target:
            return "Error: window close failed: target window not found"
        win32.close_window(target)
        return f"Sent close request to window id={target.get('id')} title={target.get('title', '')}"

    if action == "info":
        uc = find_usecomputer()
        displays = mouse = None
        if uc:
            rc, out, _ = await _run([uc, "display", "list", "--json"])
            displays = json.loads(out) if rc == 0 and out else None
            rc, out, _ = await _run([uc, "mouse", "position", "--json"])
            mouse = json.loads(out) if rc == 0 and out else None
        return json.dumps(
            {
                "provider": "helper",
                "usecomputer_path": uc,
                "mouse_position": mouse,
                "displays": displays,
                "windows": win32.list_windows() if args.get("include_windows", True) else None,
                "input_state": win32.get_input_state(),
            },
            ensure_ascii=False,
            indent=2,
        )

    _maybe_focus(args)
    uc = find_usecomputer()

    if action == "mouse_move":
        x = int(args["x"])
        y = int(args["y"])
        if provider == "usecomputer":
            pos = win32.move_mouse(x, y)
            win32.reset_input_state()
            return f"Mouse moved to ({pos['x']}, {pos['y']}) on Windows host via helper-native"
        if provider == "windows-mcp":
            text = await _mcp_text("Move", {"loc": [x, y]})
            win32.reset_input_state()
            return text or f"Mouse moved to ({x}, {y}) on Windows host via windows-mcp-helper"
        rc, out, err = await _run([uc, "mouse", "move", "-x", str(x), "-y", str(y)])
        win32.reset_input_state()
        return f"Mouse moved to ({x}, {y}) on Windows host via helper" if rc == 0 else f"Error: mouse move failed: {err or out}"

    if action == "click":
        x = int(args["x"])
        y = int(args["y"])
        button = str(args.get("button", "left"))
        count = int(args.get("count", 1))
        if provider == "usecomputer":
            win32.click_mouse(x, y, button=button, count=count)
            win32.reset_input_state()
            return f"Clicked ({x}, {y}) button={button} count={count} on Windows host via helper-native"
        if provider == "windows-mcp":
            text = await _mcp_text("Click", {"loc": [x, y], "button": button, "clicks": count})
            win32.reset_input_state()
            return text or f"Clicked ({x}, {y}) button={button} count={count} on Windows host via windows-mcp-helper"
        rc, out, err = await _run([uc, "click", "-x", str(x), "-y", str(y), "--button", button, "--count", str(count)])
        win32.reset_input_state()
        return f"Clicked ({x}, {y}) button={button} count={count} on Windows host via helper" if rc == 0 else f"Error: click failed: {err or out}"

    if action == "type":
        text = str(args.get("text", ""))
        if provider == "usecomputer":
            typed = win32.type_text(text)
            win32.reset_input_state()
            return f"Typed {typed['text_length']} chars on Windows host via helper-native"
        if provider == "windows-mcp":
            x = args.get("x")
            y = args.get("y")
            if x is None or y is None:
                return "Error: windows-mcp typing requires x and y"
            text_out = await _mcp_text("Type", {"loc": [int(x), int(y)], "text": text})
            win32.reset_input_state()
            return text_out or f"Typed {len(text)} chars on Windows host via windows-mcp-helper"
        rc, out, err = await _run([uc, "type", text], timeout=45)
        win32.reset_input_state()
        return f"Typed {len(text)} chars on Windows host via helper" if rc == 0 else f"Error: type failed: {err or out}"

    if action == "key":
        key = str(args.get("key", ""))
        if provider == "usecomputer":
            win32.press_key_combo(key)
            win32.reset_input_state()
            return f"Pressed '{key}' on Windows host via helper-native"
        if provider == "windows-mcp":
            text = await _mcp_text("Shortcut", {"shortcut": key})
            win32.reset_input_state()
            return text or f"Pressed '{key}' on Windows host via windows-mcp-helper"
        rc, out, err = await _run([uc, "press", key])
        win32.reset_input_state()
        return f"Pressed '{key}' on Windows host via helper" if rc == 0 else f"Error: key press failed: {err or out}"

    if action == "scroll":
        direction = str(args.get("direction", "down"))
        amount = int(args.get("amount", 3))
        if provider == "usecomputer":
            if args.get("x") is not None and args.get("y") is not None:
                win32.move_mouse(int(args["x"]), int(args["y"]))
            horizontal = direction in {"left", "right"}
            win32.scroll_mouse(direction=direction, amount=amount, horizontal=horizontal)
            win32.reset_input_state()
            return f"Scrolled {direction} x{amount} on Windows host via helper-native"
        if provider == "windows-mcp":
            payload = {
                "direction": direction,
                "wheel_times": amount,
                "type": "vertical" if direction in {"up", "down"} else "horizontal",
            }
            if args.get("x") is not None and args.get("y") is not None:
                payload["loc"] = [int(args["x"]), int(args["y"])]
            text = await _mcp_text("Scroll", payload)
            win32.reset_input_state()
            return text or f"Scrolled {direction} x{amount} on Windows host via windows-mcp-helper"
        cmd = [uc, "scroll", direction, str(amount)]
        if args.get("x") is not None and args.get("y") is not None:
            cmd += ["--at", f"{args['x']},{args['y']}"]
        rc, out, err = await _run(cmd)
        win32.reset_input_state()
        return f"Scrolled {direction} x{amount} on Windows host via helper" if rc == 0 else f"Error: scroll failed: {err or out}"

    if action == "screenshot":
        save_path = str(args.get("save_path", "") or "")
        if provider == "usecomputer" and args.get("display") is None and not args.get("annotate"):
            image = ImageGrab.grab(all_screens=True)
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                tmp = Path(tf.name)
            try:
                image.save(tmp, format="PNG")
                data = tmp.read_bytes()
            finally:
                tmp.unlink(missing_ok=True)
            if save_path:
                Path(save_path).parent.mkdir(parents=True, exist_ok=True)
                Path(save_path).write_bytes(data)
            saved_note = f"\nSaved to: {save_path}" if save_path else ""
            return (
                f"Windows screenshot OK — provider=helper-native, {len(data)//1024}KB\n"
                f"Metadata: {{\"all_screens\": true}}{saved_note}\n"
                f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"
            )
        if provider == "windows-mcp":
            payload = {}
            if args.get("display") is not None:
                payload["display"] = [int(args["display"])]
            if args.get("annotate"):
                payload["use_annotation"] = True
            result = await run_windows_mcp({"tool": "Screenshot", "arguments": payload})
            texts = [item.get("text", "").strip() for item in result.get("content", []) if item.get("type") == "text"]
            text = "\n".join(part for part in texts if part).strip()
            image = next((item for item in result.get("content", []) if item.get("type") == "image"), None)
            if not image:
                return f"Error: screenshot failed: {text or 'no image returned'}"
            raw = base64.b64decode(image.get("data", ""))
            if save_path:
                Path(save_path).parent.mkdir(parents=True, exist_ok=True)
                Path(save_path).write_bytes(raw)
            saved_note = f"\nSaved to: {save_path}" if save_path else ""
            return (
                f"Windows screenshot OK — provider=windows-mcp-helper\n"
                f"Details: {text}{saved_note}\n"
                f"data:{image.get('mimeType','image/png')};base64,{image.get('data','')}"
            )

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tmp = tf.name
        try:
            cmd = [uc, "screenshot", tmp, "--json"]
            if args.get("annotate"):
                cmd.append("--annotate")
            if args.get("display") is not None:
                cmd += ["--display", str(args["display"])]
            rc, out, err = await _run(cmd, timeout=45)
            if rc != 0:
                return f"Error: screenshot failed: {err or out}"
            data = Path(tmp).read_bytes()
            if save_path:
                Path(save_path).parent.mkdir(parents=True, exist_ok=True)
                Path(save_path).write_bytes(data)
            saved_note = f"\nSaved to: {save_path}" if save_path else ""
            return (
                f"Windows screenshot OK — provider=helper, {len(data)//1024}KB\n"
                f"Metadata: {out or '{}'}{saved_note}\n"
                f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"
            )
        finally:
            Path(tmp).unlink(missing_ok=True)

    return f"Error: unknown helper action '{action}'"
