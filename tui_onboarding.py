#!/usr/bin/env python3
"""HASHI TUI Onboarding — First-run setup + chat in one terminal window.

Usage:
    python tui_onboarding.py

Runs the full first-run onboarding flow (language, disclaimer, wellbeing, API key check)
then transitions seamlessly into the normal TUI chat experience with Hashiko.

If onboarding has already been completed, launches directly as a normal TUI.

The TUI is the rescue hatch, so this entry point must never die on a missing
package or an unreadable instance configuration. It therefore delegates to the
``tui.py`` entry point, which resolves this instance's verified identity and
falls through to the standard-library rescue shell instead of exiting.
"""
import importlib.util
import os
import sys
from pathlib import Path

_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def _entry_module():
    """Load ``tui.py`` by path.

    ``import tui`` resolves to the ``tui`` package, not to the entry script, so
    the shared launch/rescue logic is loaded explicitly by file path.
    """

    spec = importlib.util.spec_from_file_location(
        "hashi_tui_entry", Path(_project_root) / "tui.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover - damaged checkout
        raise RuntimeError(f"the TUI entry point is missing at {_project_root}/tui.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    return _entry_module()._launch(onboarding_mode=True)


if __name__ == "__main__":
    raise SystemExit(main())
