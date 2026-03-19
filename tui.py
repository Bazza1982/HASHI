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

# Ensure the project root is on sys.path so `tui.*` and `orchestrator.*` imports work
_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from tui.app import HASHITuiApp


def main():
    app = HASHITuiApp()
    app.run()


if __name__ == "__main__":
    main()
