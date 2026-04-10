#!/usr/bin/env python3
"""Record a /bad signal for the current agent and immediately process into habits."""
from __future__ import annotations
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(os.environ.get("BRIDGE_PROJECT_ROOT", Path(__file__).parent.parent.parent))
WORKSPACE_DIR = Path(os.environ.get("BRIDGE_WORKSPACE_DIR", PROJECT_ROOT / "workspaces" / "lily"))
AGENT_NAME = WORKSPACE_DIR.name
TRANSCRIPT_PATH = WORKSPACE_DIR / "transcript.jsonl"
SECRETS_PATH = PROJECT_ROOT / "secrets.json"

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


def _get_openrouter_key() -> str | None:
    try:
        secrets = json.loads(SECRETS_PATH.read_text())
    except Exception:
        return None
    for key in (f"{AGENT_NAME}_openrouter_key", "openrouter-api_key", "openrouter_key"):
        if secrets.get(key):
            return secrets[key]
    return None


def _call_openrouter(api_key: str, messages: list[dict], model: str = "anthropic/claude-sonnet-4-5") -> str:
    """Call OpenRouter chat completions API. Returns text response."""
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": 2000,
        "temperature": 0.3,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        return result["choices"][0]["message"]["content"]
    except (urllib.error.URLError, KeyError, json.JSONDecodeError) as e:
        raise RuntimeError(f"OpenRouter API call failed: {e}") from e


def main() -> int:
    comment = " ".join(sys.argv[1:]).strip() or None
    context = _read_transcript_context()
    store = HabitStore(workspace_dir=WORKSPACE_DIR, project_root=PROJECT_ROOT, agent_id=AGENT_NAME, agent_class=None)
    signal_id = store.record_user_signal(signal="bad", comment=comment, context=context)
    comment_note = f' — "{comment}"' if comment else ""
    words = len(context.split()) if context else 0

    # Immediately process this signal into habits
    api_key = _get_openrouter_key()
    habit_lines: list[str] = []
    if api_key:
        try:
            habit_lines = store.process_user_signals(
                api_key=api_key,
                call_llm_fn=_call_openrouter,
                max_signals=1,
                max_habits_per_signal=2,
                max_context_words=6000,
            )
        except Exception as e:
            habit_lines = [f"⚠️ Instant processing failed ({e}), will retry during next dream."]

    output = [
        f"/bad recorded (id={signal_id}){comment_note}.",
        f"Context captured: {words} words from transcript.",
    ]
    if habit_lines:
        output.append("⚡ Habits formed instantly:")
        for hl in habit_lines:
            output.append(f"  • {hl}")
    elif not api_key:
        output.append("Will be processed into habits during next dream. 🌙")
    print("\n".join(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
