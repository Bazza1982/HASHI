"""
hchat_send.py — CLI tool for agents to send Hchat messages to other agents.

Usage:
    python tools/hchat_send.py --to <agent_name> --from <sender_name> --text "<message>"
    python tools/hchat_send.py --to lily --from rain --text "Hi lily, I wanted to update you that..."

Sends a real-time message via the HASHI Workbench API (POST /api/chat).
Supports cross-instance delivery: discovers remote instance ports from instances.json
and routes messages to the correct Workbench API endpoint.

Falls back to Cross-Instance Mailbox only if API delivery fails.
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

# Known instance mailbox paths (from WSL) — fallback only
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


def _load_instances():
    """Load cross-instance registry from instances.json."""
    instances_path = ROOT / "instances.json"
    if instances_path.exists():
        try:
            data = json.loads(instances_path.read_text(encoding="utf-8-sig"))
            return data.get("instances", {})
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


def _load_remote_agents(instance_id: str, instance_info: dict) -> list[str]:
    """Load agent names from a remote instance's agents.json."""
    # Determine the path to the remote instance's agents.json
    platform = instance_info.get("platform", "")
    if platform == "windows":
        # Access Windows instance via WSL mount
        wsl_root = instance_info.get("wsl_root")
        if not wsl_root:
            return []
        agents_path = Path(wsl_root) / "agents.json"
    else:
        # WSL instance — direct path
        root = instance_info.get("root")
        if not root:
            return []
        agents_path = Path(root) / "agents.json"

    if not agents_path.exists():
        return []

    try:
        data = json.loads(agents_path.read_text(encoding="utf-8-sig"))
        return [
            a["name"].lower()
            for a in data.get("agents", [])
            if a.get("is_active", True)
        ]
    except Exception:
        return []


def _find_remote_instance(target_agent: str, local_instance_id: str,
                          target_instance: str | None = None) -> dict | None:
    """Find which remote instance hosts the target agent.

    If target_instance is specified, only check that instance.
    Returns dict with 'instance_id', 'host', 'port' if found, else None.
    """
    instances = _load_instances()
    candidates = []

    for inst_id, inst_info in instances.items():
        if inst_id.upper() == local_instance_id.upper():
            continue
        if not inst_info.get("active", False):
            continue
        if target_instance and inst_id.upper() != target_instance.upper():
            continue

        port = inst_info.get("workbench_port")
        if not port:
            continue

        # Check if this instance has the target agent
        agents = _load_remote_agents(inst_id, inst_info)
        if target_agent.lower() in agents:
            candidates.append({
                "instance_id": inst_id.upper(),
                "host": inst_info.get("api_host", "127.0.0.1"),
                "port": port,
            })

    if not candidates:
        return None

    # If multiple instances have the same agent, try to verify which is alive
    if len(candidates) == 1:
        return candidates[0]

    # Probe each candidate's API to find the live one
    for candidate in candidates:
        try:
            url = f"http://{candidate['host']}:{candidate['port']}/api/chat"
            # Quick probe — just check if the port responds
            probe_req = urllib_request.Request(url, method="GET")
            urllib_request.urlopen(probe_req, timeout=3)
        except URLError:
            continue
        except Exception:
            # Any response (even 405 Method Not Allowed) means the server is alive
            return candidate

    # If no probe succeeded, return the last candidate (prefer higher instance numbers = HASHI9 > HASHI2)
    return candidates[-1]


def _send_via_api(host: str, port: int, to_agent: str, from_agent: str, text: str,
                  is_remote: bool = False) -> bool:
    """Send via Workbench HTTP API (real-time)."""
    url = f"http://{host}:{port}/api/chat"
    full_text = f"[hchat from {from_agent}] {text}"
    payload = json.dumps({"agent": to_agent.lower(), "text": full_text}).encode("utf-8")

    req = urllib_request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    label = "remote" if is_remote else "local"
    try:
        with urllib_request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("ok"):
                print(f"✅ Hchat delivered ({label} API, {host}:{port}): {from_agent} → {to_agent}")
                print(f"   Message: {text[:80]}{'...' if len(text) > 80 else ''}")
                return True
            else:
                print(f"❌ Hchat API error: {result.get('error', 'unknown')}", file=sys.stderr)
                return False
    except URLError as e:
        print(f"❌ Hchat {label} API connection failed ({host}:{port}): {e}", file=sys.stderr)
        return False


