"""
hchat_send.py — CLI tool for agents to send Hchat messages to other agents.

Usage:
    python tools/hchat_send.py --to <agent_name> --from <sender_name> --text "<message>"
    python tools/hchat_send.py --to lily --from rain --text "Hi lily, I wanted to update you that..."

Sends a real-time message via the HASHI Workbench API (POST /api/chat).
Supports cross-instance delivery: discovers remote instance ports from instances.json
and routes messages to the correct Workbench API endpoint.

Identity and routing are separated:
  Message header: [hchat from rain@HASHI1] message text  (identity only, no IP/port)
  Routing info:   reply_route in API payload metadata     (resolved by infrastructure)

When the receiving Workbench sees reply_route in the payload, it auto-updates
the local contacts.json cache so future replies route correctly.

Cross-instance routing (priority order):
  1. Local agent  → local Workbench API (127.0.0.1:<local_port>)
  2. Contacts cache → previously learned route (with TTL expiry)
  3. Hashi Remote → /hchat endpoint on remote instance (internet-safe, only exposes port 8766)
  4. Direct Workbench → auto-discover via instances.json (LAN only)
"""

import argparse
import json
import sys
import time
from pathlib import Path
from urllib import request as urllib_request
from urllib.error import URLError

ROOT = Path(__file__).resolve().parent.parent
CONTACTS_FILE = ROOT / "contacts.json"
DEFAULT_TTL = 3600  # 1 hour — contacts expire and get re-discovered


# ─────────────────────────────────────────────────────────
# Contacts cache (learned routes with TTL)
# ─────────────────────────────────────────────────────────

def _load_contacts() -> dict:
    """Load local contacts cache (agent → {instance_id, host, port, wb_port, updated, expires})."""
    if CONTACTS_FILE.exists():
        try:
            return json.loads(CONTACTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_contacts(contacts: dict):
    """Save contacts cache."""
    CONTACTS_FILE.write_text(json.dumps(contacts, indent=2, ensure_ascii=False), encoding="utf-8")


def update_contact(agent_name: str, instance_id: str, host: str, port: int,
                   wb_port: int | None = None, ttl: int = DEFAULT_TTL):
    """Update a single agent's routing info in the local contacts cache."""
    contacts = _load_contacts()
    now = time.time()
    contacts[agent_name.lower()] = {
        "instance_id": instance_id,
        "host": host,
        "port": port,              # Hashi Remote port (8766) or workbench port
        "wb_port": wb_port or port, # Workbench port for direct delivery
        "updated": now,
        "expires": now + ttl,
    }
    _save_contacts(contacts)


def _get_cached_route(agent_name: str) -> dict | None:
    """Get a non-expired cached route for an agent."""
    contacts = _load_contacts()
    cached = contacts.get(agent_name.lower())
    if not cached:
        return None
    if cached.get("expires", 0) < time.time():
        return None  # expired
    return cached


def parse_return_address(hchat_header: str) -> dict | None:
    """Parse identity from hchat header.

    Input:  '[hchat from rain@HASHI1] ...'
    Returns: {'agent': 'rain', 'instance_id': 'HASHI1'}

    Also supports legacy format with IP:port (ignored, only identity extracted):
    Input:  '[hchat from rain@HASHI1:127.0.0.1:18800] ...'
    Returns: {'agent': 'rain', 'instance_id': 'HASHI1'}
    """
    import re
    # New format: agent@INSTANCE (no IP)
    m = re.match(r'\[hchat from (\w+)@(\w+)\]', hchat_header)
    if m:
        return {"agent": m.group(1), "instance_id": m.group(2)}
    # Legacy format: agent@INSTANCE:host:port
    m = re.match(r'\[hchat from (\w+)@(\w+):', hchat_header)
    if m:
        return {"agent": m.group(1), "instance_id": m.group(2)}
    return None


# ─────────────────────────────────────────────────────────
# Config loaders
# ─────────────────────────────────────────────────────────

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
    platform = instance_info.get("platform", "")
    if platform == "windows":
        wsl_root = instance_info.get("wsl_root")
        if not wsl_root:
            return []
        agents_path = Path(wsl_root) / "agents.json"
    else:
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

    Returns dict with 'instance_id', 'host', 'wb_port', 'remote_port' if found.
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

        wb_port = inst_info.get("workbench_port")
        if not wb_port:
            continue

        agents = _load_remote_agents(inst_id, inst_info)
        if target_agent.lower() in agents:
            # Prefer lan_ip (set by Hashi Remote mDNS discovery) over api_host
            host = inst_info.get("lan_ip") or inst_info.get("api_host", "127.0.0.1")
            candidates.append({
                "instance_id": inst_id.upper(),
                "host": host,
                "wb_port": wb_port,
                "remote_port": inst_info.get("remote_port", 8766),
            })

    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0]

    # Probe each candidate to find the live one
    for candidate in candidates:
        try:
            url = f"http://{candidate['host']}:{candidate['wb_port']}/api/chat"
            probe_req = urllib_request.Request(url, method="GET")
            urllib_request.urlopen(probe_req, timeout=3)
        except URLError:
            continue
        except Exception:
            return candidate

    return candidates[-1]


# ─────────────────────────────────────────────────────────
# Delivery methods
# ─────────────────────────────────────────────────────────

def _build_reply_route(cfg: dict) -> dict:
    """Build reply_route metadata for outgoing messages."""
    instance_id = _get_instance_id(cfg)
    host = cfg.get("global", {}).get("api_host", "127.0.0.1")
    # Use lan_ip from our own instance in instances.json if available
    instances = _load_instances()
    our_inst = instances.get(instance_id.lower(), {})
    if our_inst.get("lan_ip"):
        host = our_inst["lan_ip"]
    return {
        "instance_id": instance_id,
        "host": host,
        "port": our_inst.get("remote_port", 8766),
        "wb_port": _get_workbench_port(cfg),
        "ttl": DEFAULT_TTL,
    }


