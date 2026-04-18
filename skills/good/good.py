#!/usr/bin/env python3
"""Record a /good signal for the current agent and immediately process into habits."""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("BRIDGE_PROJECT_ROOT", Path(__file__).parent.parent.parent))
WORKSPACE_DIR = Path(os.environ.get("BRIDGE_WORKSPACE_DIR", PROJECT_ROOT / "workspaces" / "lily"))
AGENT_NAME = WORKSPACE_DIR.name
TRANSCRIPT_PATH = WORKSPACE_DIR / "transcript.jsonl"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from orchestrator.habits import HabitStore


def _read_transcript_context() -> str:
    if not TRANSCRIPT_PATH.exists():
        return ""
    lines: list[str] = []
    try:
        for line in TRANSCRIPT_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            source = entry.get("source", "")
            if source in ("startup", "handoff", "fyi", "system"):
                continue
            role = entry.get("role", "")
            text = entry.get("text", "")
            ts = entry.get("ts", "")
            ts_tag = f" @ {ts}" if ts else ""
            if source == "think":
                prefix = f"[THINKING{ts_tag}]"
            elif role == "user":
                prefix = f"[USER{ts_tag}]"
            else:
                prefix = f"[AGENT{ts_tag}]"
            lines.append(f"{prefix} {text}")
    except Exception:
        pass
    return "\n".join(lines)

def main() -> int:
    comment = " ".join(sys.argv[1:]).strip() or None
    context = _read_transcript_context()
    store = HabitStore(workspace_dir=WORKSPACE_DIR, project_root=PROJECT_ROOT, agent_id=AGENT_NAME, agent_class=None)
    signal_id = store.record_user_signal(signal="good", comment=comment, context=context)
    comment_note = f' — "{comment}"' if comment else ""
    words = len(context.split()) if context else 0

    output = [
        f"/good recorded (id={signal_id}){comment_note}.",
        f"Context captured: {words} words from transcript.",
        "Instant external processing is disabled for safety.",
        "Will be processed later by dream using the agent's current approved backend only.",
    ]
    print("\n".join(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
