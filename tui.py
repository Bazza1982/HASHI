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
  /log           Pause/resume log scrolling
  /quit          Graceful shutdown
  All other /commands are forwarded to the active agent.
"""
import sys
import os
import json

# Ensure the project root is on sys.path so `tui.*` and `orchestrator.*` imports work
_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from tui.app import HASHITuiApp


def _get_workbench_url() -> str:
    """Read workbench_port from agents.json so TUI always connects to THIS instance."""
    try:
        config_path = os.path.join(_project_root, "agents.json")
        with open(config_path) as f:
            config = json.load(f)
        port = config.get("global", {}).get("workbench_port", 18800)
        return f"http://localhost:{port}"
    except Exception:
        return "http://localhost:18800"


def main():
    app = HASHITuiApp(workbench_url=_get_workbench_url())
    app.run()


if __name__ == "__main__":
    main()