def _send_via_workbench(host: str, port: int, to_agent: str, from_agent: str,
                        text: str, instance_id: str,
                        reply_route: dict | None = None,
                        label: str = "local") -> bool:
    """Send via Workbench HTTP API (POST /api/chat)."""
    url = f"http://{host}:{port}/api/chat"
    full_text = f"[hchat from {from_agent}@{instance_id}] {text}"
    payload = {"agent": to_agent.lower(), "text": full_text}
    if reply_route:
        payload["reply_route"] = reply_route
    data = json.dumps(payload).encode("utf-8")

    req = urllib_request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

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


def _send_via_remote(host: str, port: int, to_agent: str, from_agent: str,
                     text: str, from_instance: str,
                     reply_route: dict | None = None) -> bool:
    """Send via Hashi Remote /hchat endpoint (internet-safe)."""
    url = f"http://{host}:{port}/hchat"
    full_text = f"[hchat from {from_agent}@{from_instance}] {text}"
    payload = {
        "from_instance": from_instance,
        "to_agent": to_agent.lower(),
        "text": full_text,
        "source_hchat_format": True,
    }
    if reply_route:
        payload["reply_route"] = reply_route
    data = json.dumps(payload).encode("utf-8")

    req = urllib_request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib_request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("ok"):
                print(f"✅ Hchat delivered (Remote, {host}:{port}): {from_agent} → {to_agent}")
                print(f"   Message: {text[:80]}{'...' if len(text) > 80 else ''}")
                return True
            else:
                print(f"❌ Remote /hchat error: {result.get('error', 'unknown')}", file=sys.stderr)
                return False
    except URLError as e:
        print(f"❌ Remote /hchat connection failed ({host}:{port}): {e}", file=sys.stderr)
        return False


# ─────────────────────────────────────────────────────────
# Group resolution
# ─────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────
# Main send logic
# ─────────────────────────────────────────────────────────

def send_hchat(to_agent: str, from_agent: str, text: str,
               target_instance: str | None = None) -> bool:
    cfg = _load_config()
    port = _get_workbench_port(cfg)
    instance_id = _get_instance_id(cfg)
    reply_route = _build_reply_route(cfg)

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
            print(f"ℹ️ {to_agent} → {remote['instance_id']} (port {remote['wb_port']})", file=sys.stderr)
            if _send_via_workbench(remote["host"], remote["wb_port"], to_agent, from_agent,
                                   text, instance_id, reply_route, label="remote"):
                return True
        print(f"❌ Failed to deliver to {to_agent}@{target_instance}. Target instance may be offline.", file=sys.stderr)
        return False

    # === 1. Local agent: send via local Workbench API ===
    if _is_local_agent(cfg, to_agent):
        if _send_via_workbench("127.0.0.1", port, to_agent, from_agent,
                               text, instance_id, reply_route):
            return True
        print(f"⚠️ Local API failed for {to_agent}, trying remote...", file=sys.stderr)

    # === 2. Contacts cache: check if we have a learned (non-expired) route ===
    cached = _get_cached_route(to_agent)
    if cached:
        print(f"ℹ️ {to_agent} found in contacts → {cached['instance_id']} ({cached['host']})", file=sys.stderr)
        # Try Remote /hchat first (internet-safe), then direct Workbench
        if cached.get("port") and cached["port"] != cached.get("wb_port"):
            if _send_via_remote(cached["host"], cached["port"], to_agent, from_agent,
                                text, instance_id, reply_route):
                return True
        if cached.get("wb_port"):
            if _send_via_workbench(cached["host"], cached["wb_port"], to_agent, from_agent,
                                   text, instance_id, reply_route, label="cached"):
                return True
        print(f"⚠️ Contacts cache route failed, falling back to discovery...", file=sys.stderr)

    # === 3. Instance discovery → try Remote /hchat then direct Workbench ===
    remote = _find_remote_instance(to_agent, instance_id)
    if remote:
        print(f"ℹ️ {to_agent} found on {remote['instance_id']} ({remote['host']})", file=sys.stderr)
        # Try Remote /hchat endpoint first (internet-safe)
        if remote.get("remote_port"):
            if _send_via_remote(remote["host"], remote["remote_port"], to_agent, from_agent,
                                text, instance_id, reply_route):
                return True
        # Fall back to direct Workbench
        if _send_via_workbench(remote["host"], remote["wb_port"], to_agent, from_agent,
                               text, instance_id, reply_route, label="remote"):
            return True
        print(f"❌ Remote delivery to {remote['instance_id']} failed.", file=sys.stderr)
        return False

    # === 4. Fallback: try local API (might be a dynamic agent) ===
    if _send_via_workbench("127.0.0.1", port, to_agent, from_agent,
                           text, instance_id, reply_route):
        return True

    print(f"❌ Could not deliver message to {to_agent}. Agent not found on any active instance.", file=sys.stderr)
    return False


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Send a Hchat message to another agent")
    parser.add_argument("--to", help="Target agent name or @group_name (e.g. lily or @staff)")
    parser.add_argument("--from", dest="from_agent", help="Sender agent name (e.g. rain)")
    parser.add_argument("--text", help="Message text to send")
    parser.add_argument("--instance", default=None, help="Target instance (e.g. HASHI9) — forces routing to specific instance")
    args = parser.parse_args()

    if not args.to or not args.from_agent or not args.text:
        parser.error("--to, --from, and --text are required for sending messages")

    success = send_hchat(args.to, args.from_agent, args.text, target_instance=args.instance)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
