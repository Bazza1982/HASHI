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
        content = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        if private:
            from tools.private_files import protect_private_file
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                protect_private_file(temporary)
                with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                    descriptor = -1
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        else:
            temporary.write_text(content, encoding='utf-8')
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
