#!/usr/bin/env python3
"""
Universal OpenClaw -> bridge-u-f agent importer.

Usage:
    python import_openclaw.py --source <openclaw_dir> --target <bridge-u-f_dir>
    python import_openclaw.py --source <openclaw_dir> --target <bridge-u-f_dir> --agents sakura,ying
    python import_openclaw.py --source <openclaw_dir> --target <bridge-u-f_dir> --apply

Options:
    --source        Path to the OpenClaw installation directory (contains openclaw.json)
    --target        Path to the bridge-u-f project directory (contains agents.json)
    --agents        Comma-separated list of agent IDs to import (default: all)
    --apply         Actually write changes (default: dry-run)
    --default-type  Default agent type: flex or fixed (default: flex)
    --engine        For fixed agents: gemini-cli, claude-cli, codex-cli, openrouter-api
    --model         For fixed agents: model name to use

Imports:
    - Agent identity docs (IDENTITY.md, SOUL.md, USER.md, TOOLS.md -> agent.md)
    - Agent memories (MEMORY.md + memory/*.md state/journal files)
    - Telegram bot tokens
    - Cron jobs / heartbeats -> tasks.json
    - API credentials (openrouter key) -> secrets.json
    - Agent scripts and skills -> workspace copy
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

# Ensure console can handle unicode
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# --- Default backend roster for flex agents ---
DEFAULT_ALLOWED_BACKENDS = [
    {"engine": "gemini-cli", "model": "gemini-3.1-pro-preview"},
    {"engine": "claude-cli", "model": "claude-sonnet-4-6"},
    {"engine": "codex-cli", "model": "gpt-5.4", "effort": "medium"},
]
DEFAULT_ACTIVE_BACKEND = "gemini-cli"

# Identity doc files to merge into agent.md, in order
IDENTITY_FILES = ["IDENTITY.md", "SOUL.md", "USER.md", "TOOLS.md"]
# Memory doc file
MEMORY_FILE = "MEMORY.md"

# API credential mappings: OpenClaw provider -> bridge-u-f secrets key
CREDENTIAL_MAP = {
    "openrouter": "openrouter_key",
}


def load_json_relaxed(path: Path) -> dict:
    """Load JSON that may contain // comments (OpenClaw style)."""
    text = path.read_text(encoding="utf-8-sig")
    lines = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("//"):
            continue
        if "//" in line:
            in_string = False
            result = []
            i = 0
            while i < len(line):
                ch = line[i]
                if ch == '"' and (i == 0 or line[i - 1] != '\\'):
                    in_string = not in_string
                    result.append(ch)
                elif not in_string and line[i:i+2] == '//':
                    break
                else:
                    result.append(ch)
                i += 1
            line = ''.join(result)
        lines.append(line)
    return json.loads("\n".join(lines))


def load_openclaw_config(source: Path) -> tuple[dict, Path]:
    """Load the primary OpenClaw config, falling back to valid backups if needed."""
    candidates = [source / "openclaw.json"] + sorted(source.glob("openclaw.json.bak*"))
    errors: list[str] = []
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            return load_json_relaxed(candidate), candidate
        except Exception as exc:
            errors.append(f"{candidate.name}: {exc}")

    joined = "; ".join(errors) if errors else "no config files found"
    raise RuntimeError(f"Failed to load any OpenClaw config from {source}: {joined}")


def load_json_with_backups(path: Path) -> tuple[dict, Path]:
    """Load a JSON file, falling back to sibling .bak files when necessary."""
    candidates = [path] + sorted(path.parent.glob(f"{path.name}.bak*"))
    errors: list[str] = []
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            return json.loads(candidate.read_text(encoding="utf-8-sig")), candidate
        except Exception as exc:
            errors.append(f"{candidate.name}: {exc}")

    joined = "; ".join(errors) if errors else "no config files found"
    raise RuntimeError(f"Failed to load any JSON config for {path}: {joined}")


def discover_agents(oc: dict) -> list[dict]:
    """Extract agent list from openclaw.json."""
    return oc.get("agents", {}).get("list", [])


def discover_bindings(oc: dict) -> dict[str, str]:
    """Map agentId -> telegram accountId from bindings."""
    result = {}
    for b in oc.get("bindings", []):
        agent_id = b.get("agentId")
        account_id = b.get("match", {}).get("accountId")
        if agent_id and account_id:
            result[agent_id] = account_id
    return result


