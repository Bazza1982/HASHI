from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from orchestrator.pathing import BridgePaths


def run_onboarding_gate(paths: BridgePaths, code_root: Path) -> bool:
    """Return True when onboarding was launched and the caller should exit."""
    agents_path = paths.bridge_home / "agents.json"
    onboarding_done = False
    try:
        # Keep this gate aligned with ConfigManager: existing Windows-managed
        # configs may contain a UTF-8 BOM and are still valid HASHI configs.
        with agents_path.open(encoding="utf-8-sig") as fh:
            cfg = json.load(fh)
            if cfg.get("agents"):
                onboarding_done = True
    except Exception:
        pass

    if onboarding_done:
        return False

    print("\033[38;5;180mOnboarding required. Starting onboarding program...\033[0m")
    subprocess.run([sys.executable, str(code_root / "onboarding" / "onboarding_main.py")])
    return True
