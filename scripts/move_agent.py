#!/usr/bin/env python3
"""
move_agent.py — HASHI Agent Migration Tool

Moves or copies an agent (config + secrets + workspace) between HASHI instances,
to/from USB/portable paths, or between WSL and Windows.

Usage:
  # Instance-to-instance (direct)
  python scripts/move_agent.py zelda hashi2
  python scripts/move_agent.py zelda hashi9 --keep-source
  python scripts/move_agent.py zelda hashi1 --sync          # move-back with memory sync

  # Export to package file
  python scripts/move_agent.py zelda --export /mnt/usb
  python scripts/move_agent.py zelda --export /mnt/usb --plain     # no encryption
  python scripts/move_agent.py zelda --export /mnt/usb --no-secrets

  # Import from package file
  python scripts/move_agent.py --import /mnt/usb/zelda_20260322_143000.hashi-agent
  python scripts/move_agent.py --import /mnt/usb/zelda_20260322.hashi-agent --target hashi2

  # List instances and agents
  python scripts/move_agent.py --list-instances
  python scripts/move_agent.py --list-agents [instance]

  # Dry run (preview only)
  python scripts/move_agent.py zelda hashi2 --dry-run
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PACKAGE_EXT = ".hashi-agent"
INSTANCES_FILE_CANDIDATES = [
    Path(__file__).parent.parent / "instances.json",           # same repo
    Path.home() / ".hashi" / "instances.json",                 # user-level
    Path("/mnt/c/Users") / os.environ.get("USER", "user") / ".hashi" / "instances.json",
]


# ---------------------------------------------------------------------------
# Encryption helpers (password-based AES via cryptography.fernet)
# ---------------------------------------------------------------------------

def _derive_key(password: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    import base64
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=480000)
    return base64.urlsafe_b64encode(kdf.derive(password.encode()))


def encrypt_data(data: bytes, password: str) -> bytes:
    """Encrypt bytes with password. Returns: 16-byte salt + fernet token."""
    from cryptography.fernet import Fernet
    salt = os.urandom(16)
    key = _derive_key(password, salt)
    token = Fernet(key).encrypt(data)
    return salt + token


def decrypt_data(data: bytes, password: str) -> bytes:
    """Decrypt bytes with password."""
    from cryptography.fernet import Fernet
    salt, token = data[:16], data[16:]
    key = _derive_key(password, salt)
    return Fernet(key).decrypt(token)


def has_crypto() -> bool:
    try:
        import cryptography  # noqa
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Instance registry
# ---------------------------------------------------------------------------

def load_instances(instances_file: Optional[Path] = None) -> dict:
    candidates = [instances_file] if instances_file else INSTANCES_FILE_CANDIDATES
    for path in candidates:
        if path and Path(path).exists():
            with open(path) as f:
                data = json.load(f)
            return data.get("instances", {})
    return {}


def resolve_instance(name: str, instances: dict) -> dict:
    if name not in instances:
        print(f"Error: unknown instance '{name}'. Known: {list(instances.keys())}")
        sys.exit(1)
    inst = instances[name]
    root = inst.get("root")
    # Resolve WSL ↔ Windows path
    if root is None and inst.get("platform") == "portable":
        print(f"Error: instance '{name}' has no root path set. Edit instances.json.")
        sys.exit(1)
    return inst


def instance_root(inst: dict) -> Path:
    """Return the filesystem root of the instance, accessible from current OS."""
    platform = inst.get("platform", "wsl")
    root = inst.get("root")
    if platform == "windows":
        # If running in WSL, use the wsl_root mapping
        if _is_wsl():
            wsl_root = inst.get("wsl_root")
            if wsl_root:
                return Path(wsl_root)
        return Path(root)
    elif platform == "wsl":
        # If running from Windows, use the UNC path to access WSL filesystem
        if not _is_wsl():
            wsl_root = inst.get("wsl_root_from_windows")
            if wsl_root:
                return Path(wsl_root)
        return Path(root)
    elif platform == "portable":
        return Path(root)
    return Path(root)


def _is_wsl() -> bool:
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Agent discovery
# ---------------------------------------------------------------------------

def find_agent_in_instance(agent_id: str, root: Path) -> Optional[dict]:
    """Find agent config block from agents.json."""
    agents_file = root / "agents.json"
    if not agents_file.exists():
        return None
    with open(agents_file) as f:
        data = json.load(f)
    agents = data if isinstance(data, list) else data.get("agents", [])
    for ag in agents:
        if ag.get("name") == agent_id or ag.get("id") == agent_id:
            return ag
    return None


def list_agents_in_instance(root: Path) -> list[str]:
    agents_file = root / "agents.json"
    if not agents_file.exists():
        return []
    with open(agents_file) as f:
        data = json.load(f)
    agents = data if isinstance(data, list) else data.get("agents", [])
    return [ag.get("name") or ag.get("id", "?") for ag in agents]


def get_agent_secrets(agent_id: str, root: Path) -> dict:
    """Extract all secrets keys belonging to this agent."""
    secrets_file = root / "secrets.json"
    if not secrets_file.exists():
        return {}
    with open(secrets_file) as f:
        all_secrets = json.load(f)
    # Collect keys that start with agent_id or are exactly agent_id
    result = {}
    for k, v in all_secrets.items():
        if k == agent_id or k.startswith(f"{agent_id}_") or k.startswith(f"{agent_id}."):
            result[k] = v
    return result


def get_workspace_dir(agent_config: dict, root: Path) -> Optional[Path]:
    ws = agent_config.get("workspace_dir") or agent_config.get("workspace")
    if not ws:
        # fallback: workspaces/<name>
        ws = f"workspaces/{agent_config.get('name', '')}"
    p = Path(ws)
    if not p.is_absolute():
        p = root / p
    return p if p.exists() else None


# ---------------------------------------------------------------------------
# Package: export
# ---------------------------------------------------------------------------

def export_agent(
    agent_id: str,
    source_root: Path,
    dest_dir: Path,
    password: Optional[str] = None,
    include_secrets: bool = True,
    dry_run: bool = False,
) -> Path:
    """Pack agent into a .hashi-agent zip and save to dest_dir."""
    agent_config = find_agent_in_instance(agent_id, source_root)
    if not agent_config:
        print(f"Error: agent '{agent_id}' not found in {source_root}")
        sys.exit(1)

    workspace = get_workspace_dir(agent_config, source_root)
    secrets = get_agent_secrets(agent_id, source_root) if include_secrets else {}

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    pkg_name = f"{agent_id}_{ts}{PACKAGE_EXT}"
    pkg_path = dest_dir / pkg_name

    manifest = {
        "agent_id": agent_id,
        "exported_at": datetime.now().isoformat(),
        "source_root": str(source_root),
        "has_secrets": include_secrets and bool(secrets),
        "secrets_encrypted": password is not None,
        "hashi_version": "1.2",
    }

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Exporting agent '{agent_id}'")
    print(f"  Source:    {source_root}")
    print(f"  Package:   {pkg_path}")
    print(f"  Workspace: {workspace}")
    print(f"  Secrets:   {len(secrets)} keys {'(encrypted)' if password else '(plain)' if include_secrets else '(excluded)'}")

    if workspace:
        ws_files = list(workspace.rglob("*"))
        print(f"  Files:     {len([f for f in ws_files if f.is_file()])} files")

    if dry_run:
        print("\n[DRY RUN] No files written.")
        return pkg_path

    dest_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(pkg_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # manifest
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        # agent config
        zf.writestr("agent_config.json", json.dumps(agent_config, indent=2))

        # secrets
        if include_secrets and secrets:
            secrets_bytes = json.dumps(secrets, indent=2).encode()
            if password:
                secrets_bytes = encrypt_data(secrets_bytes, password)
                zf.writestr("secrets.bin", secrets_bytes)
            else:
                zf.writestr("secrets.json", secrets_bytes.decode())

        # workspace
        if workspace and workspace.exists():
            for fpath in workspace.rglob("*"):
                if fpath.is_file():
                    arcname = "workspace/" + str(fpath.relative_to(workspace))
                    zf.write(fpath, arcname)

    size_mb = pkg_path.stat().st_size / 1024 / 1024
    print(f"\n✅ Package created: {pkg_path} ({size_mb:.1f} MB)")
    return pkg_path


# ---------------------------------------------------------------------------
# Package: import
# ---------------------------------------------------------------------------

def import_agent(
    pkg_path: Path,
    target_root: Path,
    password: Optional[str] = None,
    on_conflict: str = "ask",   # ask | overwrite | merge | skip
    dry_run: bool = False,
) -> bool:
    """Unpack a .hashi-agent zip into target instance."""
    if not pkg_path.exists():
        print(f"Error: package not found: {pkg_path}")
        return False

    with zipfile.ZipFile(pkg_path, "r") as zf:
        names = zf.namelist()
        manifest = json.loads(zf.read("manifest.json"))
        agent_config = json.loads(zf.read("agent_config.json"))

        # Secrets
        secrets: dict = {}
        if "secrets.bin" in names:
            if password is None:
                password = getpass.getpass("Package password: ")
            try:
                secrets_bytes = decrypt_data(zf.read("secrets.bin"), password)
                secrets = json.loads(secrets_bytes)
            except Exception:
                print("Error: wrong password or corrupted secrets.")
                return False
        elif "secrets.json" in names:
            secrets = json.loads(zf.read("secrets.json"))

        agent_id = manifest["agent_id"]
        ws_files = [n for n in names if n.startswith("workspace/")]

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Importing agent '{agent_id}'")
    print(f"  Package:  {pkg_path}")
    print(f"  Target:   {target_root}")
    print(f"  Secrets:  {len(secrets)} keys")
    print(f"  Files:    {len(ws_files)}")

    # Check conflict
    existing = find_agent_in_instance(agent_id, target_root)
    if existing:
        if on_conflict == "ask":
            ans = input(f"\n  Agent '{agent_id}' already exists in target. [O]verwrite / [M]erge memories / [S]kip? ").strip().lower()
            if ans == "s":
                print("Skipped.")
                return False
            on_conflict = "overwrite" if ans == "o" else "merge"
        if on_conflict == "skip":
            print(f"  Skipping — agent already exists.")
            return False

    if dry_run:
        print("\n[DRY RUN] No files written.")
        return True

    # Write workspace
    target_ws = target_root / "workspaces" / agent_id
    with zipfile.ZipFile(pkg_path, "r") as zf:
        if on_conflict == "merge" and target_ws.exists():
            _merge_workspace(zf, target_ws, agent_id)
        else:
            if target_ws.exists():
                shutil.rmtree(target_ws)
            target_ws.mkdir(parents=True, exist_ok=True)
            for name in ws_files:
                rel = name[len("workspace/"):]
                if not rel:
                    continue
                dest = target_ws / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(name))

    # Update agents.json
    _upsert_agent_config(agent_config, target_root, agent_id)

    # Update secrets.json
    if secrets:
        _upsert_secrets(secrets, target_root)

    # Vector backfill if available
    _try_vector_backfill(target_root, agent_id)

    print(f"\n✅ Agent '{agent_id}' imported to {target_root}")
    return True


def _merge_workspace(zf: zipfile.ZipFile, target_ws: Path, agent_id: str):
    """Merge workspace files; for sqlite, merge memory rows by timestamp."""
    ws_files = [n for n in zf.namelist() if n.startswith("workspace/")]
    for name in ws_files:
        rel = name[len("workspace/"):]
        if not rel:
            continue
        dest = target_ws / rel
        # Special: merge SQLite databases
        if dest.exists() and dest.suffix == ".sqlite":
            _merge_sqlite(zf.read(name), dest)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        # For other files: newer wins (from package = source)
        dest.write_bytes(zf.read(name))


def _merge_sqlite(src_bytes: bytes, dest_path: Path):
    """Merge memories/turns from src SQLite into dest, INSERT OR IGNORE by rowid."""
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp.write(src_bytes)
        tmp_path = tmp.name
    try:
        dst = sqlite3.connect(str(dest_path))
        src = sqlite3.connect(tmp_path)
        # Attach source and copy rows for key tables
        dst.execute(f"ATTACH DATABASE '{tmp_path}' AS src")
        for table in ["memories", "conversation_turns"]:
            try:
                dst.execute(f"INSERT OR IGNORE INTO {table} SELECT * FROM src.{table}")
            except Exception:
                pass
        dst.commit()
        dst.close()
        src.close()
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Direct instance-to-instance move
# ---------------------------------------------------------------------------

def move_agent(
    agent_id: str,
    source_root: Path,
    target_root: Path,
    keep_source: bool = False,
    sync_memories: bool = False,
    include_secrets: bool = True,
    dry_run: bool = False,
) -> bool:
    """Move agent directly between two accessible instances."""
    agent_config = find_agent_in_instance(agent_id, source_root)
    if not agent_config:
        print(f"Error: agent '{agent_id}' not found in {source_root}")
        return False

    workspace = get_workspace_dir(agent_config, source_root)
    secrets = get_agent_secrets(agent_id, source_root) if include_secrets else {}
    target_ws = target_root / "workspaces" / agent_id

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Moving agent '{agent_id}'")
    print(f"  From:     {source_root}")
    print(f"  To:       {target_root}")
    print(f"  Secrets:  {len(secrets)} keys")
    if workspace:
        ws_count = len(list(workspace.rglob("*")))
        print(f"  Files:    {ws_count}")

    # Conflict check
    existing = find_agent_in_instance(agent_id, target_root)
    if existing:
        if sync_memories and target_ws.exists() and workspace:
            print(f"  Conflict: merging memories (--sync mode)")
        else:
            ans = input(f"\n  Agent '{agent_id}' already exists in target. [O]verwrite / [M]erge / [S]kip? ").strip().lower()
            if ans == "s":
                print("Skipped.")
                return False
            if ans == "m":
                sync_memories = True

    if dry_run:
        print("\n[DRY RUN] No changes made.")
        return True

    # Copy workspace
    if workspace and workspace.exists():
        if sync_memories and target_ws.exists():
            # Merge sqlite, copy other files
            for src_file in workspace.rglob("*"):
                if src_file.is_file():
                    rel = src_file.relative_to(workspace)
                    dst_file = target_ws / rel
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    if dst_file.exists() and dst_file.suffix == ".sqlite":
                        _merge_sqlite(src_file.read_bytes(), dst_file)
                    else:
                        shutil.copy2(src_file, dst_file)
        else:
            if target_ws.exists():
                shutil.rmtree(target_ws)
            shutil.copytree(workspace, target_ws)

    # Update target agents.json
    _upsert_agent_config(agent_config, target_root, agent_id)

    # Update target secrets.json
    if secrets:
        _upsert_secrets(secrets, target_root)

    # Vector backfill on target
    _try_vector_backfill(target_root, agent_id)

    # Handle source
    if not keep_source:
        _deactivate_agent(agent_id, source_root)
        print(f"  Source:   agent deactivated (is_active: false) — workspace preserved")
    else:
        print(f"  Source:   kept active (--keep-source)")

    print(f"\n✅ Agent '{agent_id}' moved to {target_root}")
    print(f"   Next: /reboot on target instance to start {agent_id}")
    return True


# ---------------------------------------------------------------------------
# agents.json / secrets.json helpers
# ---------------------------------------------------------------------------

def _upsert_agent_config(agent_config: dict, root: Path, agent_id: str):
    agents_file = root / "agents.json"
    if not agents_file.exists():
        print(f"  WARNING: {agents_file} not found — skipping config update")
        return

    with open(agents_file) as f:
        data = json.load(f)

    is_list = isinstance(data, list)
    agents = data if is_list else data.get("agents", [])

    # Update workspace_dir to match target layout
    new_config = dict(agent_config)
    new_config["workspace_dir"] = f"workspaces/{agent_id}"
    new_config["system_md"] = f"workspaces/{agent_id}/agent.md"
    # Ensure active on target
    new_config["is_active"] = True

    updated = False
    for i, ag in enumerate(agents):
        if ag.get("name") == agent_id or ag.get("id") == agent_id:
            agents[i] = new_config
            updated = True
            break
    if not updated:
        agents.append(new_config)

    if is_list:
        data = agents
    else:
        data["agents"] = agents

    with open(agents_file, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  agents.json updated on target")


def _upsert_secrets(secrets: dict, root: Path):
    secrets_file = root / "secrets.json"
    existing = {}
    if secrets_file.exists():
        with open(secrets_file) as f:
            existing = json.load(f)
    existing.update(secrets)
    with open(secrets_file, "w") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)
    print(f"  secrets.json updated on target ({len(secrets)} keys)")


def _deactivate_agent(agent_id: str, root: Path):
    agents_file = root / "agents.json"
    if not agents_file.exists():
        return
    with open(agents_file) as f:
        data = json.load(f)
    is_list = isinstance(data, list)
    agents = data if is_list else data.get("agents", [])
    for ag in agents:
        if ag.get("name") == agent_id or ag.get("id") == agent_id:
            ag["is_active"] = False
            break
    if is_list:
        data = agents
    else:
        data["agents"] = agents
    with open(agents_file, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _try_vector_backfill(root: Path, agent_id: str):
    """Attempt vector backfill if migrate_vectors.py exists."""
    migrate = root / "scripts" / "migrate_vectors.py"
    ws = root / "workspaces" / agent_id
    if migrate.exists() and ws.exists():
        import subprocess
        try:
            result = subprocess.run(
                [sys.executable, str(migrate), str(ws)],
                capture_output=True, text=True, cwd=str(root)
            )
            if result.returncode == 0:
                print(f"  Vector memory backfill complete")
            else:
                print(f"  Vector backfill skipped: {result.stderr.strip()[:80]}")
        except Exception as e:
            print(f"  Vector backfill skipped: {e}")


# ---------------------------------------------------------------------------
# USB auto-detect
# ---------------------------------------------------------------------------

def detect_usb_paths() -> list[Path]:
    candidates = []
    if _is_wsl():
        for d in Path("/mnt").iterdir():
            if d.is_dir() and len(d.name) == 1 and d.name not in ("c", "wsl"):
                candidates.append(d)
        for d in Path("/media").rglob("*"):
            if d.is_dir() and d.parent != Path("/media"):
                candidates.append(d)
    else:
        import string
        for letter in string.ascii_uppercase[3:]:  # D onwards
            p = Path(f"{letter}:\\")
            if p.exists():
                candidates.append(p)
    return candidates


# ---------------------------------------------------------------------------
# Migration report
# ---------------------------------------------------------------------------

def print_report(agent_id: str, source: str, target: str, details: list[str]):
    width = 55
    print("\n" + "═" * width)
    print("HASHI Agent Migration Report")
    print("═" * width)
    print(f"  Agent:  {agent_id}")
    print(f"  From:   {source}")
    print(f"  To:     {target}")
    print("─" * width)
    for line in details:
        print(f"  {line}")
    print("═" * width)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="HASHI Agent Migration Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("agent_id", nargs="?", help="Agent ID to move/export")
    parser.add_argument("target_instance", nargs="?", help="Target instance ID (from instances.json)")

    parser.add_argument("--export", metavar="DIR", help="Export agent to package in this directory")
    parser.add_argument("--import", dest="import_path", metavar="FILE", help="Import agent from .hashi-agent package")
    parser.add_argument("--target", metavar="INSTANCE", help="Target instance for --import (default: current)")

    parser.add_argument("--plain", action="store_true", help="Do not encrypt secrets (export only)")
    parser.add_argument("--no-secrets", action="store_true", help="Exclude secrets from package")
    parser.add_argument("--keep-source", action="store_true", help="Keep source agent active after move")
    parser.add_argument("--sync", action="store_true", help="Merge memories instead of overwrite (move-back)")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without making changes")
    parser.add_argument("--list-instances", action="store_true", help="List known instances")
    parser.add_argument("--list-agents", nargs="?", const="auto", metavar="INSTANCE", help="List agents in an instance")
    parser.add_argument("--instances-file", metavar="FILE", help="Path to instances.json")
    parser.add_argument("--source-instance", metavar="INSTANCE", default="hashi2", help="Source instance (default: hashi2)")
    parser.add_argument("--password", metavar="PASS", help="Encryption password (prompted if omitted for encrypted packages)")

    args = parser.parse_args()
    instances = load_instances(Path(args.instances_file) if args.instances_file else None)

    # ── list instances ────────────────────────────────────────────────────
    if args.list_instances:
        print("\nKnown HASHI instances:")
        for name, inst in instances.items():
            root = inst.get("root") or "(auto-detect)"
            active = "✓" if inst.get("active") else "✗"
            print(f"  [{active}] {name:12s} — {inst.get('display_name', '')}  ({root})")
        return

    # ── list agents ────────────────────────────────────────────────────────
    if args.list_agents is not None:
        inst_name = args.list_agents if args.list_agents != "auto" else args.source_instance
        inst = resolve_instance(inst_name, instances)
        root = instance_root(inst)
        agents = list_agents_in_instance(root)
        print(f"\nAgents in {inst_name} ({root}):")
        for a in agents:
            print(f"  • {a}")
        return

    # ── import ────────────────────────────────────────────────────────────
    if args.import_path:
        pkg = Path(args.import_path)
        if args.target:
            inst = resolve_instance(args.target, instances)
            target_root = instance_root(inst)
        else:
            # default: current hashi2
            inst = resolve_instance(args.source_instance, instances)
            target_root = instance_root(inst)

        password = args.password
        # Check if package has encrypted secrets
        with zipfile.ZipFile(pkg, "r") as zf:
            needs_pw = "secrets.bin" in zf.namelist()
        if needs_pw and not password and not args.plain:
            password = getpass.getpass("Package password: ")

        import_agent(pkg, target_root, password=password, dry_run=args.dry_run)
        return

    # ── export ────────────────────────────────────────────────────────────
    if args.export:
        if not args.agent_id:
            print("Error: agent_id required for --export"); sys.exit(1)

        inst = resolve_instance(args.source_instance, instances)
        source_root = instance_root(inst)
        dest_dir = Path(args.export)

        password = None
        if not args.plain and not args.no_secrets:
            if has_crypto():
                password = args.password or getpass.getpass("Set package password (Enter to skip encryption): ").strip() or None
            else:
                print("  WARNING: cryptography package not installed — exporting secrets as plain text")

        export_agent(
            args.agent_id, source_root, dest_dir,
            password=password,
            include_secrets=not args.no_secrets,
            dry_run=args.dry_run,
        )
        return

    # ── direct move ───────────────────────────────────────────────────────
    if not args.agent_id or not args.target_instance:
        parser.print_help()
        sys.exit(1)

    src_inst = resolve_instance(args.source_instance, instances)
    dst_inst = resolve_instance(args.target_instance, instances)
    source_root = instance_root(src_inst)
    target_root = instance_root(dst_inst)

    success = move_agent(
        args.agent_id,
        source_root,
        target_root,
        keep_source=args.keep_source,
        sync_memories=args.sync,
        include_secrets=not args.no_secrets,
        dry_run=args.dry_run,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