def discover_tokens(oc: dict) -> dict[str, str]:
    """Map telegram accountId -> botToken from channels config."""
    accounts = oc.get("channels", {}).get("telegram", {}).get("accounts", {})
    return {
        acct_id: acct.get("botToken")
        for acct_id, acct in accounts.items()
        if acct.get("botToken")
    }


def merge_identity_docs(agent_dir: Path) -> str | None:
    """Merge IDENTITY.md + SOUL.md + USER.md + TOOLS.md into one agent.md string."""
    parts = []
    for filename in IDENTITY_FILES:
        fpath = agent_dir / filename
        if fpath.is_file():
            content = fpath.read_text(encoding="utf-8", errors="replace").strip()
            if content:
                parts.append(content)
    if not parts:
        return None
    return "\n\n---\n\n".join(parts) + "\n"


def copy_memories(agent_dir: Path, target_workspace: Path, apply: bool) -> list[str]:
    """Copy MEMORY.md and the full memory/ tree into the target workspace."""
    actions = []

    # MEMORY.md -> workspace root
    mem_file = agent_dir / MEMORY_FILE
    if mem_file.is_file():
        dest = target_workspace / MEMORY_FILE
        actions.append(f"  Copy {mem_file.name} -> {dest}")
        if apply:
            shutil.copy2(str(mem_file), str(dest))

    # memory/** -> memory/ (preserve journals, nested folders, and state files)
    mem_dir = agent_dir / "memory"
    if mem_dir.is_dir():
        target_mem_dir = target_workspace / "memory"
        files = [f for f in sorted(mem_dir.rglob("*")) if f.is_file()]
        if files:
            actions.append(f"  Copy {len(files)} memory files -> {target_mem_dir}/")
            if apply:
                target_mem_dir.mkdir(parents=True, exist_ok=True)
                for f in files:
                    rel = f.relative_to(mem_dir)
                    dest = target_mem_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(f), str(dest))
    return actions


def copy_scripts(agent_dir: Path, target_workspace: Path, apply: bool) -> list[str]:
    """Copy agent scripts/ directory into target workspace."""
    actions = []
    scripts_dir = agent_dir / "scripts"
    if scripts_dir.is_dir():
        files = [f for f in scripts_dir.rglob("*") if f.is_file()]
        if files:
            target_scripts = target_workspace / "scripts"
            actions.append(f"  Copy {len(files)} script files -> {target_scripts}/")
            if apply:
                for f in files:
                    rel = f.relative_to(scripts_dir)
                    dest = target_scripts / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(f), str(dest))
    return actions


def copy_skills(agent_dir: Path, target_workspace: Path, apply: bool) -> list[str]:
    """Copy agent skills/ directory into target workspace."""
    actions = []
    skills_dir = agent_dir / "skills"
    if skills_dir.is_dir():
        entries = [d for d in skills_dir.iterdir() if d.is_dir()]
        if entries:
            target_skills = target_workspace / "skills"
            actions.append(f"  Copy {len(entries)} skill dirs -> {target_skills}/")
            if apply:
                for d in entries:
                    dest = target_skills / d.name
                    if dest.exists():
                        shutil.rmtree(str(dest))
                    shutil.copytree(str(d), str(dest))
    return actions


def copy_extra_dirs(agent_dir: Path, target_workspace: Path, apply: bool,
                    dir_names: list[str]) -> list[str]:
    """Copy additional named subdirectories (e.g. news_briefing/) into workspace."""
    actions = []
    for name in dir_names:
        src = agent_dir / name
        if src.is_dir():
            files = [f for f in src.rglob("*") if f.is_file()]
            if files:
                dest = target_workspace / name
                actions.append(f"  Copy dir {name}/ ({len(files)} files) -> {dest}/")
                if apply:
                    if dest.exists():
                        shutil.rmtree(str(dest))
                    shutil.copytree(str(src), str(dest))
    return actions


# --- Cron/Heartbeat conversion ---

def _parse_simple_daily_cron(expr: str) -> str | None:
    """
    Parse a simple daily cron expression like '0 7 * * *' -> '07:00'.
    Returns None for complex expressions (ranges, steps, weekday filters).
    """
    parts = expr.strip().split()
    if len(parts) != 5:
        return None
    minute, hour, dom, month, dow = parts
    # Must be simple: single minute, single hour, * * * (or * * 0-7)
    if dom != "*" or month != "*":
        return None
    if dow != "*":
        return None  # weekday filter, not simple daily
    if not minute.isdigit() or not hour.isdigit():
        return None  # ranges or steps
    return f"{int(hour):02d}:{int(minute):02d}"


