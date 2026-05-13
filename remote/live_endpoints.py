from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable


def live_endpoints_path(root: Path | str) -> Path:
    return Path(root).expanduser().resolve() / "state" / "remote_live_endpoints.json"


def write_live_endpoints(root: Path | str, peers: Iterable[Any]) -> dict[str, Any]:
    now = time.time()
    entries: dict[str, dict[str, Any]] = {}
    for peer in peers:
        instance_id = str(getattr(peer, "instance_id", "") or "").strip().upper()
        if not instance_id:
            continue
        properties = dict(getattr(peer, "properties", {}) or {})
        entries[instance_id.lower()] = {
            "instance_id": instance_id,
            "display_name": str(getattr(peer, "display_name", "") or instance_id),
            "host": str(getattr(peer, "host", "") or ""),
            "port": int(getattr(peer, "port", 0) or 0),
            "workbench_port": int(getattr(peer, "workbench_port", 0) or 0),
            "platform": str(getattr(peer, "platform", "") or "unknown"),
            "protocol_version": str(getattr(peer, "protocol_version", "") or "1.0"),
            "capabilities": list(getattr(peer, "capabilities", []) or []),
            "discovery": str(properties.get("preferred_backend") or properties.get("discovery") or "unknown"),
            "host_identity": str(properties.get("host_identity") or ""),
            "environment_kind": str(properties.get("environment_kind") or ""),
            "updated_at": now,
        }
    data = {"version": 1, "updated_at": now, "endpoints": entries}
    path = live_endpoints_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)
    return data


def remove_live_endpoint(root: Path | str, instance_id: str) -> bool:
    normalized_id = str(instance_id or "").strip().lower()
    if not normalized_id:
        return False
    path = live_endpoints_path(root)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    endpoints = data.get("endpoints") if isinstance(data, dict) else {}
    if not isinstance(endpoints, dict):
        return False
    removed = endpoints.pop(normalized_id, None) is not None
    if not removed:
        return False
    data["endpoints"] = endpoints
    data["updated_at"] = time.time()
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)
    return True


def read_live_endpoints(root: Path | str, *, max_age_seconds: int = 24 * 3600) -> dict[str, dict[str, Any]]:
    path = live_endpoints_path(root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    endpoints = data.get("endpoints") if isinstance(data, dict) else {}
    if not isinstance(endpoints, dict):
        return {}
    now = time.time()
    fresh: dict[str, dict[str, Any]] = {}
    for key, entry in endpoints.items():
        if not isinstance(entry, dict):
            continue
        try:
            updated_at = float(entry.get("updated_at") or 0)
        except Exception:
            updated_at = 0
        if updated_at and now - updated_at > max_age_seconds:
            continue
        instance_id = str(entry.get("instance_id") or key or "").strip().lower()
        if instance_id:
            fresh[instance_id] = entry
    return fresh
