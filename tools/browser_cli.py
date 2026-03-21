#!/usr/bin/env python3
"""
browser_cli.py — Command-line wrapper for HASHI browser tools.

Allows any agent with bash access (Claude CLI, Gemini CLI, Codex CLI, etc.)
to use browser capabilities without needing the OpenRouter tool framework.

Usage:
  python tools/browser_cli.py screenshot --url <url> [options]
  python tools/browser_cli.py get_text   --url <url> [options]
  python tools/browser_cli.py get_html   --url <url> [options]
  python tools/browser_cli.py click      --url <url> --selector <css> [options]
  python tools/browser_cli.py fill       --url <url> --selector <css> --text <text> [options]
  python tools/browser_cli.py evaluate   --url <url> --script <js> [options]

Common options:
  --cdp-url   http://localhost:9222   Attach to user's running Chrome (reuses cookies/session)
  --headed                            Launch visible browser window (standalone mode only)
  --wait-ms   1500                    Extra wait after page load (ms)
  --full-page                         Full-page screenshot
  --max-length 15000                  Max text/html characters returned
  --submit                            Press Enter after fill
  --out <file>                        Write output to file (useful for screenshots)

Output:
  - Text/HTML commands: plain text to stdout
  - screenshot: base64 PNG to stdout (or raw PNG bytes if --out <file>)
  - Errors: "Error: ..." to stdout, exit code 1
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import sys
from pathlib import Path

# Allow running from any directory
sys.path.insert(0, str(Path(__file__).parent.parent))


async def _run(args: argparse.Namespace) -> int:
    from tools.browser import (
        execute_browser_screenshot,
        execute_browser_get_text,
        execute_browser_get_html,
        execute_browser_click,
        execute_browser_fill,
        execute_browser_evaluate,
    )

    kwargs: dict = {"url": args.url}
    if args.cdp_url:
        kwargs["cdp_url"] = args.cdp_url
    if args.headed:
        kwargs["headed"] = True

    cmd = args.command

    if cmd == "screenshot":
        kwargs["wait_ms"] = args.wait_ms
        kwargs["full_page"] = args.full_page
        result = await execute_browser_screenshot(kwargs)

        if result.startswith("Error:"):
            print(result)
            return 1

        # result is "screenshot:<base64>"
        b64 = result[len("screenshot:"):]

        if args.out:
            out_path = Path(args.out)
            out_path.write_bytes(base64.b64decode(b64))
            print(f"OK: screenshot saved to {out_path}")
        else:
            # Print base64 so the agent can read it
            print(b64)
        return 0

    elif cmd == "get_text":
        kwargs["wait_ms"] = args.wait_ms
        kwargs["max_length"] = args.max_length
        result = await execute_browser_get_text(kwargs)

    elif cmd == "get_html":
        kwargs["wait_ms"] = args.wait_ms
        kwargs["max_length"] = args.max_length
        result = await execute_browser_get_html(kwargs)

    elif cmd == "click":
        if not args.selector:
            print("Error: --selector is required for click")
            return 1
        kwargs["selector"] = args.selector
        result = await execute_browser_click(kwargs)

    elif cmd == "fill":
        if not args.selector:
            print("Error: --selector is required for fill")
            return 1
        kwargs["selector"] = args.selector
        kwargs["text"] = args.text or ""
        kwargs["submit"] = args.submit
        result = await execute_browser_fill(kwargs)

    elif cmd == "evaluate":
        if not args.script:
            print("Error: --script is required for evaluate")
            return 1
        kwargs["script"] = args.script
        kwargs["wait_ms"] = args.wait_ms
        result = await execute_browser_evaluate(kwargs)

    else:
        print(f"Error: unknown command '{cmd}'")
        return 1

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
        choices=["screenshot", "get_text", "get_html", "click", "fill", "evaluate"],
        help="Browser action to perform",
    )
    parser.add_argument("--url", required=True, help="URL to navigate to")
    parser.add_argument(
        "--cdp-url",
        dest="cdp_url",
        default=None,
        help="CDP endpoint to attach to existing Chrome (e.g. http://localhost:9222)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        default=False,
        help="Launch a visible browser window (standalone mode only)",
    )
    parser.add_argument("--selector", default=None, help="CSS selector (click / fill)")
    parser.add_argument("--text", default=None, help="Text to fill (fill command)")
    parser.add_argument("--script", default=None, help="JS to evaluate (evaluate command)")
    parser.add_argument("--submit", action="store_true", default=False, help="Press Enter after fill")
    parser.add_argument("--wait-ms", dest="wait_ms", type=int, default=1500, help="Wait ms after load")
    parser.add_argument("--full-page", dest="full_page", action="store_true", default=False)
    parser.add_argument("--max-length", dest="max_length", type=int, default=15000)
    parser.add_argument("--out", default=None, help="Write output to this file path")

    args = parser.parse_args()
    exit_code = asyncio.run(_run(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
