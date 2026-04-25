"""
hchat_send.py - CLI tool for agents to send Hchat messages to other agents.

Usage:
    python tools/hchat_send.py --to <agent_name> --from <sender_name> --text "<message>"
    python tools/hchat_send.py --to lily --from rain --text "Hi lily, I wanted to update you that..."

Hchat protocol rules:
  1. Workbench /api/chat is the primary delivery surface.
  2. instances.json + agents.json + live health are authoritative.
  3. contacts.json is only a short-lived learned cache.
  4. Hashi Remote /hchat is a restricted-network fallback, not the normal path.
  5. Mailbox is retired and must not be used for delivery.

Cross-instance routing (priority order):
  1. Local agent -> local Workbench API
  2. Contacts cache -> refreshed against instances.json before use
  3. Instance discovery -> direct Workbench using instances.json + live health
  4. Remote /hchat -> only when direct Workbench is unavailable or a forced target needs relay
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent.parent
CONTACTS_FILE = ROOT / "contacts.json"
INSTANCES_FILE = ROOT / "instances.json"
DEFAULT_TTL = 3600
DEFAULT_REMOTE_PORT = 8766


def _infer_instance_id_from_root() -> str:
    root_str = str(ROOT).replace("\\", "/").lower()
    if root_str.endswith("/projects/hashi2"):
        return "HASHI2"
    if root_str.endswith("/projects/hashi9"):
        return "HASHI9"
    if root_str.endswith("/projects/hashi"):
        return "HASHI1" if os.name != "nt" else "HASHI9"
    return "HASHI1"


def _linux_root_to_local_path(root: str) -> Path:
    if root.startswith("/home/") and os.name == "nt":
        parts = [p for p in root.strip("/").split("/") if p]
        path = Path(r"\\wsl.localhost\Ubuntu-22.04")
        for part in parts:
            path /= part
        return path
    return Path(root)


def _default_hashi9_paths() -> tuple[str, str]:
    explicit_windows = os.getenv("HASHI9_ROOT")
    explicit_wsl = os.getenv("HASHI9_WSL_ROOT")
    if explicit_windows and explicit_wsl:
        return explicit_windows, explicit_wsl

    for candidate in Path("/mnt/c/Users").glob("*/projects/HASHI"):
        if candidate.is_dir():
            user = candidate.parts[4]
            windows_root = explicit_windows or f"C:\\Users\\{user}\\projects\\HASHI"
            return windows_root, explicit_wsl or str(candidate)

    username = os.getenv("USERNAME") or os.getenv("USER") or "<user>"
    windows_root = explicit_windows or f"C:\\Users\\{username}\\projects\\HASHI"
    wsl_root = explicit_wsl or f"/mnt/c/Users/{username}/projects/HASHI"
    return windows_root, wsl_root


def _load_config() -> dict:
    config_path = ROOT / "agents.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {}


def _temporary_default_instances(cfg: dict) -> dict:
    local_instance_id = cfg.get("global", {}).get("instance_id") or _infer_instance_id_from_root()
    local_workbench = cfg.get("global", {}).get("workbench_port", 18819 if local_instance_id == "HASHI9" else 18800)
    hashi9_root, hashi9_wsl_root = _default_hashi9_paths()
    return {
        "hashi1": {
            "instance_id": "HASHI1",
            "display_name": "HASHI1",
            "platform": "wsl",
            "root": "/home/lily/projects/hashi",
            "workbench_port": 18800,
            "api_host": "127.0.0.1",
            "remote_port": DEFAULT_REMOTE_PORT,
            "active": True,
            "_temporary_default": True,
        },
        "hashi2": {
            "instance_id": "HASHI2",
            "display_name": "HASHI2",
            "platform": "wsl",
            "root": "/home/lily/projects/hashi2",
            "workbench_port": 18802,
            "api_host": "127.0.0.1",
            "remote_port": DEFAULT_REMOTE_PORT,
            "active": True,
            "_temporary_default": True,
        },
        "hashi9": {
            "instance_id": "HASHI9",
            "display_name": "HASHI9",
            "platform": "windows",
            "root": hashi9_root,
            "wsl_root": hashi9_wsl_root,
            "workbench_port": local_workbench if local_instance_id.upper() == "HASHI9" else 18819,
            "api_host": "127.0.0.1",
            "remote_port": DEFAULT_REMOTE_PORT,
            "active": True,
            "_temporary_default": True,
        },
    }


def _load_instances() -> dict:
    cfg = _load_config()
    defaults = _temporary_default_instances(cfg)
    if INSTANCES_FILE.exists():
        try:
            data = json.loads(INSTANCES_FILE.read_text(encoding="utf-8-sig"))
            instances = data.get("instances", {})
            merged = defaults.copy()
            merged.update(instances)
            return merged
        except Exception:
            pass
    return defaults


def _get_workbench_port(cfg: dict) -> int:
    return cfg.get("global", {}).get("workbench_port", 18800)


def _get_instance_id(cfg: dict) -> str:
    return cfg.get("global", {}).get("instance_id") or _infer_instance_id_from_root()


def _is_local_agent(cfg: dict, agent_name: str) -> bool:
    for agent in cfg.get("agents", []):
        if agent.get("name", "").lower() == agent_name.lower():
            return True
    return False


def _load_contacts() -> dict:
    if CONTACTS_FILE.exists():
        try:
            return json.loads(CONTACTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_contacts(contacts: dict) -> None:
    CONTACTS_FILE.write_text(json.dumps(contacts, indent=2, ensure_ascii=False), encoding="utf-8")


def update_contact(
    agent_name: str,
    instance_id: str,
    host: str,
    port: int,
    wb_port: int | None = None,
    ttl: int = DEFAULT_TTL,
) -> None:
    contacts = _load_contacts()
    now = time.time()
    contacts[agent_name.lower()] = {
        "instance_id": instance_id,
        "host": host,
        "port": port,
        "wb_port": wb_port or port,
        "updated": now,
        "expires": now + ttl,
    }
    _save_contacts(contacts)


def _normalized_cached_route(agent_name: str, cached: dict) -> dict:
    instance_id = str(cached.get("instance_id", "")).lower()
    authoritative = _load_instances().get(instance_id)
    if not authoritative:
        return cached

    updated = dict(cached)
    authoritative_host = (
        authoritative.get("lan_ip")
        or authoritative.get("tailscale_ip")
        or authoritative.get("internet_host")
        or authoritative.get("api_host")
        or cached.get("host")
    )
    authoritative_wb = authoritative.get("workbench_port", cached.get("wb_port"))
    authoritative_remote = authoritative.get("remote_port", cached.get("port", DEFAULT_REMOTE_PORT))

    changed = False
    if authoritative_host and authoritative_host != updated.get("host"):
        updated["host"] = authoritative_host
        changed = True
    if authoritative_wb and authoritative_wb != updated.get("wb_port"):
        updated["wb_port"] = authoritative_wb
        changed = True
    if authoritative_remote and authoritative_remote != updated.get("port"):
        updated["port"] = authoritative_remote
        changed = True

    if changed:
        contacts = _load_contacts()
        contacts[agent_name.lower()] = updated
        _save_contacts(contacts)
    return updated


def _get_cached_route(agent_name: str) -> dict | None:
    contacts = _load_contacts()
    cached = contacts.get(agent_name.lower())
    if not cached:
        return None
    if cached.get("expires", 0) < time.time():
        return None
    return _normalized_cached_route(agent_name, cached)


def parse_return_address(hchat_header: str) -> dict | None:
    m = re.match(r"\[hchat from (\w+)@(\w+)\]", hchat_header)
    if m:
        return {"agent": m.group(1), "instance_id": m.group(2)}
    m = re.match(r"\[hchat from (\w+)@(\w+):", hchat_header)
    if m:
        return {"agent": m.group(1), "instance_id": m.group(2)}
    return None


def _load_remote_agents(instance_id: str, instance_info: dict) -> list[str]:
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
        agents_path = _linux_root_to_local_path(root) / "agents.json"

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


def _preferred_host(instance_info: dict, *, for_remote: bool = False) -> str:
    if for_remote:
        keys = ["internet_host", "tailscale_ip", "lan_ip", "api_host", "host"]
    else:
        keys = ["lan_ip", "tailscale_ip", "api_host", "internet_host", "host"]
    for key in keys:
        value = instance_info.get(key)
        if value:
            return value
    return "127.0.0.1"


def _probe_http(url: str, timeout: int = 3) -> bool:
    try:
        req = urllib_request.Request(url, method="GET")
        urllib_request.urlopen(req, timeout=timeout)
        return True
    except HTTPError:
        return True
    except URLError:
        return False
    except Exception:
        return True


def _probe_workbench(host: str, port: int) -> bool:
    return _probe_http(f"http://{host}:{port}/api/chat")


def _probe_remote(host: str, port: int) -> bool:
    return _probe_http(f"http://{host}:{port}/health")


def _find_remote_instance(
    target_agent: str,
    local_instance_id: str,
    target_instance: str | None = None,
) -> dict | None:
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

        if target_instance:
            matches = True
        else:
            agents = _load_remote_agents(inst_id, inst_info)
            matches = target_agent.lower() in agents

        if not matches:
            continue

        candidates.append(
            {
                "instance_id": inst_info.get("instance_id", inst_id).upper(),
                "host": _preferred_host(inst_info),
                "wb_port": wb_port,
                "remote_host": _preferred_host(inst_info, for_remote=True),
                "remote_port": inst_info.get("remote_port", DEFAULT_REMOTE_PORT),
            }
        )

    if not candidates:
        return None

    for candidate in candidates:
        if _probe_workbench(candidate["host"], candidate["wb_port"]):
            return candidate

    return candidates[0]


def _build_reply_route(cfg: dict) -> dict:
    instance_id = _get_instance_id(cfg)
    instances = _load_instances()
    our_inst = instances.get(instance_id.lower(), {})
    host = _preferred_host(our_inst, for_remote=True)
    if host == "127.0.0.1":
        host = cfg.get("global", {}).get("api_host", "127.0.0.1")
    return {
        "instance_id": instance_id,
        "host": host,
        "port": our_inst.get("remote_port", cfg.get("global", {}).get("remote_port", DEFAULT_REMOTE_PORT)),
        "wb_port": _get_workbench_port(cfg),
        "ttl": DEFAULT_TTL,
    }


def _send_via_workbench(
    host: str,
    port: int,
    to_agent: str,
    from_agent: str,
    text: str,
    instance_id: str,
    reply_route: dict | None = None,
    label: str = "local",
) -> bool:
    url = f"http://{host}:{port}/api/chat"
    full_text = f"[hchat from {from_agent}@{instance_id}] {text}"
    payload = {"agent": to_agent.lower(), "text": full_text}
    if reply_route:
        payload["reply_route"] = reply_route
    data = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=data,
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
            print(f"❌ Hchat API error: {result.get('error', 'unknown')}", file=sys.stderr)
            return False
    except URLError as e:
        print(f"❌ Hchat {label} API connection failed ({host}:{port}): {e}", file=sys.stderr)
        return False


def _send_via_remote(
    host: str,
    port: int,
    to_agent: str,
    from_agent: str,
    text: str,
    from_instance: str,
    reply_route: dict | None = None,
) -> bool:
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
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("ok"):
                print(f"✅ Hchat delivered (Remote fallback, {host}:{port}): {from_agent} → {to_agent}")
                print(f"   Message: {text[:80]}{'...' if len(text) > 80 else ''}")
                return True
            print(f"❌ Remote /hchat error: {result.get('error', 'unknown')}", file=sys.stderr)
            return False
    except URLError as e:
        print(f"❌ Remote /hchat connection failed ({host}:{port}): {e}", file=sys.stderr)
        return False


def _resolve_group_members(cfg: dict, group_name: str, exclude_self: str | None = None) -> list[str]:
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


def _deliver_remote_route(
    route: dict,
    to_agent: str,
    from_agent: str,
    text: str,
    instance_id: str,
    reply_route: dict,
    cache_label: str,
) -> bool:
    host = route.get("host")
    wb_port = route.get("wb_port")
    remote_host = route.get("remote_host", host)
    remote_port = route.get("remote_port") or route.get("port")

    if host and wb_port and _probe_workbench(host, wb_port):
        if _send_via_workbench(host, wb_port, to_agent, from_agent, text, instance_id, reply_route, label=cache_label):
            update_contact(to_agent, route["instance_id"], host, remote_port or DEFAULT_REMOTE_PORT, wb_port=wb_port)
            return True

    if remote_host and remote_port and _probe_remote(remote_host, remote_port):
        if _send_via_remote(remote_host, remote_port, to_agent, from_agent, text, instance_id, reply_route):
            update_contact(to_agent, route["instance_id"], remote_host, remote_port, wb_port=wb_port or remote_port)
            return True

    if host and wb_port:
        if _send_via_workbench(host, wb_port, to_agent, from_agent, text, instance_id, reply_route, label=cache_label):
            update_contact(to_agent, route["instance_id"], host, remote_port or DEFAULT_REMOTE_PORT, wb_port=wb_port)
            return True

    if remote_host and remote_port:
        if _send_via_remote(remote_host, remote_port, to_agent, from_agent, text, instance_id, reply_route):
            update_contact(to_agent, route["instance_id"], remote_host, remote_port, wb_port=wb_port or remote_port)
            return True

    return False


def send_hchat(to_agent: str, from_agent: str, text: str, target_instance: str | None = None) -> bool:
    cfg = _load_config()
    local_port = _get_workbench_port(cfg)
    instance_id = _get_instance_id(cfg)
    reply_route = _build_reply_route(cfg)

    if to_agent.startswith("@"):
        group_name = to_agent[1:]
        members = _resolve_group_members(cfg, group_name, exclude_self=from_agent)
        if not members:
            print(f"❌ Group '{group_name}' not found or has no members.", file=sys.stderr)
            return False
        results = [send_hchat(member, from_agent, text, target_instance=target_instance) for member in members]
        succeeded = sum(results)
        print(f"📢 Group @{group_name}: {succeeded}/{len(members)} delivered.")
        return succeeded > 0

    if not target_instance or target_instance.upper() == instance_id.upper():
        if _is_local_agent(cfg, to_agent):
            if _send_via_workbench("127.0.0.1", local_port, to_agent, from_agent, text, instance_id, reply_route):
                return True
            print(f"⚠️ Local API failed for {to_agent}, falling back to external routing...", file=sys.stderr)

    cached = _get_cached_route(to_agent)
    if cached and (not target_instance or cached.get("instance_id", "").upper() == target_instance.upper()):
        cached_route = {
            "instance_id": cached["instance_id"],
            "host": cached["host"],
            "wb_port": cached.get("wb_port"),
            "remote_host": cached.get("host"),
            "remote_port": cached.get("port"),
        }
        print(f"ℹ️ {to_agent} found in contacts → {cached_route['instance_id']} ({cached_route['host']})", file=sys.stderr)
        if _deliver_remote_route(cached_route, to_agent, from_agent, text, instance_id, reply_route, "cached"):
            return True
        print("⚠️ Contacts cache route failed, falling back to discovery...", file=sys.stderr)

    remote = _find_remote_instance(to_agent, instance_id, target_instance=target_instance)
    if remote:
        print(f"ℹ️ {to_agent} found on {remote['instance_id']} ({remote['host']})", file=sys.stderr)
        if _deliver_remote_route(remote, to_agent, from_agent, text, instance_id, reply_route, "remote"):
            return True
        print(f"❌ Remote delivery to {remote['instance_id']} failed.", file=sys.stderr)
        return False

    if not target_instance or target_instance.upper() == instance_id.upper():
        if _send_via_workbench("127.0.0.1", local_port, to_agent, from_agent, text, instance_id, reply_route):
            return True

    if target_instance:
        print(f"❌ Failed to deliver to {to_agent}@{target_instance}. Target instance may be offline.", file=sys.stderr)
    else:
        print(f"❌ Could not deliver message to {to_agent}. Agent not found on any active instance.", file=sys.stderr)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a Hchat message to another agent")
    parser.add_argument("--to", help="Target agent name or @group_name (e.g. lily or @staff)")
    parser.add_argument("--from", dest="from_agent", help="Sender agent name (e.g. rain)")
    parser.add_argument("--text", help="Message text to send")
    parser.add_argument("--instance", default=None, help="Target instance (e.g. HASHI9) - forces routing to specific instance")
    args = parser.parse_args()

    if not args.to or not args.from_agent or not args.text:
        parser.error("--to, --from, and --text are required for sending messages")

    success = send_hchat(args.to, args.from_agent, args.text, target_instance=args.instance)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