def _send_via_mailbox(from_instance: str, from_agent: str, to_agent: str, text: str) -> bool:
    """Send via Cross-Instance Mailbox (async fallback)."""
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

    for instance_id, mailbox_path in INSTANCE_MAILBOX.items():
        if instance_id == from_instance:
            continue
        if mailbox_path.exists():
            try:
                message["to_instance"] = instance_id
                filename = f"{ts_str}_{from_instance}_{from_agent}.json"
                target = mailbox_path / filename
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


def _resolve_group_members(cfg: dict, group_name: str, exclude_self: str | None = None) -> list[str]:
    """Resolve a group name to a list of agent names."""
    groups = cfg.get("groups", {})
    group = groups.get(group_name)
    if group is None:
        return []
    members = group.get("members", [])
    excludes = {e.lower() for e in group.get("exclude_from_broadcast", [])}
    if exclude_self:
        excludes.add(exclude_self.lower())
    if members == "@active":
        return [a["name"] for a in cfg.get("agents", []) if a.get("is_active", True) and a["name"].lower() not in excludes]
    return [n for n in members if n.lower() not in excludes]


def send_hchat(to_agent: str, from_agent: str, text: str,
               target_instance: str | None = None) -> bool:
    cfg = _load_config()
    port = _get_workbench_port(cfg)
    instance_id = _get_instance_id(cfg)

    # Handle @group_name — expand to all members and send to each
    if to_agent.startswith("@"):
        group_name = to_agent[1:]
        members = _resolve_group_members(cfg, group_name, exclude_self=from_agent)
        if not members:
            print(f"❌ Group '{group_name}' not found or has no members.", file=sys.stderr)
            return False
        results = []
        for member in members:
            ok = send_hchat(member, from_agent, text, target_instance=target_instance)
            results.append(ok)
        succeeded = sum(results)
        print(f"📢 Group @{group_name}: {succeeded}/{len(members)} delivered.")
        return succeeded > 0

    # === If target instance specified, skip local check and go straight to remote ===
    if target_instance and target_instance.upper() != instance_id.upper():
        remote = _find_remote_instance(to_agent, instance_id, target_instance=target_instance)
        if remote:
            print(f"ℹ️ {to_agent} → {remote['instance_id']} (port {remote['port']})", file=sys.stderr)
            if _send_via_api(remote["host"], remote["port"], to_agent, from_agent, text, is_remote=True):
                return True
            print(f"⚠️ Remote API failed, falling back to mailbox...", file=sys.stderr)
        return _send_via_mailbox(instance_id, from_agent, to_agent, text)

    # === Local agent: send via local API, fall through on failure ===
    if _is_local_agent(cfg, to_agent):
        if _send_via_api("127.0.0.1", port, to_agent, from_agent, text):
            return True
        print(f"⚠️ Local API failed for {to_agent}, trying remote...", file=sys.stderr)

    # === Remote agent: discover instance and send via remote API ===
    remote = _find_remote_instance(to_agent, instance_id)
    if remote:
        print(f"ℹ️ {to_agent} found on {remote['instance_id']} (port {remote['port']})", file=sys.stderr)
        if _send_via_api(remote["host"], remote["port"], to_agent, from_agent, text, is_remote=True):
            return True
        print(f"⚠️ Remote API failed, falling back to mailbox...", file=sys.stderr)

    # === Fallback: try local API (might be a dynamic agent) ===
    if not remote and _send_via_api("127.0.0.1", port, to_agent, from_agent, text):
        return True

    # === Last resort: cross-instance mailbox ===
    print(f"ℹ️ Falling back to cross-instance mailbox...", file=sys.stderr)
    return _send_via_mailbox(instance_id, from_agent, to_agent, text)


def main():
    parser = argparse.ArgumentParser(description="Send a Hchat message to another agent")
    parser.add_argument("--to", required=True, help="Target agent name or @group_name (e.g. lily or @staff)")
    parser.add_argument("--from", dest="from_agent", required=True, help="Sender agent name (e.g. rain)")
    parser.add_argument("--text", required=True, help="Message text to send")
    parser.add_argument("--instance", default=None, help="Target instance (e.g. HASHI9) — forces routing to specific instance")
    args = parser.parse_args()

    success = send_hchat(args.to, args.from_agent, args.text, target_instance=args.instance)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
