"""Compatibility onboarding entry and its atomic instance persistence boundary."""
from __future__ import annotations
import json
import os
from pathlib import Path
from tools.terminal_environment import has_interactive_input
from uuid import uuid4

def _atomic_write_json(path: Path, payload: dict, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        if private:
            from tools.private_files import protect_private_file
            protect_private_file(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run_onboarding():
    import sys
    if not has_interactive_input():
        print("ONBOARDING_REQUIRED: Open interactive hashi onboard.", file=sys.stderr)
        return 78
    from tui.connection import main
    return main()


if __name__ == "__main__":
    raise SystemExit(run_onboarding())
