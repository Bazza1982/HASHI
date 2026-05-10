#!/usr/bin/env python3
"""Deploy a Soul seed into a HASHI flex-agent workspace."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


NO_TOKEN = "WORKBENCH_ONLY_NO_TOKEN"
DEFAULT_BACKENDS = [
    {"engine": "gemini-cli", "model": "gemini-3.1-pro-preview"},
    {"engine": "claude-cli", "model": "claude-sonnet-4-6"},
    {"engine": "codex-cli", "model": "gpt-5.4"},
    {"engine": "openrouter-api", "model": "anthropic/claude_sonnet-4.6"},
    {"engine": "deepseek-api", "model": "deepseek-chat"},
]


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def normalize_agent_id(value: str) -> str:
    agent_id = re.sub(r"[^a-zA-Z0-9_ -]+", "", value.strip()).lower()
    agent_id = re.sub(r"[\s-]+", "_", agent_id).strip("_")
    if not agent_id:
        raise ValueError("agent_id is empty after normalization")
    if not re.match(r"^[a-z][a-z0-9_]*$", agent_id):
        raise ValueError(
            f"invalid agent_id {agent_id!r}; use lowercase letters, numbers, and underscores, starting with a letter"
        )
    return agent_id


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc


def write_json(path: Path, data: dict[str, Any], dry_run: bool) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if dry_run:
        print(f"[dry-run] would write {path}")
        return
    path.write_text(text, encoding="utf-8")


def resolve_seed(seed_dir: Path, seed: str) -> Path:
    raw = Path(seed)
    candidates = []
    if raw.suffix:
        candidates.append(raw if raw.is_absolute() else seed_dir / raw)
    else:
        candidates.extend(
            [
                seed_dir / f"{seed}.md",
                seed_dir / f"{seed.lower()}.md",
                seed_dir / f"{normalize_agent_id(seed)}.md",
            ]
        )

    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate

    available = ", ".join(sorted(path.stem for path in seed_dir.glob("*.md"))) or "none"
    raise SystemExit(f"Seed not found: {seed!r}. Available seeds: {available}")


def sample_allowed_backends(sample_path: Path) -> list[dict[str, Any]]:
    sample = read_json(sample_path)
    for agent in sample.get("agents", []):
        if agent.get("type") == "flex" and agent.get("allowed_backends"):
            return agent["allowed_backends"]
    return DEFAULT_BACKENDS


def build_agent_entry(
    *,
    agent_id: str,
    display_name: str,
    emoji: str,
    engine: str,
    model: str,
    active: bool,
    token_key: str,
    allowed_backends: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "name": agent_id,
        "display_name": display_name,
        "emoji": emoji,
        "type": "flex",
        "engine": engine,
        "system_md": f"workspaces/{agent_id}/AGENT.md",
        "workspace_dir": f"workspaces/{agent_id}",
        "telegram_token_key": token_key,
        "is_active": active,
        "model": model,
        "allowed_backends": allowed_backends,
        "active_backend": engine,
    }


def upsert_agent(
    agents_config: dict[str, Any],
    entry: dict[str, Any],
    *,
    overwrite_agent: bool,
) -> str:
    agents = agents_config.setdefault("agents", [])
    for index, agent in enumerate(agents):
        if agent.get("name") == entry["name"]:
            if not overwrite_agent:
                raise SystemExit(
                    f"Agent {entry['name']!r} already exists in agents.json. "
                    "Use --overwrite-agent to replace its config entry."
                )
            agents[index] = entry
            return "updated"
    agents.append(entry)
    return "added"


def update_secrets(
    secrets_config: dict[str, Any],
    *,
    token_key: str,
    token_value: str,
    overwrite_secret: bool,
) -> str:
    if token_key in secrets_config and not overwrite_secret:
        return "preserved"
    secrets_config[token_key] = token_value
    return "updated" if overwrite_secret else "added"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deploy a Soul seed from agent_seeds/ into workspaces/<agent_id>/AGENT.md and agent config."
    )
    parser.add_argument("seed", help="Seed name or seed markdown filename, e.g. zelda or zelda.md")
    parser.add_argument("agent_id", nargs="?", help="Agent id. Defaults to the seed filename stem.")
    parser.add_argument("--display-name", help="Display name shown in the agent list")
    parser.add_argument("--emoji", default="✨", help="Agent emoji, default: ✨")
    parser.add_argument("--engine", default="claude-cli", help="Default active backend engine")
    parser.add_argument("--model", default="claude-sonnet-4-6", help="Default model for the active backend")
    parser.add_argument("--active", action="store_true", help="Create the agent as active")
    parser.add_argument("--token-key", help="secrets.json key for the Telegram token. Defaults to agent_id.")
    parser.add_argument("--telegram-token", default=NO_TOKEN, help=f"Telegram token value, default: {NO_TOKEN}")
    parser.add_argument("--no-secrets", action="store_true", help="Do not create/update secrets.json")
    parser.add_argument("--overwrite-agent", action="store_true", help="Replace an existing agents.json entry")
    parser.add_argument("--overwrite-agent-md", action="store_true", help="Replace existing workspace AGENT.md")
    parser.add_argument("--overwrite-secret", action="store_true", help="Replace existing token key in secrets.json")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print planned changes without writing")
    parser.add_argument("--agents", default="agents.json", help="Path to agents config")
    parser.add_argument("--secrets", default="secrets.json", help="Path to secrets config")
    parser.add_argument("--sample", default="agents.json.sample", help="Path to sample agents config")
    parser.add_argument("--seed-dir", default="agent_seeds", help="Path to seed directory")
    parser.add_argument("--workspaces-dir", default="workspaces", help="Path to workspaces directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = repo_root()

    seed_dir = (root / args.seed_dir).resolve()
    workspaces_dir = (root / args.workspaces_dir).resolve()
    agents_path = (root / args.agents).resolve()
    secrets_path = (root / args.secrets).resolve()
    sample_path = (root / args.sample).resolve()

    seed_path = resolve_seed(seed_dir, args.seed)
    agent_id = normalize_agent_id(args.agent_id or seed_path.stem)
    display_name = args.display_name or agent_id.replace("_", " ").title()
    token_key = args.token_key or agent_id
    workspace_dir = workspaces_dir / agent_id
    agent_md = workspace_dir / "AGENT.md"

    if agent_md.exists() and not args.overwrite_agent_md:
        raise SystemExit(f"{agent_md} already exists. Use --overwrite-agent-md to replace it.")

    agents_config = read_json(agents_path)
    secrets_config = read_json(secrets_path)
    allowed_backends = sample_allowed_backends(sample_path)
    entry = build_agent_entry(
        agent_id=agent_id,
        display_name=display_name,
        emoji=args.emoji,
        engine=args.engine,
        model=args.model,
        active=args.active,
        token_key=token_key,
        allowed_backends=allowed_backends,
    )

    agent_action = upsert_agent(agents_config, entry, overwrite_agent=args.overwrite_agent)
    secret_action = "skipped"
    if not args.no_secrets:
        secret_action = update_secrets(
            secrets_config,
            token_key=token_key,
            token_value=args.telegram_token,
            overwrite_secret=args.overwrite_secret,
        )

    seed_content = seed_path.read_text(encoding="utf-8")
    if args.dry_run:
        print(f"[dry-run] seed: {seed_path.relative_to(root)}")
        print(f"[dry-run] workspace: {workspace_dir.relative_to(root)}")
        print(f"[dry-run] AGENT.md: {agent_md.relative_to(root)}")
        print(f"[dry-run] agents.json entry: {agent_action}")
        print(f"[dry-run] secrets.json token key: {token_key} ({secret_action})")
        print(json.dumps(entry, ensure_ascii=False, indent=2))
        return 0

    workspace_dir.mkdir(parents=True, exist_ok=True)
    agent_md.write_text(seed_content, encoding="utf-8")
    write_json(agents_path, agents_config, dry_run=False)
    if not args.no_secrets:
        write_json(secrets_path, secrets_config, dry_run=False)

    print(f"Deployed Soul seed {seed_path.name} as agent {agent_id!r}.")
    print(f"- wrote {agent_md.relative_to(root)}")
    print(f"- {agent_action} agents.json entry")
    print(f"- {secret_action} secrets.json token key {token_key!r}")
    print("Restart HASHI to load the new agent, e.g. /reboot or /reboot max.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
