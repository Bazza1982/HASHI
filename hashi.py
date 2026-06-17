#!/usr/bin/env python3
"""
HASHI command-line tool.

Usage:
    python hashi.py whatsapp        — interactive WhatsApp setup wizard
    python hashi.py whatsapp status — show current WhatsApp config & session state
    python hashi.py whatsapp reset  — clear saved session (force re-link)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# ──────────────────────────────────────────────
# Colour helpers
# ──────────────────────────────────────────────
_BOLD  = "\033[1m"
_GREEN = "\033[32m"
_CYAN  = "\033[36m"
_YELLOW= "\033[33m"
_RED   = "\033[31m"
_RESET = "\033[0m"

def _b(t): return f"{_BOLD}{t}{_RESET}"
def _g(t): return f"{_GREEN}{t}{_RESET}"
def _c(t): return f"{_CYAN}{t}{_RESET}"
def _y(t): return f"{_YELLOW}{t}{_RESET}"
def _r(t): return f"{_RED}{t}{_RESET}"

def _hr(): print(_CYAN + "─" * 55 + _RESET)


# ──────────────────────────────────────────────
# Config helpers
# ──────────────────────────────────────────────

def _load_agents_json() -> tuple[dict, Path]:
    path = ROOT_DIR / "agents.json"
    if not path.exists():
        sample = ROOT_DIR / "agents.json.sample"
        if sample.exists():
            import shutil
            shutil.copy(sample, path)
        else:
            path.write_text("{}", encoding="utf-8")
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    return raw, path


def _save_agents_json(data: dict, path: Path):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _wa_cfg(data: dict) -> dict:
    return data.setdefault("global", {}).setdefault("whatsapp", {})


def _session_dir(data: dict) -> Path:
    wa = _wa_cfg(data)
    raw = wa.get("session_dir", "@home/wa_session")
    if raw.startswith("@home/"):
        return ROOT_DIR / raw[len("@home/"):]
    return Path(raw)


def _session_exists(data: dict) -> bool:
    sd = _session_dir(data)
    db = sd / "bridge-u-f"
    return db.exists() or (sd / "bridge-u-f.db").exists()


# ──────────────────────────────────────────────
# whatsapp status
# ──────────────────────────────────────────────

def cmd_whatsapp_status(data: dict):
    wa = _wa_cfg(data)
    _hr()
    print(f"  {_b('WhatsApp Status')}")
    _hr()
    enabled = wa.get("enabled", False)
    print(f"  Enabled        : {_g('yes') if enabled else _y('no')}")
    nums = wa.get("allowed_numbers", [])
    print(f"  Allowed numbers: {', '.join(nums) if nums else _y('(none — accepts all DMs)')}")
    chat_ids = wa.get("allowed_chat_ids", [])
    print(f"  Allowed groups : {', '.join(chat_ids) if chat_ids else '(none)'}")
    print(f"  Default agent  : {wa.get('default_agent', 'hashiko')}")
    session_ok = _session_exists(data)
    print(f"  Session saved  : {_g('yes — no QR needed on next start') if session_ok else _y('no — QR scan required')}")
    _hr()


# ──────────────────────────────────────────────
# whatsapp reset
# ──────────────────────────────────────────────

def cmd_whatsapp_reset(data: dict):
    sd = _session_dir(data)
    removed = []
    for pattern in ("bridge-u-f", "bridge-u-f.db", "bridge-u-f-wal", "bridge-u-f-shm"):
        p = sd / pattern
        if p.exists():
            p.unlink()
            removed.append(p.name)
    if removed:
        print(_g(f"✓ Removed session files: {', '.join(removed)}"))
    else:
        print(_y("No session files found — already clean."))
    print("  Next HASHI start will show a fresh QR code.")


# ──────────────────────────────────────────────
# whatsapp wizard (main setup flow)
# ──────────────────────────────────────────────

def _prompt(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"  {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return val or (default or "")


def _confirm(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    ans = _prompt(f"{prompt} ({hint})").lower()
    if not ans:
        return default
    return ans in ("y", "yes")


def _validate_phone(number: str) -> str | None:
    """Return normalised E.164 (+digits) or None if invalid."""
    n = number.strip()
    if not n.startswith("+"):
        n = "+" + n
    digits = n[1:]
    if not digits.isdigit() or len(digits) < 7:
        return None
    return n


async def _run_link_wizard(session_dir: Path):
    """Run the neonize QR linker interactively, printing QR in the terminal."""
    try:
        from neonize.aioze.client import NewAClient
        from neonize.aioze.events import ConnectedEv, DisconnectedEv, PairStatusEv
    except ImportError:
        print(_r("  ✗ neonize is not installed.  Run:  pip install neonize"))
        sys.exit(1)

    try:
        import segno
    except ImportError:
        print(_r("  ✗ segno is not installed.  Run:  pip install segno"))
        sys.exit(1)

    import asyncio

    session_dir.mkdir(parents=True, exist_ok=True)
    client_name = str(session_dir / "bridge-u-f")

    linked = asyncio.Event()
    client = NewAClient(client_name)

    async def on_qr(_, data_qr: bytes):
        print()
        print(_b("  Scan this QR code with WhatsApp on your phone:"))
        print("  WhatsApp → Linked Devices → Link a device")
        print()
        segno.make_qr(data_qr).terminal(compact=True)
        print()
        print(_y("  Waiting for you to scan… (Ctrl+C to cancel)"))

    client.qr(on_qr)

    @client.event(ConnectedEv)
    async def _on_connected(_, __):
        print()
        print(_g("  ✓ WhatsApp linked and connected!"))
        linked.set()

    @client.event(PairStatusEv)
    async def _on_pair_status(_, ev):
        pass  # suppress noise

    @client.event(DisconnectedEv)
    async def _on_disconnected(_, __):
        if not linked.is_set():
            print(_y("  ⚠  WhatsApp disconnected before linking completed."))

    connect_task = await client.connect()
    try:
        await asyncio.wait_for(linked.wait(), timeout=300.0)
        await asyncio.sleep(1.0)
        print("  Session saved — future starts will reuse this login.")
    except asyncio.TimeoutError:
        print(_r("  ✗ Timed out (5 minutes). Please try again."))
        sys.exit(1)
    finally:
        connect_task.cancel()
        try:
            await connect_task
        except asyncio.CancelledError:
            pass
        await client.disconnect()


def cmd_whatsapp_wizard():
    import asyncio

    data, path = _load_agents_json()
    wa = _wa_cfg(data)

    print()
    _hr()
    print(f"  {_b('HASHI  ·  WhatsApp Setup Wizard')}")
    _hr()
    print()

    # ── Step 1: check existing session ──────────────────────────────────
    session_ok = _session_exists(data)
    if session_ok:
        print(_g("  ✓ An existing WhatsApp session was found."))
        if wa.get("enabled"):
            print("    WhatsApp is already enabled in agents.json.")
        relink = _confirm("  Re-link WhatsApp (scan a new QR code)?", default=False)
        if relink:
            cmd_whatsapp_reset(data)
            session_ok = False

    # ── Step 2: allowed phone number ────────────────────────────────────
    print()
    print(_b("  Step 1 · Set your allowed phone number"))
    print("  Only messages from this number will be forwarded to agents.")
    print("  Format: +CountryCodeNumber  e.g. +6591234567")
    print()
    existing_nums = wa.get("allowed_numbers", [])
    if existing_nums:
        print(f"  Currently set: {', '.join(existing_nums)}")
        change = _confirm("  Change phone number?", default=False)
        if not change:
            allowed_numbers = existing_nums
        else:
            allowed_numbers = None
    else:
        allowed_numbers = None

    if allowed_numbers is None:
        while True:
            raw = _prompt("  Your WhatsApp phone number (with country code)")
            norm = _validate_phone(raw)
            if norm:
                allowed_numbers = [norm]
                break
            print(_y(f"    Invalid number '{raw}'. Example: +6591234567"))

    wa["allowed_numbers"] = allowed_numbers
    print(_g(f"  ✓ Allowed number set to: {', '.join(allowed_numbers)}"))

    # ── Step 3: default agent ───────────────────────────────────────────
    print()
    print(_b("  Step 2 · Default agent"))
    current_agent = wa.get("default_agent", "hashiko")
    # List active agents from agents.json
    agents_list = [a.get("name", "") for a in data.get("agents", []) if a.get("is_active")]
    if agents_list:
        print(f"  Active agents: {', '.join(agents_list)}")
    agent = _prompt("  Default agent to receive WhatsApp messages", default=current_agent)
    wa["default_agent"] = agent or current_agent
    print(_g(f"  ✓ Default agent: {wa['default_agent']}"))

    # ── Step 4: enable WhatsApp ──────────────────────────────────────────
    wa["enabled"] = True
    _save_agents_json(data, path)
    print()
    print(_g("  ✓ agents.json updated — WhatsApp enabled."))

    # ── Step 5: link (QR) if no session ─────────────────────────────────
    if not session_ok:
        print()
        print(_b("  Step 3 · Link your WhatsApp account"))
        print("  Make sure your phone has internet access.")
        print()
        sd = _session_dir(data)
        asyncio.run(_run_link_wizard(sd))
    else:
        print()
        print(_g("  ✓ Existing session will be reused — no QR scan needed."))

    # ── Done ─────────────────────────────────────────────────────────────
    print()
    _hr()
    print(f"  {_b('Setup complete!')}  🎉")
    print()
    print("  Next steps:")
    print(f"    • Restart HASHI (or run {_c('/reboot')}) to apply the new config.")
    print(f"    • Send a WhatsApp message from {_c(', '.join(allowed_numbers))} to test.")
    print(f"    • Use {_c('/agent <name>')} in WhatsApp to switch agents.")
    _hr()
    print()


# ──────────────────────────────────────────────
# enterprise backup / restore
# ──────────────────────────────────────────────

def _enterprise_backup_items(include_workspaces: bool = False):
    from orchestrator.enterprise import BackupItem

    items = [
        BackupItem("state/enterprise.sqlite", ROOT_DIR / "state" / "enterprise.sqlite", required=True),
        BackupItem("state/enterprise_audit.jsonl", ROOT_DIR / "state" / "enterprise_audit.jsonl"),
        BackupItem("agents.json", ROOT_DIR / "agents.json", required=True),
        BackupItem("agent_capabilities.json", ROOT_DIR / "agent_capabilities.json"),
    ]
    if include_workspaces:
        items.append(BackupItem("workspaces", ROOT_DIR / "workspaces"))
    return items


def _default_enterprise_backup_path() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ROOT_DIR / "backups" / f"hashi-enterprise-{stamp}.tar.gz"


def cmd_enterprise_backup(args) -> int:
    from orchestrator.enterprise import EnterpriseBackup

    output = Path(args.output).expanduser() if args.output else _default_enterprise_backup_path()
    result = EnterpriseBackup().create_archive(
        output,
        _enterprise_backup_items(include_workspaces=bool(args.include_workspaces)),
    )
    print(_g(f"✓ Enterprise backup written: {result.archive_path}"))
    print(f"  Backup id: {result.manifest['backup_id']}")
    included = [item["name"] for item in result.manifest["items"] if item["included"]]
    skipped = [item["name"] for item in result.manifest["items"] if not item["included"]]
    print(f"  Included : {', '.join(included) if included else '(none)'}")
    if skipped:
        print(f"  Skipped  : {', '.join(skipped)}")
    return 0


def cmd_enterprise_restore(args) -> int:
    from orchestrator.enterprise import EnterpriseBackup

    result = EnterpriseBackup().restore_archive(
        Path(args.archive).expanduser(),
        Path(args.destination).expanduser(),
        overwrite=bool(args.overwrite),
    )
    print(_g(f"✓ Enterprise backup restored to: {result['destination']}"))
    print(f"  Restored: {', '.join(result['restored']) if result['restored'] else '(none)'}")
    return 0


def cmd_enterprise_backup_inspect(args) -> int:
    from orchestrator.enterprise import EnterpriseBackup

    manifest = EnterpriseBackup().read_manifest(Path(args.archive).expanduser())
    print(json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_enterprise_migrate(args) -> int:
    from orchestrator.enterprise import EnterpriseStore

    db_path = Path(args.db).expanduser() if args.db else ROOT_DIR / "state" / "enterprise.sqlite"
    result = EnterpriseStore(db_path).migrate()
    print(_g(f"✓ Enterprise schema migrated: {result['db_path']}"))
    print(f"  Before: {result['before'] if result['before'] is not None else '(none)'}")
    print(f"  After : {result['after']}")
    return 0


def _parse_http_headers(raw_headers: list[str] | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw in raw_headers or []:
        if ":" not in raw:
            raise ValueError("HTTP headers must use 'Name: value' format")
        name, value = raw.split(":", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            raise ValueError("HTTP header name cannot be empty")
        headers[name] = value
    return headers


def _http_post_transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, str]:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read(4096).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(4096).decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise ValueError(f"audit live export endpoint is unreachable: {exc.reason}") from exc


def cmd_enterprise_audit_export_live(args) -> int:
    from orchestrator.enterprise import (
        AuditLiveExportEndpoint,
        AuditLiveExporter,
        EnterpriseAuditLedger,
        FileAuditLiveExportCheckpoint,
    )

    db_path = Path(args.db).expanduser() if args.db else ROOT_DIR / "state" / "enterprise.sqlite"
    checkpoint_path = (
        Path(args.checkpoint).expanduser()
        if args.checkpoint
        else ROOT_DIR / "state" / "audit_live_export_checkpoint.json"
    )
    ledger = EnterpriseAuditLedger.from_path(db_path, org_id=args.org_id)
    endpoint = AuditLiveExportEndpoint(
        url=args.endpoint,
        format=args.format,
        headers=_parse_http_headers(args.header),
        timeout_seconds=float(args.timeout),
        batch_size=int(args.batch_size),
    )
    cycle = AuditLiveExporter(ledger, transport=_http_post_transport).export_with_checkpoint(
        endpoint,
        FileAuditLiveExportCheckpoint(checkpoint_path),
        max_attempts=int(args.max_attempts),
        backoff_seconds=float(args.backoff),
    )
    print(_g("✓ Enterprise audit live export completed"))
    print(f"  Attempted       : {cycle.result.attempted}")
    print(f"  Sent            : {cycle.result.sent}")
    print(f"  Attempts        : {cycle.attempts}")
    print(f"  Last chain index: {cycle.result.last_chain_index}")
    print(f"  Status code     : {cycle.result.status_code if cycle.result.status_code is not None else '(no-op)'}")
    print(f"  Checkpoint      : {cycle.checkpoint_path}")
    return 0


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="hashi",
        description="HASHI command-line tool",
    )
    sub = parser.add_subparsers(dest="cmd")

    wa_parser = sub.add_parser("whatsapp", help="WhatsApp setup and management")
    wa_parser.add_argument(
        "action",
        nargs="?",
        choices=["status", "reset"],
        default=None,
        help="status = show config | reset = clear saved session | (none) = full wizard",
    )

    enterprise_parser = sub.add_parser("enterprise", help="Enterprise mode operations")
    enterprise_sub = enterprise_parser.add_subparsers(dest="enterprise_cmd")

    enterprise_backup = enterprise_sub.add_parser("backup", help="Create an enterprise backup archive")
    enterprise_backup.add_argument("--output", help="Backup archive path. Defaults to backups/hashi-enterprise-<utc>.tar.gz")
    enterprise_backup.add_argument(
        "--include-workspaces",
        action="store_true",
        help="Also include the workspaces directory. This may create a large archive.",
    )
    enterprise_backup.set_defaults(func=cmd_enterprise_backup)

    enterprise_restore = enterprise_sub.add_parser("restore", help="Restore an enterprise backup archive")
    enterprise_restore.add_argument("archive", help="Backup archive to restore")
    enterprise_restore.add_argument("destination", help="Destination directory")
    enterprise_restore.add_argument("--overwrite", action="store_true", help="Overwrite existing restore targets")
    enterprise_restore.set_defaults(func=cmd_enterprise_restore)

    enterprise_inspect = enterprise_sub.add_parser("inspect-backup", help="Print an enterprise backup manifest")
    enterprise_inspect.add_argument("archive", help="Backup archive to inspect")
    enterprise_inspect.set_defaults(func=cmd_enterprise_backup_inspect)

    enterprise_migrate = enterprise_sub.add_parser("migrate", help="Initialize or migrate the enterprise SQLite schema")
    enterprise_migrate.add_argument("--db", help="Enterprise SQLite path. Defaults to state/enterprise.sqlite")
    enterprise_migrate.set_defaults(func=cmd_enterprise_migrate)

    enterprise_audit_live = enterprise_sub.add_parser(
        "audit-export-live",
        help="Push new enterprise audit ledger events to an HTTP SIEM/OTLP endpoint",
    )
    enterprise_audit_live.add_argument("--endpoint", required=True, help="HTTP endpoint that accepts audit export payloads")
    enterprise_audit_live.add_argument(
        "--format",
        choices=["ledger", "siem", "otel", "splunk-hec", "elastic-bulk"],
        default="siem",
        help="Export payload format. Defaults to siem.",
    )
    enterprise_audit_live.add_argument("--db", help="Enterprise SQLite path. Defaults to state/enterprise.sqlite")
    enterprise_audit_live.add_argument("--org-id", default="ORG-001", help="Enterprise organization id. Defaults to ORG-001")
    enterprise_audit_live.add_argument(
        "--checkpoint",
        help="Checkpoint JSON path. Defaults to state/audit_live_export_checkpoint.json",
    )
    enterprise_audit_live.add_argument("--batch-size", type=int, default=100, help="Events to send per cycle")
    enterprise_audit_live.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds")
    enterprise_audit_live.add_argument("--max-attempts", type=int, default=3, help="Retry attempts before failing")
    enterprise_audit_live.add_argument("--backoff", type=float, default=1.0, help="Retry backoff seconds")
    enterprise_audit_live.add_argument(
        "--header",
        action="append",
        default=[],
        help="HTTP header in 'Name: value' format. Repeat for multiple headers.",
    )
    enterprise_audit_live.set_defaults(func=cmd_enterprise_audit_export_live)

    args = parser.parse_args()

    if args.cmd == "whatsapp":
        if args.action == "status":
            data, _ = _load_agents_json()
            cmd_whatsapp_status(data)
        elif args.action == "reset":
            data, _ = _load_agents_json()
            cmd_whatsapp_reset(data)
        else:
            cmd_whatsapp_wizard()
    elif args.cmd == "enterprise":
        if hasattr(args, "func"):
            raise SystemExit(args.func(args))
        enterprise_parser.print_help()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
