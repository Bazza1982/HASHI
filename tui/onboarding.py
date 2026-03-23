"""Embedded first-run onboarding — runs inside the TUI before main.py starts."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def load_languages(bridge_home: Path) -> list[dict]:
    lang_dir = bridge_home / "onboarding" / "languages"
    order = [
        "english.json", "japanese.json", "chinese_sim.json", "chinese_trad.json",
        "korean.json", "german.json", "french.json", "russian.json", "arabic.json",
    ]
    files = sorted(lang_dir.glob("*.json"), key=lambda x: order.index(x.name) if x.name in order else 99)
    langs = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["_file"] = f.name
            langs.append(data)
        except Exception:
            pass
    return langs


def lang_code_from_file(filename: str) -> str:
    mapping = {
        "chinese_sim": "zh", "chinese_trad": "zh-tw", "japanese": "ja",
        "korean": "ko", "german": "de", "french": "fr", "russian": "ru", "arabic": "ar",
    }
    for key, code in mapping.items():
        if key in filename.lower():
            return code
    return "en"


def audit_environment() -> tuple[str | None, str | None]:
    checks = [
        ("Claude Code", "claude-cli", ["claude", "-v"]),
        ("Gemini CLI", "gemini-cli", ["gemini", "--version"]),
        ("Codex CLI", "codex-cli", ["codex", "--version"]),
    ]
    for name, engine, cmd in checks:
        try:
            if shutil.which(cmd[0]):
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if res.returncode == 0:
                    return name, engine
        except Exception:
            pass
    return None, None


def verify_openrouter(key: str) -> bool:
    import http.client
    try:
        conn = http.client.HTTPSConnection("openrouter.ai")
        conn.request("GET", "/api/v1/models", headers={"Authorization": f"Bearer {key}"})
        return conn.getresponse().status == 200
    except Exception:
        return False


def write_config(bridge_home: Path, engine: str, lang: dict, l_code: str, or_key: str | None = None):
    """Create agents.json, secrets.json, and workspace for the onboarding agent."""
    workspace_dir = bridge_home / "workspaces" / "onboarding_agent"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # Copy initial docs
    for fname in ("initial.md", "AGENT_FYI.md"):
        src = bridge_home / "docs" / fname
        if src.exists():
            shutil.copy(src, workspace_dir / fname)

    # Prime conversation log with welcome prompt
    log_path = workspace_dir / "conversation_log.jsonl"
    welcome = lang.get("welcomePrompt", "Hello! I am ready to assist you.")
    if log_path.exists():
        log_path.unlink()
    entry = {"timestamp": datetime.now().isoformat(), "role": "user", "source": "system", "text": welcome}
    log_path.write_text(json.dumps(entry, ensure_ascii=True) + "\n", encoding="utf-8")

    # Wakeup trigger
    (workspace_dir / "WAKEUP.prompt").write_text(welcome, encoding="utf-8")

    # Display name by language
    display_names = {
        "zh": "\u5c0f\u4e54", "zh-tw": "\u5c0f\u55ac", "ja": "\u30cf\u30b7\u30b3",
        "ko": "\ud558\uc2dc\ucf54", "ru": "\u0425\u0430\u0448\u0438\u043a\u043e",
        "ar": "\u0647\u0627\u0634\u064a\u0643\u0648",
    }
    display = display_names.get(l_code, "Hashiko")

    default_models = {
        "gemini-cli": "gemini-3.1-pro-preview",
        "claude-cli": "claude-sonnet-4-6",
        "codex-cli": "gpt-5.4",
        "openrouter-api": "anthropic/claude-sonnet-4.6",
    }

    all_backends = [
        {"engine": "gemini-cli", "model": "gemini-3.1-pro-preview"},
        {"engine": "claude-cli", "model": "claude-sonnet-4-6"},
        {"engine": "codex-cli", "model": "gpt-5.4"},
        {"engine": "openrouter-api", "model": "anthropic/claude-sonnet-4.6"},
    ]

    agent_cfg = {
        "name": "hashiko",
        "display_name": display,
        "emoji": "\U0001f423",
        "type": "flex",
        "engine": engine,
        "system_md": "docs/initial.md",
        "workspace_dir": "workspaces/onboarding_agent",
        "is_active": True,
        "model": default_models.get(engine, ""),
        "allowed_backends": all_backends,
        "active_backend": engine,
        "telegram_token_key": "hashiko",
    }

    agents_json = {"global": {"authorized_id": 0, "whatsapp": {"enabled": False}}, "agents": [agent_cfg]}

    # Write agents.json
    agents_path = bridge_home / "agents.json"
    agents_path.write_text(json.dumps(agents_json, indent=2, ensure_ascii=False), encoding="utf-8")

    # Write secrets.json (merge if exists)
    secrets_path = bridge_home / "secrets.json"
    secrets = {}
    if secrets_path.exists():
        try:
            secrets = json.loads(secrets_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    secrets.setdefault("hashiko", "WORKBENCH_ONLY_NO_TOKEN")
    secrets.setdefault("authorized_telegram_id", 0)
    if or_key:
        secrets["openrouter_key"] = or_key
    secrets_path.write_text(json.dumps(secrets, indent=2), encoding="utf-8")
