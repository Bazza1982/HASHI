"""
protocol_send.py - Send a Hashi Remote protocol message to agent@INSTANCE.

Usage:
    python tools/protocol_send.py --to hashiko@HASHI9 --from lin_yueru --text "hello"
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib import request as urllib_request
from urllib.error import URLError

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.hchat_send import (
    _find_remote_instance,
    _get_instance_id,
    _load_instances,
    _load_config,
    _normalize_instance_id,
    _split_target_address,
)


def _post_json(url: str, payload: dict, timeout: int = 10) -> dict:
    req = urllib_request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib_request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _probe_remote_http(host: str, port: int, timeout: int = 3) -> bool:
    req = urllib_request.Request(f"http://{host}:{port}/health", method="GET")
    try:
        with urllib_request.urlopen(req, timeout=timeout):
            return True
    except URLError:
        return False
    except Exception:
        return True


def send_protocol_message(
    target: str,
    from_agent: str,
    text: str,
    *,
    target_instance: str | None = None,
    ttl: int = 8,
    conversation_id: str | None = None,
) -> bool:
    cfg = _load_config()
    source_instance = _normalize_instance_id(_get_instance_id(cfg))
    to_agent, inline_instance = _split_target_address(target)
    target_instance = _normalize_instance_id(target_instance) or inline_instance
    if not to_agent or not target_instance:
        print("❌ Target must be written as agent@INSTANCE or provided with --instance.", file=sys.stderr)
        return False

    remote = _find_remote_instance(to_agent, source_instance, target_instance=target_instance)
    if not remote:
        print(f"❌ Could not resolve remote peer for {to_agent}@{target_instance}.", file=sys.stderr)
        return False

    message_id = f"msg-{uuid.uuid4().hex[:16]}"
    conversation_id = conversation_id or f"conv-{uuid.uuid4().hex[:16]}"
    payload = {
        "message_type": "agent_message",
        "message_id": message_id,
        "conversation_id": conversation_id,
        "from_instance": source_instance,
        "from_agent": from_agent.lower(),
        "to_instance": target_instance,
        "to_agent": to_agent.lower(),
        "body": {"text": text},
        "hop_count": 0,
        "ttl": max(1, int(ttl)),
        "route_trace": [source_instance],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    instances = _load_instances()
    target_info = instances.get(str(target_instance or "").lower(), {})
    candidate_hosts = []
    for value in (remote.get("remote_host"), remote.get("host")):
        if value and value not in candidate_hosts:
            candidate_hosts.append(value)
    if str(target_info.get("platform") or "").lower() == "windows":
        for value in ("127.0.0.1", "localhost"):
            if value not in candidate_hosts:
                candidate_hosts.append(value)
    remote_port = remote.get("remote_port") or remote.get("port")
    if not candidate_hosts or not remote_port:
        print(f"❌ Remote peer for {to_agent}@{target_instance} has no remote port.", file=sys.stderr)
        return False
    remote_host = candidate_hosts[0]
    for candidate in candidate_hosts:
        if _probe_remote_http(candidate, int(remote_port)):
            remote_host = candidate
            break
    url = f"http://{remote_host}:{remote_port}/protocol/message"
    result = _post_json(url, payload, timeout=10)
    if result.get("ok"):
        print(f"✅ Protocol message delivered: {from_agent} → {to_agent}@{target_instance}")
        print(f"   message_id: {message_id}")
        print(f"   conversation_id: {conversation_id}")
        print(f"   state: {result.get('state', 'accepted')}")
        return True
    print(f"❌ Protocol message failed: {json.dumps(result, ensure_ascii=False)}", file=sys.stderr)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a Hashi Remote protocol message")
    parser.add_argument("--to", required=True, help="Target agent@INSTANCE")
    parser.add_argument("--from", dest="from_agent", required=True, help="Source agent")
    parser.add_argument("--text", required=True, help="Message text")
    parser.add_argument("--instance", default=None, help="Optional target instance override")
    parser.add_argument("--ttl", type=int, default=8, help="Requested TTL")
    parser.add_argument("--conversation-id", default=None, help="Optional existing conversation id")
    args = parser.parse_args()
    ok = send_protocol_message(
        args.to,
        args.from_agent,
        args.text,
        target_instance=args.instance,
        ttl=args.ttl,
        conversation_id=args.conversation_id,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