def _cron_expr_to_interval_seconds(expr: str) -> int | None:
    """
    For step-based cron expressions like '0 6-22/2 * * *', estimate interval.
    Returns None if not convertible.
    """
    parts = expr.strip().split()
    if len(parts) != 5:
        return None
    minute, hour, dom, month, dow = parts
    # Pattern: 0 start-end/step * * *  or  0 */step * * *
    m = re.match(r'^(\d+)-(\d+)/(\d+)$', hour)
    if m and minute.isdigit():
        step = int(m.group(3))
        return step * 3600
    m = re.match(r'^\*/(\d+)$', hour)
    if m and minute.isdigit():
        step = int(m.group(1))
        return step * 3600
    # Pattern: 0 10,14,16 * * 1-5  (comma-separated hours)
    if ',' in hour and minute.isdigit():
        hours = [int(h) for h in hour.split(',') if h.isdigit()]
        if len(hours) >= 2:
            # Use average gap
            gaps = [hours[i+1] - hours[i] for i in range(len(hours)-1)]
            avg_gap = sum(gaps) / len(gaps)
            return int(avg_gap * 3600)
    return None


def convert_cron_jobs(jobs: list[dict], selected_agents: set[str] | None,
                      agent_name_map: dict[str, str]) -> tuple[list[dict], list[dict], list[str]]:
    """
    Convert OpenClaw cron jobs to bridge-u-f tasks.json format.

    Returns: (new_heartbeats, new_crons, warnings)
    """
    heartbeats = []
    crons = []
    warnings = []

    for job in jobs:
        agent_id = job.get("agentId", "")
        if selected_agents and agent_id not in selected_agents:
            continue

        name = job.get("name", "unnamed")
        enabled = job.get("enabled", False)
        schedule = job.get("schedule", {})
        kind = schedule.get("kind", "")
        payload = job.get("payload", {})
        prompt = payload.get("message", "")
        job_id = job.get("id", "")

        # Generate a clean bridge-u-f task ID
        clean_name = re.sub(r'[^a-zA-Z0-9_-]', '-', name.lower())[:40].strip('-')
        task_id = f"oc-{agent_id}-{clean_name}" if clean_name else f"oc-{job_id[:8]}"

        if kind == "at":
            # One-shot scheduled task — skip, it's a future reminder
            warnings.append(f"  SKIP one-shot job '{name}' for {agent_id} (kind=at, not repeating)")
            continue

        if kind == "every":
            # Interval-based -> heartbeat
            every_ms = schedule.get("everyMs", 0)
            if every_ms > 0:
                heartbeats.append({
                    "id": task_id,
                    "agent": agent_id,
                    "enabled": enabled,
                    "interval_seconds": every_ms // 1000,
                    "prompt": prompt,
                    "note": f"[OpenClaw] {name}",
                })
            else:
                warnings.append(f"  SKIP interval job '{name}' for {agent_id} (everyMs=0)")
            continue

        if kind == "cron":
            expr = schedule.get("expr", "")

            # Try simple daily conversion first
            simple_time = _parse_simple_daily_cron(expr)
            if simple_time:
                crons.append({
                    "id": task_id,
                    "agent": agent_id,
                    "enabled": enabled,
                    "time": simple_time,
                    "action": "enqueue_prompt",
                    "prompt": prompt,
                    "note": f"[OpenClaw] {name}",
                })
                continue

            # Try converting to heartbeat (step/range expressions)
            interval = _cron_expr_to_interval_seconds(expr)
            if interval:
                heartbeats.append({
                    "id": task_id,
                    "agent": agent_id,
                    "enabled": enabled,
                    "interval_seconds": interval,
                    "prompt": prompt,
                    "note": f"[OpenClaw] {name} (was cron: {expr})",
                })
                warnings.append(
                    f"  CONVERT '{name}' for {agent_id}: cron '{expr}' -> heartbeat every {interval}s "
                    f"(original schedule had weekday/time constraints that are lost)"
                )
                continue

            # Fallback: can't convert, report
            warnings.append(
                f"  SKIP complex cron '{name}' for {agent_id}: expr='{expr}' "
                f"(bridge-u-f scheduler only supports HH:MM daily crons; "
                f"manually add to tasks.json)"
            )
            continue

        warnings.append(f"  SKIP unknown schedule kind '{kind}' for job '{name}' ({agent_id})")

    return heartbeats, crons, warnings


