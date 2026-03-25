"""
hchat_send.py — CLI tool for agents to send Hchat messages to other agents.

Usage:
    python tools/hchat_send.py --to <agent_name> --from <sender_name> --text "<message>"
    python tools/hchat_send.py --to lily --from rain --text "Hi lily, I wanted to update you that..."

This is intended to be called by an LLM agent via bash after composing the message.
The message is delivered to the target agent's queue and triggers a Telegram reply.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


async def send_hchat(to_agent: str, from_agent: str, text: str) -> bool:
    try:
        from orchestrator.agent_runtime import UniversalOrchestrator
    except ImportError:
        print(f"ERROR: Could not import orchestrator.", file=sys.stderr)
        return False

    # Load config to find the orchestrator instance
    config_path = ROOT / "agents.json"
    if not config_path.exists():
        print(f"ERROR: agents.json not found at {config_path}", file=sys.stderr)
        return False

    # Try to connect to the running orchestrator via its internal state
    # We use a file-based queue approach as fallback
    queue_file = ROOT / "mailbox" / "hchat_outbox.jsonl"
    queue_file.parent.mkdir(exist_ok=True)

    import time
    entry = {
        "to": to_agent.lower(),
        "from": from_agent.lower(),
        "text": text,
        "ts": time.time(),
    }
    with open(queue_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print(f"✅ Hchat queued: {from_agent} → {to_agent}")
    print(f"   Message: {text[:80]}{'...' if len(text) > 80 else ''}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Send a Hchat message to another agent")
    parser.add_argument("--to", required=True, help="Target agent name (e.g. lily)")
    parser.add_argument("--from", dest="from_agent", required=True, help="Sender agent name (e.g. rain)")
    parser.add_argument("--text", required=True, help="Message text to send")
    args = parser.parse_args()

    success = asyncio.run(send_hchat(args.to, args.from_agent, args.text))
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
