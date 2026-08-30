#!/usr/bin/env python3
"""HASHI TUI — Terminal-first interface for onboarding and daily use.

Usage:
    python tui.py

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
  /log           Pause/resume log scrolling
  /quit          Graceful shutdown
  All other /commands are forwarded to the active agent.
"""
import os
import sys
from pathlib import Path

# Ensure the project root is on sys.path so `tui.*` and `orchestrator.*` imports work
_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from tui.app import HASHITuiApp
from tui.instances import load_launch_instance, local_workbench_urls


def _get_workbench_urls() -> tuple[str, list[str], str]:
    """Resolve this repository's identity and local Workbench candidates."""
    instance_id, port = load_launch_instance(Path(_project_root))
    urls = local_workbench_urls(port)
    configured = str(os.environ.get("HASHI_WORKBENCH_URL") or "").strip().rstrip("/")
    if configured:
        urls = [configured, *(url for url in urls if url != configured)]
    return instance_id, urls, urls[0]


def _get_workbench_url() -> str:
    """Compatibility accessor for callers that only need the primary URL."""
    return _get_workbench_urls()[2]


def main():
    instance_id, workbench_urls, primary_url = _get_workbench_urls()
    app = HASHITuiApp(
        workbench_url=primary_url,
        workbench_urls=workbench_urls,
        bridge_home=Path(_project_root),
        launch_instance_id=instance_id,
    )
    app.run()


if __name__ == "__main__":
    main()