def extract_api_credentials(source: Path, agent_ids: list[str]) -> tuple[dict[str, str], list[str]]:
    """
    Extract API credentials from OpenClaw agent auth-profiles.json.
    Returns: (new_secrets, actions)
    """
    new_secrets = {}
    actions = []

    # Check each agent's auth-profiles.json
    seen_providers = set()
    for agent_id in agent_ids:
        auth_path = source / "agents" / agent_id / "agent" / "auth-profiles.json"
        if not auth_path.is_file():
            continue

        try:
            auth_data = json.loads(auth_path.read_text(encoding="utf-8-sig"))
        except Exception as e:
            actions.append(f"  WARNING: Failed to read {auth_path}: {e}")
            continue

        profiles = auth_data.get("profiles", {})
        for profile_id, profile in profiles.items():
            provider = profile.get("provider", "")
            if provider in seen_providers:
                continue
            seen_providers.add(provider)

            bridge_key = CREDENTIAL_MAP.get(provider)
            if not bridge_key:
                continue

            # Extract the relevant credential
            if profile.get("type") == "api_key":
                key_val = profile.get("key", "")
                if key_val:
                    new_secrets[bridge_key] = key_val
                    actions.append(f"  API key: {provider} -> secrets['{bridge_key}'] (from {agent_id})")

    return new_secrets, actions


def build_flex_entry(agent_id: str, display_name: str, workspace_rel: str,
                     system_md_abs: str) -> dict:
    """Build a flex agent entry for agents.json."""
    return {
        "name": agent_id,
        "type": "flex",
        "display_name": display_name,
        "emoji": "🔄",
        "typing_message": f"_{display_name} is typing..._",
        "typing_parse_mode": "Markdown",
        "active_backend": DEFAULT_ACTIVE_BACKEND,
        "system_md": system_md_abs,
        "workspace_dir": workspace_rel,
        "is_active": True,
        "access_scope": "project",
        "background_mode": True,
        "background_detach_after": 150,
        "process_timeout": 600,
        "escalation_thresholds": [30, 60, 90, 150],
        "allowed_backends": list(DEFAULT_ALLOWED_BACKENDS),
    }


def build_fixed_entry(agent_id: str, display_name: str, workspace_rel: str,
                      system_md_abs: str, engine: str, model: str) -> dict:
    """Build a fixed agent entry for agents.json."""
    return {
        "name": agent_id,
        "display_name": display_name,
        "emoji": "🔄",
        "typing_message": f"_{display_name} is typing..._",
        "typing_parse_mode": "Markdown",
        "engine": engine,
        "system_md": system_md_abs,
        "workspace_dir": workspace_rel,
        "model": model,
        "resume_policy": "latest",
        "is_active": True,
        "access_scope": "project",
        "background_mode": True,
        "background_detach_after": 150,
        "process_timeout": 600,
        "escalation_thresholds": [30, 60, 90, 150],
    }


def ask_collision(agent_id: str, dry_run: bool) -> str:
    """Ask user how to handle an existing agent. Returns 'overwrite', 'skip', or 'merge'."""
    if dry_run:
        return "skip"
    while True:
        choice = input(
            f"  Agent '{agent_id}' already exists. [o]verwrite / [s]kip / [m]erge? "
        ).strip().lower()
        if choice in ("o", "overwrite"):
            return "overwrite"
        if choice in ("s", "skip"):
            return "skip"
        if choice in ("m", "merge"):
            return "merge"
        print("  Please enter o, s, or m.")


