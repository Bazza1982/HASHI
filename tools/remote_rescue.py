#!/usr/bin/env python3
"""
remote_rescue.py - Inspect and start HASHI core through Hashi Remote.

Usage:
    python tools/remote_rescue.py capabilities HASHI1
    python tools/remote_rescue.py status HASHI1
    python tools/remote_rescue.py start HASHI1 --reason "core down"
    python tools/remote_rescue.py restart WATCHTOWER --reason "operator hard restart"
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.hchat_send import DEFAULT_REMOTE_PORT, _load_instances, _normalize_instance_id
from tools.remote_file_transfer import _candidate_hosts
from remote.live_endpoints import read_live_endpoints
from remote.runtime_identity import configured_instance_id, read_runtime_claim
from remote.security.client_auth import build_client_auth_headers
from remote.security.shared_token import load_shared_token


EXIT_REMOTE_ERROR = 1
EXIT_LOCAL_ERROR = 2
EXIT_UNSUPPORTED = 3
EXIT_FORBIDDEN = 4


@dataclass
class HttpResult:
    status: int
    body: dict
    url: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and bool(self.body.get("ok", True))


def _default_instance_id() -> str | None:
    return configured_instance_id(ROOT) or os.getenv("HASHI_INSTANCE_ID")


def _instance_entry(instance_id: str) -> dict:
    normalized = (_normalize_instance_id(instance_id) or "").lower()
    local_id = (_normalize_instance_id(_default_instance_id()) or "").lower()
    live = read_live_endpoints(ROOT).get(normalized)
    if live:
        entry = dict(live)
        if normalized and normalized == local_id:
            entry.setdefault("api_host", "127.0.0.1")
            entry.setdefault("lan_ip", "127.0.0.1")
        return entry
    if normalized and normalized == local_id:
        claim = read_runtime_claim(ROOT) or {}
        if claim:
            return {
                "instance_id": _normalize_instance_id(instance_id),
                "display_name": _normalize_instance_id(instance_id),
                "api_host": "127.0.0.1",
                "lan_ip": "127.0.0.1",
                "remote_port": int(claim.get("port") or DEFAULT_REMOTE_PORT),
            }
    instances = _load_instances()
    entry = instances.get(normalized)
    if not entry:
        known = ", ".join(sorted(instances)) or "none"
        raise ValueError(f"unknown instance {instance_id}; known instances: {known}")
    return entry


def _candidate_base_urls(instance_id: str) -> list[str]:
    entry = _instance_entry(instance_id)
    port = int(entry.get("remote_port") or entry.get("port") or DEFAULT_REMOTE_PORT)
    urls: list[str] = []
    for host in _candidate_hosts(entry):
        # Prefer HTTPS for upgraded/TLS deployments, then HTTP for LAN/dev.
        for scheme in ("https", "http"):
            url = f"{scheme}://{host}:{port}"
            if url not in urls:
                urls.append(url)
    return urls


def _request_json_status(
    url: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    token: str | None = None,
    shared_token: str | None = None,
    from_instance: str | None = None,
    timeout: int = 5,
) -> HttpResult:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = build_client_auth_headers(
        url=url,
        method=method,
        data=data,
        token=token,
        shared_token=shared_token,
        from_instance=from_instance,
        normalize_instance=_normalize_instance_id,
        load_default_instance=_default_instance_id,
    )
    req = urllib_request.Request(url, data=data, headers=headers, method=method)
    context = ssl._create_unverified_context() if url.startswith("https://") else None
    try:
        with urllib_request.urlopen(req, timeout=timeout, context=context) as resp:
            raw = resp.read().decode("utf-8")
            body = json.loads(raw) if raw else {}
            return HttpResult(status=resp.status, body=body, url=url)
    except HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = {"ok": False, "error": str(exc)}
        return HttpResult(status=exc.code, body=body, url=url)


def _reachable_base_url(
    instance_id: str,
    *,
    token: str | None = None,
    shared_token: str | None = None,
    from_instance: str | None = None,
    timeout: int = 5,
) -> str:
    last_error = ""
    for base_url in _candidate_base_urls(instance_id):
        try:
            result = _request_json_status(
                f"{base_url}/health",
                token=token,
                shared_token=shared_token,
                from_instance=from_instance,
                timeout=timeout,
            )
        except (URLError, TimeoutError, OSError, ssl.SSLError) as exc:
            last_error = str(exc)
            continue
        if result.ok:
            return base_url
        last_error = result.body.get("error") or str(result.body)
    raise RuntimeError(f"{instance_id} remote is not reachable: {last_error}")


def _unsupported_payload(instance_id: str, endpoint: str, base_url: str) -> dict:
    return {
        "ok": False,
        "instance": _normalize_instance_id(instance_id),
        "base_url": base_url,
        "supported": False,
        "endpoint": endpoint,
        "error": "peer does not support HASHI remote rescue",
    }


def probe_capabilities(
    instance_id: str,
    *,
    token: str | None = None,
    shared_token: str | None = None,
    from_instance: str | None = None,
    timeout: int = 5,
) -> dict:
    base_url = _reachable_base_url(instance_id, token=token, shared_token=shared_token, from_instance=from_instance, timeout=timeout)
    protocol = _request_json_status(f"{base_url}/protocol/status", token=token, shared_token=shared_token, from_instance=from_instance, timeout=timeout)
    advertised = []
    if protocol.status != 404 and protocol.ok:
        advertised = list(protocol.body.get("capabilities") or [])

    status_probe = _request_json_status(f"{base_url}/control/hashi/status", token=token, shared_token=shared_token, from_instance=from_instance, timeout=timeout)
    rescue_control = status_probe.status != 404 and status_probe.ok
    rescue_start = "rescue_start" in advertised
    return {
        "ok": True,
        "instance": _normalize_instance_id(instance_id),
        "base_url": base_url,
        "advertised_capabilities": advertised,
        "capabilities": {
            "remote_basic": True,
            "protocol_status": protocol.status != 404 and protocol.ok,
            "rescue_control": rescue_control,
            "rescue_start": rescue_start,
        },
        "remote_supervisor": protocol.body.get("remote_supervisor") if protocol.ok else None,
        "status_endpoint_status": status_probe.status,
    }


def rescue_status(instance_id: str, *, token: str | None = None, shared_token: str | None = None, from_instance: str | None = None, timeout: int = 5) -> tuple[int, dict]:
    base_url = _reachable_base_url(instance_id, token=token, shared_token=shared_token, from_instance=from_instance, timeout=timeout)
    result = _request_json_status(f"{base_url}/control/hashi/status", token=token, shared_token=shared_token, from_instance=from_instance, timeout=timeout)
    if result.status == 404:
        return EXIT_UNSUPPORTED, _unsupported_payload(instance_id, "/control/hashi/status", base_url)
    payload = dict(result.body)
    payload.setdefault("instance", _normalize_instance_id(instance_id))
    payload.setdefault("base_url", base_url)
    if result.status == 401:
        return EXIT_FORBIDDEN, payload
    return (0 if result.ok else EXIT_REMOTE_ERROR), payload


def rescue_logs(
    instance_id: str,
    *,
    name: str = "start",
    tail: int = 120,
    token: str | None = None,
    shared_token: str | None = None,
    from_instance: str | None = None,
    timeout: int = 5,
) -> tuple[int, dict]:
    base_url = _reachable_base_url(instance_id, token=token, shared_token=shared_token, from_instance=from_instance, timeout=timeout)
    result = _request_json_status(
        f"{base_url}/control/hashi/logs?name={name}&tail={int(tail)}",
        token=token,
        shared_token=shared_token,
        from_instance=from_instance,
        timeout=timeout,
    )
    if result.status == 404:
        return EXIT_UNSUPPORTED, _unsupported_payload(instance_id, "/control/hashi/logs", base_url)
    payload = dict(result.body)
    payload.setdefault("instance", _normalize_instance_id(instance_id))
    payload.setdefault("base_url", base_url)
    if result.status in {401, 403}:
        return EXIT_FORBIDDEN, payload
    return (0 if result.ok else EXIT_REMOTE_ERROR), payload


def rescue_start(
    instance_id: str,
    *,
    reason: str | None = None,
    token: str | None = None,
    shared_token: str | None = None,
    from_instance: str | None = None,
    timeout: int = 10,
) -> tuple[int, dict]:
    base_url = _reachable_base_url(instance_id, token=token, shared_token=shared_token, from_instance=from_instance, timeout=timeout)
    result = _request_json_status(
        f"{base_url}/control/hashi/start",
        method="POST",
        payload={"reason": reason},
        token=token,
        shared_token=shared_token,
        from_instance=from_instance,
        timeout=timeout,
    )
    if result.status == 404:
        return EXIT_UNSUPPORTED, _unsupported_payload(instance_id, "/control/hashi/start", base_url)
    payload = dict(result.body)
    payload.setdefault("instance", _normalize_instance_id(instance_id))
    payload.setdefault("base_url", base_url)
    if result.status in {401, 403}:
        return EXIT_FORBIDDEN, payload
    return (0 if result.ok else EXIT_REMOTE_ERROR), payload


def rescue_restart(
    instance_id: str,
    *,
    reason: str | None = None,
    token: str | None = None,
    shared_token: str | None = None,
    from_instance: str | None = None,
    timeout: int = 15,
) -> tuple[int, dict]:
    base_url = _reachable_base_url(instance_id, token=token, shared_token=shared_token, from_instance=from_instance, timeout=timeout)
    result = _request_json_status(
        f"{base_url}/control/hashi/restart",
        method="POST",
        payload={"reason": reason},
        token=token,
        shared_token=shared_token,
        from_instance=from_instance,
        timeout=timeout,
    )
    if result.status == 404:
        return EXIT_UNSUPPORTED, _unsupported_payload(instance_id, "/control/hashi/restart", base_url)
    payload = dict(result.body)
    payload.setdefault("instance", _normalize_instance_id(instance_id))
    payload.setdefault("base_url", base_url)
    if result.status in {401, 403}:
        return EXIT_FORBIDDEN, payload
    return (0 if result.ok else EXIT_REMOTE_ERROR), payload


def _print_result(payload: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True))
        return
    if "capabilities" in payload:
        caps = payload["capabilities"]
        print(f"{payload.get('instance')}: {payload.get('base_url')}")
        for name in sorted(caps):
            print(f"  {name}: {'yes' if caps[name] else 'no'}")
        supervisor = payload.get("remote_supervisor") or {}
        if supervisor:
            print(f"  remote_supervisor: {supervisor.get('mode', 'unknown')}")
        return
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Remote rescue helper for HASHI core")
    parser.add_argument("--token", default=os.getenv("HASHI_REMOTE_TOKEN"), help="Bearer token when remote LAN mode is off")
    parser.add_argument("--shared-token", default=os.getenv("HASHI_REMOTE_SHARED_TOKEN") or load_shared_token(ROOT), help="Shared-token HMAC secret")
    parser.add_argument("--from-instance", default=os.getenv("HASHI_INSTANCE_ID") or _default_instance_id(), help="Sender instance id for shared-token HMAC auth")
    parser.add_argument("--timeout", type=int, default=5, help="HTTP timeout seconds")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name in ("capabilities", "status"):
        command = sub.add_parser(name)
        command.add_argument("instance", help="Target instance, e.g. HASHI1")

    start = sub.add_parser("start")
    start.add_argument("instance", help="Target instance, e.g. HASHI1")
    start.add_argument("--reason", default="remote rescue", help="Audit reason recorded by target Remote")

    restart = sub.add_parser("restart")
    restart.add_argument("instance", help="Target instance, e.g. WATCHTOWER")
    restart.add_argument("--reason", default="remote hard restart", help="Audit reason recorded by target Remote")

    logs = sub.add_parser("logs")
    logs.add_argument("instance", help="Target instance, e.g. HASHI1")
    logs.add_argument("--name", choices=["start", "audit", "supervisor"], default="start")
    logs.add_argument("--tail", type=int, default=120)

    args = parser.parse_args(argv)
    try:
        if args.cmd == "capabilities":
            payload = probe_capabilities(args.instance, token=args.token, shared_token=None if args.token else args.shared_token, from_instance=args.from_instance, timeout=args.timeout)
            _print_result(payload, as_json=args.json)
            return 0
        if args.cmd == "status":
            code, payload = rescue_status(args.instance, token=args.token, shared_token=None if args.token else args.shared_token, from_instance=args.from_instance, timeout=args.timeout)
            _print_result(payload, as_json=args.json)
            return code
        if args.cmd == "start":
            code, payload = rescue_start(args.instance, reason=args.reason, token=args.token, shared_token=None if args.token else args.shared_token, from_instance=args.from_instance, timeout=max(args.timeout, 10))
            _print_result(payload, as_json=args.json)
            return code
        if args.cmd == "restart":
            code, payload = rescue_restart(args.instance, reason=args.reason, token=args.token, shared_token=None if args.token else args.shared_token, from_instance=args.from_instance, timeout=max(args.timeout, 15))
            _print_result(payload, as_json=args.json)
            return code
        if args.cmd == "logs":
            code, payload = rescue_logs(args.instance, name=args.name, tail=args.tail, token=args.token, shared_token=None if args.token else args.shared_token, from_instance=args.from_instance, timeout=args.timeout)
            _print_result(payload, as_json=args.json)
            return code
    except (RuntimeError, ValueError, URLError, TimeoutError, OSError, ssl.SSLError) as exc:
        payload = {"ok": False, "error": str(exc)}
        _print_result(payload, as_json=args.json)
        return EXIT_LOCAL_ERROR
    return EXIT_LOCAL_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
