from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from orchestrator.pathing import BridgePaths


def needs_onboarding(paths: BridgePaths) -> bool:
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

    return not onboarding_done


def run_onboarding_gate(paths: BridgePaths, code_root: Path, *, terminal: str | None = None) -> bool:
    """First-run Function flow; never launch this during a replacement probe."""
    if not needs_onboarding(paths):
        return False
    print("\033[38;5;180mOnboarding required. Starting onboarding program...\033[0m")
    command = [sys.executable, str(code_root / "onboarding" / "onboarding_main.py")]
    if terminal is not None:
        # Multiprocessing closes stdin. Reopen only the terminal supplied by
        # the supervising process, never the RPC pipe or a guessed device.
        with open(terminal, "r", encoding="utf-8") as input_stream:
            subprocess.run(command, stdin=input_stream, check=True)
    elif sys.stdin.isatty():
        subprocess.run(command, check=True)
    else:
        raise RuntimeError("First-run setup requires an interactive terminal; run onboarding/onboarding_main.py")
    return True