def main():
    parser = argparse.ArgumentParser(
        description="Import agents from OpenClaw into bridge-u-f"
    )
    parser.add_argument("--source", required=True, help="OpenClaw directory")
    parser.add_argument("--target", required=True, help="bridge-u-f project directory")
    parser.add_argument("--agents", default=None, help="Comma-separated agent IDs (default: all)")
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    parser.add_argument("--default-type", default="flex", choices=["flex", "fixed"],
                        help="Default agent type (default: flex)")
    parser.add_argument("--engine", default="gemini-cli",
                        help="Engine for fixed agents (default: gemini-cli)")
    parser.add_argument("--model", default="gemini-3.1-pro-preview",
                        help="Model for fixed agents (default: gemini-3.1-pro-preview)")
    args = parser.parse_args()

    source = Path(args.source).resolve()
    target = Path(args.target).resolve()
    dry_run = not args.apply
    selected = set(args.agents.split(",")) if args.agents else None

    # --- Validate paths ---
    oc_json_path = source / "openclaw.json"
    if not oc_json_path.is_file():
        print(f"ERROR: {oc_json_path} not found. Is --source an OpenClaw directory?")
        sys.exit(1)

    agents_json_path = target / "agents.json"
    secrets_json_path = target / "secrets.json"
    tasks_json_path = target / "tasks.json"
    if not agents_json_path.is_file():
        print(f"ERROR: {agents_json_path} not found. Is --target a bridge-u-f directory?")
        sys.exit(1)

    # --- Load configs ---
    oc, oc_loaded_from = load_openclaw_config(source)
    with open(agents_json_path, "r", encoding="utf-8-sig") as f:
        bridge_cfg = json.load(f)
    with open(secrets_json_path, "r", encoding="utf-8-sig") as f:
        secrets = json.load(f)
    if tasks_json_path.is_file():
        with open(tasks_json_path, "r", encoding="utf-8-sig") as f:
            tasks_cfg = json.load(f)
    else:
        tasks_cfg = {"version": 1, "heartbeats": [], "crons": []}

    oc_agents = discover_agents(oc)
    bindings = discover_bindings(oc)
    tokens = discover_tokens(oc)

    existing_names = {a["name"] for a in bridge_cfg.get("agents", [])}
    existing_task_ids = (
        {h["id"] for h in tasks_cfg.get("heartbeats", [])}
        | {c["id"] for c in tasks_cfg.get("crons", [])}
    )

    if dry_run:
        print("=== DRY RUN (use --apply to write changes) ===\n")
    else:
        print("=== APPLYING CHANGES ===\n")

    print(f"Source: {source}")
    print(f"OpenClaw config: {oc_loaded_from}")
    print(f"Target: {target}")
    print(f"OpenClaw agents found: {[a['id'] for a in oc_agents]}")
    print(f"Telegram bindings: {bindings}")
    print()

    new_agent_entries = []
    new_secrets = {}
    all_actions = []
    imported_agent_ids = []

    for oc_agent in oc_agents:
        agent_id = oc_agent["id"]
        display_name = oc_agent.get("name", agent_id)

        if selected and agent_id not in selected:
            continue

        print(f"--- Agent: {agent_id} ({display_name}) ---")

        # --- Collision check ---
        if agent_id in existing_names:
            action = ask_collision(agent_id, dry_run)
            if action == "skip":
                print(f"  SKIP (already exists)\n")
                imported_agent_ids.append(agent_id)  # still import tasks/creds
                continue
            elif action == "overwrite":
                print(f"  OVERWRITE (will replace existing entry)")
                bridge_cfg["agents"] = [
                    a for a in bridge_cfg["agents"] if a["name"] != agent_id
                ]
                existing_names.discard(agent_id)
            elif action == "merge":
                print(f"  MERGE (will merge identity docs and memories into existing workspace)")
                existing_entry = next(
                    (a for a in bridge_cfg["agents"] if a["name"] == agent_id), None
                )
                if existing_entry:
                    ws_rel = existing_entry.get("workspace_dir", f"workspaces\\{agent_id}")
                    ws_abs = target / ws_rel
                    oc_agent_dir = source / "agents" / agent_id

                    merge_actions = []
                    if oc_agent_dir.is_dir():
                        merged = merge_identity_docs(oc_agent_dir)
                        if merged:
                            agent_md = ws_abs / "agent.md"
                            merge_actions.append(f"  Append OpenClaw identity to {agent_md}")
                            if not dry_run:
                                ws_abs.mkdir(parents=True, exist_ok=True)
                                existing_text = ""
                                if agent_md.is_file():
                                    existing_text = agent_md.read_text(encoding="utf-8", errors="replace")
                                with open(agent_md, "w", encoding="utf-8") as f:
                                    if existing_text.strip():
                                        f.write(existing_text.rstrip() + "\n\n---\n\n# Imported from OpenClaw\n\n" + merged)
                                    else:
                                        f.write(merged)

                        merge_actions.extend(copy_memories(oc_agent_dir, ws_abs, not dry_run))
                        merge_actions.extend(copy_scripts(oc_agent_dir, ws_abs, not dry_run))
                        merge_actions.extend(copy_skills(oc_agent_dir, ws_abs, not dry_run))

                    for a in merge_actions:
                        print(a)
                    all_actions.extend(merge_actions)
                imported_agent_ids.append(agent_id)
                print()
                continue

        # --- Locate OpenClaw agent directory ---
        oc_agent_dir = source / "agents" / agent_id

        # --- Target workspace ---
        ws_rel = f"workspaces\\{agent_id}"
        ws_abs = target / "workspaces" / agent_id
        agent_md_path = ws_abs / "agent.md"
        agent_md_abs = str(agent_md_path)

        actions = []
        actions.append(f"  Create workspace: {ws_abs}")
        if not dry_run:
            ws_abs.mkdir(parents=True, exist_ok=True)
            (ws_abs / "memory").mkdir(exist_ok=True)

        # --- Merge identity docs -> agent.md ---
        if oc_agent_dir.is_dir():
            merged = merge_identity_docs(oc_agent_dir)
            if merged:
                actions.append(f"  Write {agent_md_path} (merged from {', '.join(IDENTITY_FILES)})")
                if not dry_run:
                    agent_md_path.write_text(merged, encoding="utf-8")
            else:
                actions.append(f"  No identity docs found in {oc_agent_dir}")
                if not dry_run:
                    agent_md_path.write_text(
                        f"# {display_name}\n\nImported from OpenClaw.\n",
                        encoding="utf-8",
                    )
        else:
            actions.append(f"  No agent dir at {oc_agent_dir}, creating stub agent.md")
            if not dry_run:
                agent_md_path.write_text(
                    f"# {display_name}\n\nImported from OpenClaw.\n",
                    encoding="utf-8",
                )

        # --- Copy memories (all state files, not just journals) ---
        if oc_agent_dir.is_dir():
            actions.extend(copy_memories(oc_agent_dir, ws_abs, not dry_run))

        # --- Copy scripts ---
        if oc_agent_dir.is_dir():
            actions.extend(copy_scripts(oc_agent_dir, ws_abs, not dry_run))

        # --- Copy skills ---
        if oc_agent_dir.is_dir():
            actions.extend(copy_skills(oc_agent_dir, ws_abs, not dry_run))

        # --- Copy extra directories (e.g. renee's news_briefing/) ---
        if oc_agent_dir.is_dir():
            extra_dirs = [
                d.name for d in oc_agent_dir.iterdir()
                if d.is_dir() and d.name not in {
                    "memory", "scripts", "skills", "sessions", "agent",
                    ".openclaw", ".pi", "temp", "__pycache__",
                }
            ]
            if extra_dirs:
                actions.extend(copy_extra_dirs(oc_agent_dir, ws_abs, not dry_run, extra_dirs))

        # --- Telegram token ---
        tg_account = bindings.get(agent_id)
        bot_token = tokens.get(tg_account) if tg_account else None
        if bot_token:
            actions.append(f"  Telegram token: {tg_account} -> secrets['{agent_id}']")
            new_secrets[agent_id] = bot_token
        else:
            actions.append(f"  WARNING: No telegram token found for {agent_id}")

        # --- Build agents.json entry ---
        if args.default_type == "flex":
            entry = build_flex_entry(agent_id, display_name, ws_rel, agent_md_abs)
            actions.append(f"  agents.json: flex agent, backends={[b['engine'] for b in DEFAULT_ALLOWED_BACKENDS]}")
        else:
            entry = build_fixed_entry(
                agent_id, display_name, ws_rel, agent_md_abs,
                engine=args.engine, model=args.model,
            )
            actions.append(f"  agents.json: fixed agent, engine={args.engine}, model={args.model}")

        new_agent_entries.append(entry)
        existing_names.add(agent_id)
        imported_agent_ids.append(agent_id)

        for a in actions:
            print(a)
        all_actions.extend(actions)
        print()

    # --- API credentials ---
    all_agent_ids = [a["id"] for a in oc_agents]
    if selected:
        all_agent_ids = [aid for aid in all_agent_ids if aid in selected]
    cred_secrets, cred_actions = extract_api_credentials(source, all_agent_ids)
    if cred_actions:
        print("--- API Credentials ---")
        for a in cred_actions:
            print(a)
        all_actions.extend(cred_actions)
        # Don't overwrite existing keys
        for k, v in cred_secrets.items():
            if k not in secrets and k not in new_secrets:
                new_secrets[k] = v
            elif k in secrets:
                print(f"  NOTE: secrets['{k}'] already exists, keeping existing value")
        print()

    # --- Cron jobs / heartbeats ---
    cron_jobs_path = source / "cron" / "jobs.json"
    if cron_jobs_path.is_file():
        try:
            cron_data, cron_loaded_from = load_json_with_backups(cron_jobs_path)
            oc_jobs = cron_data.get("jobs", [])
        except Exception as e:
            print(f"WARNING: Failed to read {cron_jobs_path}: {e}")
            oc_jobs = []
            cron_loaded_from = None

        new_heartbeats = []
        new_crons = []

        if oc_jobs:
            agent_name_map = {a["id"]: a.get("name", a["id"]) for a in oc_agents}
            new_heartbeats, new_crons, cron_warnings = convert_cron_jobs(
                oc_jobs, selected, agent_name_map
            )

            print(f"--- Scheduled Tasks ({len(oc_jobs)} OpenClaw jobs) ---")
            if cron_loaded_from and cron_loaded_from != cron_jobs_path:
                print(f"  Using backup cron source: {cron_loaded_from}")

            # Filter out tasks whose IDs already exist
            new_heartbeats = [h for h in new_heartbeats if h["id"] not in existing_task_ids]
            new_crons = [c for c in new_crons if c["id"] not in existing_task_ids]

            for h in new_heartbeats:
                interval_h = h["interval_seconds"] / 3600
                prompt_preview = h["prompt"][:60].replace("\n", " ") + ("..." if len(h["prompt"]) > 60 else "")
                enabled_str = "ON " if h["enabled"] else "OFF"
                print(f"  {enabled_str} heartbeat: {h['agent']}/{h['id']} every {interval_h:.1f}h")
                print(f"       prompt: {prompt_preview}")
                all_actions.append(f"  Heartbeat: {h['id']}")

            for c in new_crons:
                prompt_preview = c["prompt"][:60].replace("\n", " ") + ("..." if len(c["prompt"]) > 60 else "")
                enabled_str = "ON " if c["enabled"] else "OFF"
                print(f"  {enabled_str} cron: {c['agent']}/{c['id']} at {c['time']}")
                print(f"       prompt: {prompt_preview}")
                all_actions.append(f"  Cron: {c['id']}")

            for w in cron_warnings:
                print(w)
                all_actions.append(w)

            print()
    else:
        new_heartbeats = []
        new_crons = []

    # --- Summary ---
    print("=" * 60)
    print(f"Agents to import: {len(new_agent_entries)}")
    print(f"Tokens/keys to add: {len(new_secrets)}")
    print(f"Heartbeats to add: {len(new_heartbeats)}")
    print(f"Crons to add: {len(new_crons)}")
    print(f"Total actions: {len(all_actions)}")

    if not new_agent_entries and not new_secrets and not new_heartbeats and not new_crons:
        print("\nNothing to import.")
        return

    if dry_run:
        print("\nRe-run with --apply to execute these changes.")
        return

    # --- Write changes ---
    # Update agents.json
    if new_agent_entries:
        bridge_cfg["agents"].extend(new_agent_entries)
        with open(agents_json_path, "w", encoding="utf-8") as f:
            json.dump(bridge_cfg, f, indent=2, ensure_ascii=False)
        print(f"\nUpdated {agents_json_path}")

    # Update secrets.json
    if new_secrets:
        secrets.update(new_secrets)
        with open(secrets_json_path, "w", encoding="utf-8") as f:
            json.dump(secrets, f, indent=2, ensure_ascii=False)
        print(f"Updated {secrets_json_path}")

    # Update tasks.json
    if new_heartbeats or new_crons:
        tasks_cfg.setdefault("heartbeats", []).extend(new_heartbeats)
        tasks_cfg.setdefault("crons", []).extend(new_crons)
        with open(tasks_json_path, "w", encoding="utf-8") as f:
            json.dump(tasks_cfg, f, indent=2, ensure_ascii=False)
        print(f"Updated {tasks_json_path}")

    print("\nDone! Restart bridge-u-f to pick up the new agents.")


if __name__ == "__main__":
    main()
