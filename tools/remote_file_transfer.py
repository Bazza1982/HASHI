#!/usr/bin/env python3
"""
remote_file_transfer.py - Push files to another Hashi Remote instance.

Usage:
    python tools/remote_file_transfer.py push ./report.md HASHI9:/tmp/report.md
    python tools/remote_file_transfer.py push ./report.md HASHI9:C:\\Users\\me\\Desktop\\report.md --overwrite
    python tools/remote_file_transfer.py stat HASHI9:/tmp/report.md
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib import parse as urllib_parse
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from remote.security.client_auth import build_client_auth_headers
from tools.hchat_send import DEFAULT_REMOTE_PORT, _load_instances, _normalize_instance_id


def _split_remote_path(value: str, instance: str | None = None) -> tuple[str, str]:
    raw = str(value or "").strip()
    if instance:
        return _normalize_instance_id(instance), raw
    if ":" not in raw:
        raise ValueError("remote path must be INSTANCE:path, or pass --instance")
    inst, path = raw.split(":", 1)
    inst = _normalize_instance_id(inst)
    if not inst or not path:
        raise ValueError("remote path must be INSTANCE:path")
    return inst, path


def _instance_entry(instance_id: str) -> dict:
    instances = _load_instances()
    entry = instances.get(instance_id.lower())
    if not entry:
        known = ", ".join(sorted(instances)) or "none"
        raise ValueError(f"unknown instance {instance_id}; known instances: {known}")
    return entry


def _candidate_hosts(entry: dict) -> list[str]:
    hosts: list[str] = []
    for key in ("lan_ip", "tailscale_ip", "internet_host", "api_host", "host"):
        value = str(entry.get(key) or "").strip()
        if value and value not in hosts:
            hosts.append(value)
    if not hosts:
        hosts.append("127.0.0.1")
    return hosts


def _load_local_instance_id() -> str | None:
    env_value = _normalize_instance_id(os.getenv("HASHI_INSTANCE_ID"))
    if env_value:
        return env_value
    config_path = ROOT / "agents.json"
    if not config_path.exists():
        return None
    try:
        data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    return _normalize_instance_id((data.get("global") or {}).get("instance_id"))


def _build_request_headers(
    *,
    url: str,
    method: str,
    data: bytes | None,
    token: str | None,
    shared_token: str | None,
    from_instance: str | None,
) -> dict[str, str]:
    try:
        return build_client_auth_headers(
            url=url,
            method=method,
            data=data,
            token=token,
            shared_token=shared_token,
            from_instance=from_instance,
            normalize_instance=_normalize_instance_id,
            load_default_instance=_load_local_instance_id,
        )
    except ValueError as exc:
        if str(exc) == "shared-token mode requires a sender instance id":
            raise ValueError(
                "shared-token mode requires --from-instance or HASHI_INSTANCE_ID, "
                "or a local global.instance_id in agents.json"
            ) from exc
        raise


def _request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    token: str | None = None,
    shared_token: str | None = None,
    from_instance: str | None = None,
    timeout: int = 60,
) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = _build_request_headers(
        url=url,
        method=method,
        data=data,
        token=token,
        shared_token=shared_token,
        from_instance=from_instance,
    )
    req = urllib_request.Request(url, data=data, headers=headers, method=method)
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


def _remote_base_url(instance_id: str, timeout: int = 3) -> str:
    entry = _instance_entry(instance_id)
    port = int(entry.get("remote_port") or DEFAULT_REMOTE_PORT)
    last_error = ""
    for host in _candidate_hosts(entry):
        url = f"http://{host}:{port}"
        try:
            result = _request_json(f"{url}/health", timeout=timeout)
            if result.get("ok"):
                return url
            last_error = result.get("error") or str(result)
        except (URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
    raise RuntimeError(f"{instance_id} remote is not reachable on port {port}: {last_error}")


def push_file(
    local_path: Path,
    remote_spec: str,
    *,
    instance: str | None = None,
    overwrite: bool = False,
    create_dirs: bool = True,
    token: str | None = None,
    shared_token: str | None = None,
    from_instance: str | None = None,
    timeout: int = 120,
) -> bool:
    instance_id, dest_path = _split_remote_path(remote_spec, instance)
    data = local_path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    payload = {
        "dest_path": dest_path,
        "content_b64": base64.b64encode(data).decode("ascii"),
        "sha256": digest,
        "overwrite": overwrite,
        "create_dirs": create_dirs,
    }
    base_url = _remote_base_url(instance_id)
    result = _request_json(
        f"{base_url}/files/push",
        method="POST",
        payload=payload,
        token=token,
        shared_token=shared_token,
        from_instance=from_instance,
        timeout=timeout,
    )
    if result.get("ok"):
        print(f"OK: pushed {local_path} -> {instance_id}:{result['dest_path']}")
        print(f"    bytes={result['bytes_written']} sha256={result['sha256']}")
        return True
    print(f"ERROR: {result.get('error') or result}", file=sys.stderr)
    return False


def stat_file(
    remote_spec: str,
    *,
    instance: str | None = None,
    token: str | None = None,
    shared_token: str | None = None,
    from_instance: str | None = None,
) -> bool:
    instance_id, dest_path = _split_remote_path(remote_spec, instance)
    base_url = _remote_base_url(instance_id)
    query = urllib_parse.urlencode({"path": dest_path})
    result = _request_json(
        f"{base_url}/files/stat?{query}",
        token=token,
        shared_token=shared_token,
        from_instance=from_instance,
    )
    if result.get("ok"):
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return True
    print(f"ERROR: {result.get('error') or result}", file=sys.stderr)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Push files through Hashi Remote")
    parser.add_argument("--token", default=os.getenv("HASHI_REMOTE_TOKEN"), help="Bearer token when remote LAN mode is off")
    parser.add_argument(
        "--shared-token",
        default=os.getenv("HASHI_REMOTE_SHARED_TOKEN"),
        help="Shared-token secret for Hashi Remote HMAC auth",
    )
    parser.add_argument(
        "--from-instance",
        default=os.getenv("HASHI_INSTANCE_ID"),
        help="Sender instance id for shared-token HMAC auth",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    push = sub.add_parser("push", help="Push a local file to INSTANCE:path")
    push.add_argument("local_path", type=Path)
    push.add_argument("remote_path", help="Target as INSTANCE:path, or path when --instance is used")
    push.add_argument("--instance", help="Target instance, e.g. HASHI9")
    push.add_argument("--overwrite", action="store_true", help="Replace destination if it already exists")
    push.add_argument("--no-create-dirs", action="store_true", help="Fail if destination parent directory is missing")
    push.add_argument("--timeout", type=int, default=120)

    stat = sub.add_parser("stat", help="Read remote file metadata/checksum")
    stat.add_argument("remote_path", help="Target as INSTANCE:path, or path when --instance is used")
    stat.add_argument("--instance", help="Target instance, e.g. HASHI9")

    args = parser.parse_args()
    if args.cmd == "push":
        if not args.local_path.is_file():
            print(f"ERROR: local file not found: {args.local_path}", file=sys.stderr)
            return 2
        return 0 if push_file(
            args.local_path,
            args.remote_path,
            instance=args.instance,
            overwrite=args.overwrite,
            create_dirs=not args.no_create_dirs,
            token=args.token,
            shared_token=None if args.token else args.shared_token,
            from_instance=args.from_instance,
            timeout=args.timeout,
        ) else 1
    if args.cmd == "stat":
        return 0 if stat_file(
            args.remote_path,
            instance=args.instance,
            token=args.token,
            shared_token=None if args.token else args.shared_token,
            from_instance=args.from_instance,
        ) else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
