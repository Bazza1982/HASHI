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
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
