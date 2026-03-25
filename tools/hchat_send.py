"""
hchat_send.py — CLI tool for agents to send Hchat messages to other agents.

Usage:
    python tools/hchat_send.py --to <agent_name> --from <sender_name> --text "<message>"
    python tools/hchat_send.py --to lily --from rain --text "Hi lily, I wanted to update you that..."

Sends a real-time message via the HASHI Workbench API (POST /api/chat).
Falls back to Cross-Instance Mailbox if the target agent is not on this instance.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request as urllib_request
from urllib.error import URLError

ROOT = Path(__file__).resolve().parent.parent

# Known instance mailbox paths (from WSL)
INSTANCE_MAILBOX = {
    "HASHI1": Path("/home/lily/projects/hashi/mailbox/incoming/"),
    "HASHI2": Path("/home/lily/projects/hashi2/mailbox/incoming/"),
    "HASHI9": Path("/mnt/c/Users/thene/projects/HASHI/mailbox/incoming/"),
}


def _load_config():
    config_path = ROOT / "agents.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {}


def _get_workbench_port(cfg: dict) -> int:
    return cfg.get("global", {}).get("workbench_port", 18800)


def _get_instance_id(cfg: dict) -> str:
    return cfg.get("global", {}).get("instance_id", "HASHI1")


def _is_local_agent(cfg: dict, agent_name: str) -> bool:
    for agent in cfg.get("agents", []):
        if agent.get("name", "").lower() == agent_name.lower():
            return True
    return False


def _send_via_api(port: int, to_agent: str, from_agent: str, text: str) -> bool:
    """Send via local Workbench HTTP API (real-time, same instance)."""
    url = f"http://127.0.0.1:{port}/api/chat"
    full_text = f"[hchat from {from_agent}] {text}"
    payload = json.dumps({"agent": to_agent.lower(), "text": full_text}).encode("utf-8")

    req = urllib_request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib_request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("ok"):
                print(f"✅ Hchat delivered (real-time): {from_agent} → {to_agent}")
                print(f"   Message: {text[:80]}{'...' if len(text) > 80 else ''}")
                return True
            else:
                print(f"❌ Hchat API error: {result.get('error', 'unknown')}", file=sys.stderr)
                return False
    except URLError as e:
        print(f"❌ Hchat connection failed: {e}", file=sys.stderr)
        return False


def _send_via_mailbox(from_instance: str, from_agent: str, to_agent: str, text: str) -> bool:
    """Send via Cross-Instance Mailbox (async fallback)."""
    # Try all instances' mailbox paths
    now = datetime.now(timezone.utc)
    ts_str = now.strftime("%Y%m%d-%H%M%S")
    msg_id = f"hchat-{ts_str}-{from_agent}-{to_agent}"

    message = {
        "msg_id": msg_id,
        "from_instance": from_instance,
        "from_agent": from_agent,
        "to_agent": to_agent,
        "intent": "ask",
        "reply_required": True,
        "text": f"[hchat from {from_agent}] {text}",
        "ts": now.isoformat(),
    }

    # Try each instance's mailbox (skip our own)
    for instance_id, mailbox_path in INSTANCE_MAILBOX.items():
        if instance_id == from_instance:
            continue
        if mailbox_path.exists():
            try:
                message["to_instance"] = instance_id
                filename = f"{ts_str}_{from_instance}_{from_agent}.json"
                target = mailbox_path / filename
                # Atomic write via temp file
                tmp = target.with_suffix(".tmp")
                tmp.write_text(json.dumps(message, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.rename(target)
                print(f"✅ Hchat queued (mailbox → {instance_id}): {from_agent} → {to_agent}")
                print(f"   Message: {text[:80]}{'...' if len(text) > 80 else ''}")
                return True
            except Exception as e:
                print(f"⚠️ Mailbox write to {instance_id} failed: {e}", file=sys.stderr)

    print(f"❌ No reachable mailbox found for {to_agent}", file=sys.stderr)
    return False


def send_hchat(to_agent: str, from_agent: str, text: str) -> bool:
    cfg = _load_config()
    port = _get_workbench_port(cfg)
    instance_id = _get_instance_id(cfg)

    # Try local API first
    if _is_local_agent(cfg, to_agent):
        return _send_via_api(port, to_agent, from_agent, text)

    # Agent not in local config — try API anyway (might be a dynamic agent)
    if _send_via_api(port, to_agent, from_agent, text):
        return True

    # Fallback to cross-instance mailbox
    print(f"ℹ️ {to_agent} not found locally, trying cross-instance mailbox...", file=sys.stderr)
    return _send_via_mailbox(instance_id, from_agent, to_agent, text)


def main():
    parser = argparse.ArgumentParser(description="Send a Hchat message to another agent")
    parser.add_argument("--to", required=True, help="Target agent name (e.g. lily)")
    parser.add_argument("--from", dest="from_agent", required=True, help="Sender agent name (e.g. rain)")
    parser.add_argument("--text", required=True, help="Message text to send")
    args = parser.parse_args()

    success = send_hchat(args.to, args.from_agent, args.text)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
