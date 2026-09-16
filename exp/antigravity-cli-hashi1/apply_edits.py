"""One-shot, anchor-checked edits for the antigravity-cli backend landing.

Each edit asserts its anchor occurs EXACTLY once before replacing it, so a
partial application cannot silently corrupt the tree. Run from any cwd:
    python3 apply_edits.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

EDITS: list[tuple[str, str, str]] = []

# 1. orchestrator/pathing.py: insert resolve_agy_executable before to_home_relative
EDITS.append((
    "orchestrator/pathing.py",
    "    return raw\n\n\ndef to_home_relative",
    '''    return raw


def resolve_agy_executable(value: str | None) -> str:
    """Resolve the ``agy`` command for the Antigravity CLI backend.

    Candidates, in order:

    1. an explicitly configured value (a filesystem path wins when it exists),
    2. ``%LOCALAPPDATA%\\agy\\bin\\agy.exe`` (official installer location),
    3. the bare command name ``agy`` on PATH.
    """

    raw = str(value or "").strip()
    candidates: list[str] = []
    if raw:
        candidates.append(raw)
    local_app_data = os.environ.get("LOCALAPPDATA") or ""
    if local_app_data:
        candidates.append(str(Path(local_app_data) / "agy" / "bin" / "agy.exe"))
    candidates.append("agy")
    for candidate in candidates:
        if not candidate:
            continue
        if ("/" in candidate or "\\" in candidate or Path(candidate).is_absolute()) and Path(
            candidate
        ).is_file():
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    return raw or "agy"


def to_home_relative''',
))

# 2. orchestrator/flexible_backend_registry.py: CLI_ENGINES member
EDITS.append((
    "orchestrator/flexible_backend_registry.py",
    '        "gemini-cli",\n        "claude-cli",',
    '        "gemini-cli",\n        "antigravity-cli",\n        "claude-cli",',
))

# 3. orchestrator/flexible_backend_registry.py: registry entry before claude-cli
EDITS.append((
    "orchestrator/flexible_backend_registry.py",
    '        "secret_keys": ["gemini-cli_key"],\n    },\n    "claude-cli": {',
    '''        "secret_keys": ["gemini-cli_key"],
    },
    "antigravity-cli": {
        "label": "antigravity",
        "gateway_enabled": True,
        "privacy_levels": [0, 1],
        # agy 1.2.3 `agy models` output captured 2026-09-16 (see
        # exp/antigravity-cli-hashi1/agy-models-2026-09-16.txt).
        "models": [
            "gemini-3.8-flash-high",
            "gemini-3.8-flash-medium",
            "gemini-3.8-flash-low",
            "gemini-3.7-flash-high",
            "gemini-3.7-flash-medium",
            "gemini-3.7-flash-low",
            "gemini-3.6-flash-high",
            "gemini-3.6-flash-medium",
            "gemini-3.6-flash-low",
            "gemini-3.1-pro-high",
            "gemini-3.1-pro-low",
            "claude-sonnet-4-6",
            "claude-opus-4-6-thinking",
            "gpt-oss-120b-medium",
        ],
        "default_model": "gemini-3.8-flash-high",
        "efforts": [],
        "default_effort": None,
        # agy authenticates through the system keyring; no secret keys.
        "secret_keys": [],
    },
    "claude-cli": {''',
))

# 4. orchestrator/config.py: field after codex_cmd
EDITS.append((
    "orchestrator/config.py",
    '    codex_cmd: str = "codex"\n    grok_cmd: str = "grok"',
    '    codex_cmd: str = "codex"\n    agy_cmd: str = "agy"\n    grok_cmd: str = "grok"',
))

# 5. orchestrator/config.py: loader entry after grok_cmd block
EDITS.append((
    "orchestrator/config.py",
    '''            grok_cmd=resolve_command_value(
                g_raw.get("grok_cmd", "grok"),
                config_dir=config_dir,
                bridge_home=bridge_home,
            ),
            gh_copilot_cmd=resolve_command_value(''',
    '''            grok_cmd=resolve_command_value(
                g_raw.get("grok_cmd", "grok"),
                config_dir=config_dir,
                bridge_home=bridge_home,
            ),
            agy_cmd=resolve_command_value(
                resolve_agy_executable(g_raw.get("agy_cmd")),
                config_dir=config_dir,
                bridge_home=bridge_home,
            ),
            gh_copilot_cmd=resolve_command_value(''',
))

# 6. orchestrator/api_gateway_preflight.py: mapping entry
EDITS.append((
    "orchestrator/api_gateway_preflight.py",
    '        "codex-cli": getattr(global_config, "codex_cmd", "codex"),\n        "grok-cli":',
    '        "codex-cli": getattr(global_config, "codex_cmd", "codex"),\n        "antigravity-cli": getattr(global_config, "agy_cmd", "agy"),\n        "grok-cli":',
))

# 7. orchestrator/backend_preflight.py: cli_map entry
EDITS.append((
    "orchestrator/backend_preflight.py",
    '            "codex-cli": global_cfg.codex_cmd,\n            "grok-cli": getattr(global_cfg, "grok_cmd", "grok"),',
    '''            "codex-cli": global_cfg.codex_cmd,
            "antigravity-cli": resolve_agy_executable(
                getattr(global_cfg, "agy_cmd", "agy")
            ),
            "grok-cli": getattr(global_cfg, "grok_cmd", "grok"),''',
))


def main() -> int:
    for rel, old, new in EDITS:
        path = ROOT / rel
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count != 1:
            print(f"FAIL {rel}: anchor count={count}")
            return 1
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        print(f"OK   {rel}")
    print("ALL EDITS APPLIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
