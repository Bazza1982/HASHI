#!/usr/bin/env python3
"""HASHI TUI - Terminal-first interface for onboarding and daily use.

Usage:
    python tui.py
    python tui.py --rescue      # force the stdlib rescue shell

Wraps main.py as a subprocess. Split-panel terminal UI:
  - Upper panel: real-time logs from bridge
  - Lower panel: chat with agents via Workbench API

TUI-only commands:
  /to <name>     Switch active agent
  /to all        Broadcast to all agents
  /agents        List available agents
  /instance      List trusted local/LAN HASHI instances
  /instance <id> Switch through authenticated Hashi Remote
  /instance current  Return to the launch instance
  /sidepanel     Open the read-only information panel
  /sidepanel off Close the information panel
  /doctor        Local environment, port, log and dependency checks
  /logs          Tail of the newest launcher / bridge logs
  /log           Pause/resume log scrolling
  /quit          Graceful shutdown
  All other /commands are forwarded to the active agent.

The TUI is the rescue hatch for a broken installation, so this entry point is
fail-open: a missing TUI package, an unreadable instance configuration or a
crashing app never leaves the operator with a closed window and no diagnosis.
It falls through to the standard-library rescue shell instead.
"""
import argparse
import os
import sys
import traceback
from pathlib import Path

# Ensure the project root is on sys.path so `tui.*` and `orchestrator.*` imports work
_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def _get_bridge_home() -> Path:
    configured = str(os.environ.get("BRIDGE_HOME") or "").strip()
    return Path(configured).resolve() if configured else Path(_project_root)


def _instances():
    """Import the (dependency-light) instance helpers on demand."""

    from tui import instances

    return instances


def _get_workbench_urls() -> tuple[str, list[str], str]:
    """Resolve this repository's identity and local Workbench candidates."""
    bridge_home = _get_bridge_home()
    helpers = _instances()
    endpoint_setting = str(os.environ.get("HASHI_LOCAL_ENDPOINT_FILE") or "").strip()
    if endpoint_setting:
        instance_id, port = helpers.load_local_endpoint(bridge_home, Path(endpoint_setting))
        endpoint_url = helpers.local_workbench_urls(port)[0]
        configured = str(os.environ.get("HASHI_WORKBENCH_URL") or "").strip().rstrip("/")
        if configured and configured != endpoint_url:
            raise RuntimeError(
                "HASHI_WORKBENCH_URL does not match the verified local endpoint"
            )
        return instance_id, [endpoint_url], endpoint_url

    instance_id, port = helpers.load_launch_instance(bridge_home)
    urls = helpers.local_workbench_urls(port)
    configured = str(os.environ.get("HASHI_WORKBENCH_URL") or "").strip().rstrip("/")
    if configured:
        urls = [configured, *(url for url in urls if url != configured)]
    return instance_id, urls, urls[0]


def _get_workbench_url() -> str:
    """Compatibility accessor for callers that only need the primary URL."""
    return _get_workbench_urls()[2]


def _default_urls() -> tuple[str, list[str], str]:
    """Last-resort local URLs when the instance configuration is unreadable."""

    from orchestrator.runtime_defaults import (
        DEFAULT_WORKBENCH_LOCALHOST_URL,
        DEFAULT_WORKBENCH_URL,
    )

    urls = [DEFAULT_WORKBENCH_LOCALHOST_URL, DEFAULT_WORKBENCH_URL]
    return "HASHI", urls, urls[0]


def _rescue(reason: str) -> int:
    """Enter the standard-library rescue shell instead of exiting."""

    try:
        from tui.rescue_cli import run as run_rescue
    except Exception:  # pragma: no cover - only if the checkout is damaged
        print("TUI could not start: " + reason, file=sys.stderr)
        print("The rescue shell is unavailable too; the installation is incomplete.", file=sys.stderr)
        return 78
    return run_rescue(Path(_project_root), _get_bridge_home(), reason)


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=True, description="HASHI terminal UI")
    parser.add_argument(
        "--rescue",
        action="store_true",
        help="start the standard-library rescue shell without the full TUI",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    if args.rescue:
        return _rescue("requested with --rescue")

    try:
        from tui.app import HASHITuiApp
    except Exception as exc:
        return _rescue(
            f"the TUI packages or modules are unavailable ({type(exc).__name__}: {exc})"
        )

    try:
        instance_id, workbench_urls, primary_url = _get_workbench_urls()
    except Exception as exc:
        instance_id, workbench_urls, primary_url = _default_urls()
        print(
            "Notice: the instance configuration could not be read "
            f"({type(exc).__name__}: {exc}).",
            file=sys.stderr,
        )
        print(
            f"        The TUI starts anyway on {primary_url}; run /doctor inside it.",
            file=sys.stderr,
        )

    try:
        app = HASHITuiApp(
            workbench_url=primary_url,
            workbench_urls=workbench_urls,
            bridge_home=_get_bridge_home(),
            code_root=Path(_project_root),
            launch_instance_id=instance_id,
        )
        app.run()
    except Exception as exc:
        traceback.print_exc()
        return _rescue(f"the TUI stopped unexpectedly ({type(exc).__name__}: {exc})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# HASHI-Fingerprint: hfp_035db0e31d074026802af5ce88bb48c4
