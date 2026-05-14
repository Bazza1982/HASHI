"""
protocol_send.py - Send a Hashi Remote protocol message to agent@INSTANCE.

Usage:
    python tools/protocol_send.py --to hashiko@HASHI9 --from lin_yueru --text "hello"
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from remote.security.client_auth import build_client_auth_headers
from tools.hchat_send import (
    _find_remote_instance,
    _get_instance_id,
    _load_instances,
    _load_config,
    _normalize_instance_id,
    _split_target_address,
)


def _build_request_headers(
    *,
    url: str,
    method: str,
    data: bytes | None,
    token: str | None,
    shared_token: str | None,
    from_instance: str | None,
) -> dict[str, str]:
    return build_client_auth_headers(
        url=url,
        method=method,
        data=data,
        token=token,
        shared_token=shared_token,
        from_instance=from_instance,
        normalize_instance=_normalize_instance_id,
    )


def _request_json(
    url: str,
    *,
    payload: dict,
    method: str = "POST",
    token: str | None = None,
    shared_token: str | None = None,
    from_instance: str | None = None,
    timeout: int = 10,
) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=data,
        headers=_build_request_headers(
            url=url,
            method=method,
            data=data,
            token=token,
            shared_token=shared_token,
            from_instance=from_instance,
        ),
        method=method,
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = {"ok": False, "error": str(exc)}
        body.setdefault("status", exc.code)
        return body


def _probe_remote_http(host: str, port: int, timeout: int = 3) -> bool:
    req = urllib_request.Request(f"http://{host}:{port}/health", method="GET")
    try:
        with urllib_request.urlopen(req, timeout=timeout):
            return True
    except URLError:
        return False
    except Exception:
        return True


def _build_message_payload(
    *,
    source_instance: str,
    from_agent: str,
    to_agent: str,
    target_instance: str,
    text: str,
    ttl: int,
    conversation_id: str | None,
) -> dict:
    message_id = f"msg-{uuid.uuid4().hex[:16]}"
    return {
        "message_type": "agent_message",
        "message_id": message_id,
        "conversation_id": conversation_id or f"conv-{uuid.uuid4().hex[:16]}",
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


def _encode_attachment(path: Path, *, message_id: str, index: int) -> dict:
    data = path.read_bytes()
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return {
        "message_id": message_id,
        "attachment_id": f"att-{index + 1}",
        "filename": path.name,
        "mime_type": mime_type,
        "content_b64": base64.b64encode(data).decode("ascii"),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _send_with_attachments(
    *,
    base_url: str,
    payload: dict,
    attachments: list[Path],
    token: str | None,
    shared_token: str | None,
    timeout: int,
) -> dict:
    staged: list[dict] = []
    for index, path in enumerate(attachments):
        upload_payload = _encode_attachment(path, message_id=payload["message_id"], index=index)
        upload_payload["from_instance"] = payload["from_instance"]
        upload_result = _request_json(
            f"{base_url}/attachments/upload",
            payload=upload_payload,
            token=token,
            shared_token=shared_token,
            from_instance=payload["from_instance"],
            timeout=timeout,
        )
        if not upload_result.get("ok"):
            _cancel_staged_attachments(
                base_url=base_url,
                message_id=payload["message_id"],
                from_instance=payload["from_instance"],
                staged=staged,
                token=token,
                shared_token=shared_token,
                timeout=timeout,
            )
            return upload_result
        attachment = dict(upload_result.get("attachment") or {})
        staged.append(
            {
                "attachment_id": upload_payload["attachment_id"],
                "pending_upload_id": attachment.get("pending_upload_id"),
                "filename": upload_payload["filename"],
                "mime_type": upload_payload["mime_type"],
                "sha256": upload_payload["sha256"],
                "size_bytes": attachment.get("size_bytes"),
            }
        )

    commit_payload = dict(payload)
    commit_payload["attachments"] = staged
    commit_result = _request_json(
        f"{base_url}/protocol/message-with-attachments",
        payload=commit_payload,
        token=token,
        shared_token=shared_token,
        from_instance=payload["from_instance"],
        timeout=timeout,
    )
    if not commit_result.get("ok"):
        _cancel_staged_attachments(
            base_url=base_url,
            message_id=payload["message_id"],
            from_instance=payload["from_instance"],
            staged=staged,
            token=token,
            shared_token=shared_token,
            timeout=timeout,
            reason="sender_commit_failed",
        )
    return commit_result


def _cancel_staged_attachments(
    *,
    base_url: str,
    message_id: str,
    from_instance: str,
    staged: list[dict],
    token: str | None,
    shared_token: str | None,
    timeout: int,
    reason: str = "sender_upload_failed",
) -> None:
    pending_upload_ids = [str(item.get("pending_upload_id") or "").strip() for item in staged if str(item.get("pending_upload_id") or "").strip()]
    if not pending_upload_ids:
        return
    payload = {
        "message_id": message_id,
        "from_instance": from_instance,
        "pending_upload_ids": pending_upload_ids,
        "reason": reason,
    }
    try:
        result = _request_json(
            f"{base_url}/attachments/upload/cancel",
            payload=payload,
            token=token,
            shared_token=shared_token,
            from_instance=from_instance,
            timeout=timeout,
        )
        if not result.get("ok"):
            print(
                f"⚠️  Failed to cancel staged uploads on server: {json.dumps(result, ensure_ascii=False)}",
                file=sys.stderr,
            )
    except Exception as exc:
        print(f"⚠️  Failed to cancel staged uploads on server: {exc}", file=sys.stderr)


def send_protocol_message(
    target: str,
    from_agent: str,
    text: str,
    *,
    target_instance: str | None = None,
    ttl: int = 8,
    conversation_id: str | None = None,
    attachments: list[Path] | None = None,
    token: str | None = None,
    shared_token: str | None = None,
    timeout: int = 10,
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

    payload = _build_message_payload(
        source_instance=source_instance,
        from_agent=from_agent,
        to_agent=to_agent,
        target_instance=target_instance,
        text=text,
        ttl=ttl,
        conversation_id=conversation_id,
    )
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
    base_url = f"http://{remote_host}:{remote_port}"
    attachments = attachments or []
    if attachments:
        result = _send_with_attachments(
            base_url=base_url,
            payload=payload,
            attachments=attachments,
            token=token,
            shared_token=shared_token,
            timeout=timeout,
        )
    else:
        result = _request_json(
            f"{base_url}/protocol/message",
            payload=payload,
            token=token,
            shared_token=shared_token,
            from_instance=source_instance,
            timeout=timeout,
        )
    if result.get("ok"):
        print(f"✅ Protocol message delivered: {from_agent} → {to_agent}@{target_instance}")
        print(f"   message_id: {payload['message_id']}")
        print(f"   conversation_id: {payload['conversation_id']}")
        print(f"   state: {result.get('state', 'accepted')}")
        if attachments:
            print(f"   attachments: {len(attachments)}")
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
    parser.add_argument("--attach", action="append", default=[], help="Attachment file path; may be repeated")
    parser.add_argument("--token", default=os.getenv("HASHI_REMOTE_TOKEN"), help="Optional bearer token")
    parser.add_argument(
        "--shared-token",
        default=os.getenv("HASHI_REMOTE_SHARED_TOKEN"),
        help="Shared-token secret for protocol/attachment HMAC auth",
    )
    parser.add_argument("--timeout", type=int, default=15, help="HTTP timeout in seconds")
    args = parser.parse_args()
    attachments = [Path(value).expanduser() for value in args.attach]
    for path in attachments:
        if not path.is_file():
            print(f"❌ Attachment not found: {path}", file=sys.stderr)
            return 2
    ok = send_protocol_message(
        args.to,
        args.from_agent,
        args.text,
        target_instance=args.instance,
        ttl=args.ttl,
        conversation_id=args.conversation_id,
        attachments=attachments,
        token=args.token,
        shared_token=args.shared_token,
        timeout=args.timeout,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
