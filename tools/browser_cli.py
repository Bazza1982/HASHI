#!/usr/bin/env python3
"""
browser_cli.py — Command-line wrapper for HASHI browser tools.

Allows any agent with bash access (Claude CLI, Gemini CLI, Codex CLI, etc.)
to use browser capabilities without needing the OpenRouter tool framework.

Usage:
  python tools/browser_cli.py screenshot    --url <url> [--out file.png]
  python tools/browser_cli.py get_text      --url <url>
  python tools/browser_cli.py get_html      --url <url>
  python tools/browser_cli.py click         --url <url> --selector <css>
  python tools/browser_cli.py fill          --url <url> --selector <css> --text <text> [--submit]
  python tools/browser_cli.py evaluate      --url <url> --script <js>
  python tools/browser_cli.py scroll        --url <url> [--x 0] [--y 500] [--selector <css>]
  python tools/browser_cli.py hover         --url <url> --selector <css>
  python tools/browser_cli.py key           --url <url> --key <key> [--selector <css>]
  python tools/browser_cli.py select        --url <url> --selector <css> [--value|--label|--index]
  python tools/browser_cli.py wait_for      --url <url> --selector <css> [--timeout-ms 10000]
  python tools/browser_cli.py get_attribute --url <url> --selector <css> --attribute <attr>
  python tools/browser_cli.py drag          --url <url> --source <css> --target <css>
  python tools/browser_cli.py upload        --url <url> --selector <css> --file-path <path>
  python tools/browser_cli.py session       --url <url> --steps '<json array>'

Common options:
  --cdp-url   http://localhost:9222   Attach to user's running Chrome (reuses cookies/session)
  --headed                            Launch visible browser window (standalone mode only)
  --out <file>                        Write output to file

Output:
  - Text/HTML/attribute commands: plain text to stdout
  - screenshot: base64 to stdout, or raw PNG if --out <file>
  - session: step-by-step log; any [screenshot] steps emit base64 inline
  - Errors: "Error: ..." to stdout, exit code 1
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def _run(args: argparse.Namespace) -> int:
    from tools.browser import (
        execute_browser_screenshot,
        execute_browser_get_text,
        execute_browser_get_html,
        execute_browser_click,
        execute_browser_fill,
        execute_browser_evaluate,
        execute_browser_scroll,
        execute_browser_hover,
        execute_browser_key,
        execute_browser_select,
        execute_browser_wait_for,
        execute_browser_get_attribute,
        execute_browser_drag,
        execute_browser_upload,
        execute_browser_session,
    )

    # Build base kwargs shared by most commands
    base: dict = {"url": args.url}
    if getattr(args, "cdp_url", None):
        base["cdp_url"] = args.cdp_url
    if getattr(args, "headed", False):
        base["headed"] = True

    cmd = args.command
    result = ""

    # ------------------------------------------------------------------ screenshot
    if cmd == "screenshot":
        kwargs = {**base, "wait_ms": args.wait_ms, "full_page": args.full_page}
        result = await execute_browser_screenshot(kwargs)
        if result.startswith("Error:"):
            print(result); return 1
        b64 = result[len("screenshot:"):]
        if args.out:
            Path(args.out).write_bytes(base64.b64decode(b64))
            print(f"OK: screenshot saved to {args.out}")
        else:
            print(b64)
        return 0

    # ------------------------------------------------------------------ get_text
    elif cmd == "get_text":
        result = await execute_browser_get_text(
            {**base, "wait_ms": args.wait_ms, "max_length": args.max_length}
        )

    # ------------------------------------------------------------------ get_html
    elif cmd == "get_html":
        result = await execute_browser_get_html(
            {**base, "wait_ms": args.wait_ms, "max_length": args.max_length}
        )

    # ------------------------------------------------------------------ click
    elif cmd == "click":
        if not args.selector:
            print("Error: --selector is required for click"); return 1
        result = await execute_browser_click({**base, "selector": args.selector})

    # ------------------------------------------------------------------ fill
    elif cmd == "fill":
        if not args.selector:
            print("Error: --selector is required for fill"); return 1
        result = await execute_browser_fill(
            {**base, "selector": args.selector, "text": args.text or "", "submit": args.submit}
        )

    # ------------------------------------------------------------------ evaluate
    elif cmd == "evaluate":
        if not args.script:
            print("Error: --script is required for evaluate"); return 1
        result = await execute_browser_evaluate(
            {**base, "script": args.script, "wait_ms": args.wait_ms}
        )

    # ------------------------------------------------------------------ scroll
    elif cmd == "scroll":
        kwargs = {**base, "x": args.x, "y": args.y}
        if args.selector:
            kwargs["selector"] = args.selector
        result = await execute_browser_scroll(kwargs)

    # ------------------------------------------------------------------ hover
    elif cmd == "hover":
        if not args.selector:
            print("Error: --selector is required for hover"); return 1
        result = await execute_browser_hover({**base, "selector": args.selector})

    # ------------------------------------------------------------------ key
    elif cmd == "key":
        if not args.key:
            print("Error: --key is required"); return 1
        kwargs = {**base, "key": args.key}
        if args.selector:
            kwargs["selector"] = args.selector
        result = await execute_browser_key(kwargs)

    # ------------------------------------------------------------------ select
    elif cmd == "select":
        if not args.selector:
            print("Error: --selector is required for select"); return 1
        kwargs = {**base, "selector": args.selector}
        if args.value is not None:
            kwargs["value"] = args.value
        elif args.label is not None:
            kwargs["label"] = args.label
        elif args.index is not None:
            kwargs["index"] = args.index
        else:
            print("Error: one of --value, --label, or --index is required"); return 1
        result = await execute_browser_select(kwargs)

    # ------------------------------------------------------------------ wait_for
    elif cmd == "wait_for":
        if not args.selector:
            print("Error: --selector is required for wait_for"); return 1
        result = await execute_browser_wait_for(
            {**base, "selector": args.selector, "timeout_ms": args.timeout_ms}
        )

    # ------------------------------------------------------------------ get_attribute
    elif cmd == "get_attribute":
        if not args.selector:
            print("Error: --selector is required"); return 1
        if not args.attribute:
            print("Error: --attribute is required"); return 1
        result = await execute_browser_get_attribute(
            {**base, "selector": args.selector, "attribute": args.attribute}
        )

    # ------------------------------------------------------------------ drag
    elif cmd == "drag":
        if not args.source or not args.target:
            print("Error: --source and --target are required for drag"); return 1
        result = await execute_browser_drag(
            {**base, "source": args.source, "target": args.target}
        )

    # ------------------------------------------------------------------ upload
    elif cmd == "upload":
        if not args.selector or not args.file_path:
            print("Error: --selector and --file-path are required for upload"); return 1
        result = await execute_browser_upload(
            {**base, "selector": args.selector, "file_path": args.file_path}
        )

    # ------------------------------------------------------------------ session
    elif cmd == "session":
        if not args.steps:
            print("Error: --steps (JSON array) is required for session"); return 1
        try:
            steps = json.loads(args.steps)
        except json.JSONDecodeError as e:
            print(f"Error: --steps must be valid JSON: {e}"); return 1
        kwargs = {"steps": steps}
        if args.url:
            kwargs["url"] = args.url
        if getattr(args, "cdp_url", None):
            kwargs["cdp_url"] = args.cdp_url
        if getattr(args, "headed", False):
            kwargs["headed"] = True
        result = await execute_browser_session(kwargs)

    else:
        print(f"Error: unknown command '{cmd}'"); return 1

    # ------------------------------------------------------------------ output
    if args.out and result and not result.startswith("Error:"):
        Path(args.out).write_text(result, encoding="utf-8")
        print(f"OK: output saved to {args.out}")
    else:
        print(result)

    return 1 if result.startswith("Error:") else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="HASHI browser tool — CLI wrapper for any agent backend",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command",
        choices=[
            "screenshot", "get_text", "get_html",
            "click", "fill", "evaluate",
            "scroll", "hover", "key", "select",
            "wait_for", "get_attribute", "drag", "upload",
            "session",
        ],
        help="Browser action to perform",
    )

    # Core
    parser.add_argument("--url", default="", help="URL to navigate to")
    parser.add_argument("--cdp-url", dest="cdp_url", default=None,
                        help="CDP endpoint to attach to existing Chrome (e.g. http://localhost:9222)")
    parser.add_argument("--headed", action="store_true", default=False,
                        help="Launch a visible browser window (standalone mode only)")
    parser.add_argument("--out", default=None, help="Write output to this file path")

    # Content controls
    parser.add_argument("--wait-ms", dest="wait_ms", type=int, default=1500)
    parser.add_argument("--full-page", dest="full_page", action="store_true", default=False)
    parser.add_argument("--max-length", dest="max_length", type=int, default=15000)

    # Element interaction
    parser.add_argument("--selector", default=None, help="CSS selector")
    parser.add_argument("--text", default=None, help="Text to fill")
    parser.add_argument("--submit", action="store_true", default=False, help="Press Enter after fill")
    parser.add_argument("--script", default=None, help="JS to evaluate")
    parser.add_argument("--key", default=None, help="Key to press (e.g. Enter, Tab, Control+a)")
    parser.add_argument("--attribute", default=None, help="HTML attribute name")

    # Scroll
    parser.add_argument("--x", type=int, default=0, help="Horizontal scroll pixels")
    parser.add_argument("--y", type=int, default=500, help="Vertical scroll pixels")

    # Select
    parser.add_argument("--value", default=None, help="<select> option value")
    parser.add_argument("--label", default=None, help="<select> option visible text")
    parser.add_argument("--index", type=int, default=None, help="<select> option index")

    # Wait
    parser.add_argument("--timeout-ms", dest="timeout_ms", type=int, default=10000)

    # Drag
    parser.add_argument("--source", default=None, help="CSS selector of drag source")
    parser.add_argument("--target", default=None, help="CSS selector of drop target")

    # Upload
    parser.add_argument("--file-path", dest="file_path", default=None, help="Local file path to upload")

    # Session
    parser.add_argument("--steps", default=None, help="JSON array of session steps")

    args = parser.parse_args()
    exit_code = asyncio.run(_run(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
