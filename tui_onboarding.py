#!/usr/bin/env python3
"""HASHI TUI Onboarding — First-run setup + chat in one terminal window.

Usage:
    python tui_onboarding.py

Runs the full first-run onboarding flow (language, disclaimer, wellbeing, API key check)
then transitions seamlessly into the normal TUI chat experience with Hashiko.

If onboarding has already been completed, launches directly as a normal TUI.
"""
import sys
import os
import json

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from tui.app import HASHITuiApp


def _get_workbench_url() -> str:
    try:
        config_path = os.path.join(_project_root, "agents.json")
        with open(config_path) as f:
            config = json.load(f)
        port = config.get("global", {}).get("workbench_port", 18800)
        return f"http://localhost:{port}"
    except Exception:
        return "http://localhost:18800"


def main():
    app = HASHITuiApp(workbench_url=_get_workbench_url(), onboarding_mode=True)
    app.run()


if __name__ == "__main__":
    main()
