from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def normalize_identity(value: Any) -> str:
    text = str(value or "").strip().lower()
    return "".join(ch for ch in text if ch.isalnum())


def wsl_unc_anchor(value: Any) -> str:
    text = str(value or "").strip().lower().replace("/", "\\")
    while text.startswith("\\\\\\"):
        text = text[1:]
    if not (text.startswith("\\\\wsl$\\") or text.startswith("\\\\wsl.localhost\\")):
        return ""
    parts = [part for part in text.split("\\") if part]
    if len(parts) < 2:
        return ""
    return f"\\\\{parts[0]}\\{parts[1]}\\"


@dataclass(frozen=True)
class RouteCandidate:
    host: str
    port: int
    scope: str
    source: str
    same_host: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "scope": self.scope,
            "source": self.source,
            "same_host": self.same_host,
        }


def _host_set(entry: dict[str, Any]) -> set[str]:
    hosts = {
        str(entry.get("api_host") or "").strip().lower(),
        str(entry.get("lan_ip") or "").strip().lower(),
        str(entry.get("tailscale_ip") or "").strip().lower(),
    }
    hosts.discard("")
    return hosts


def same_machine_hint(
    *,
    local_entry: dict[str, Any],
    target_entry: dict[str, Any],
    target_properties: dict[str, Any] | None = None,
    target_platform: str = "",
    local_profile: dict[str, Any] | None = None,
) -> bool:
    if str(target_entry.get("same_host_loopback") or "").strip():
        return True

    target_properties = target_properties or {}
    local_profile = local_profile or {}
    local_platform = str(local_entry.get("platform") or local_profile.get("environment_kind") or "").strip().lower()
    target_platform = str(target_entry.get("platform") or target_platform or "").strip().lower()
    local_identity = normalize_identity(local_entry.get("host_identity") or local_profile.get("host_identity") or "")
    target_identity = normalize_identity(target_entry.get("host_identity") or target_properties.get("host_identity") or "")

    local_hosts = _host_set(local_entry)
    for item in local_profile.get("address_candidates") or []:
        if isinstance(item, dict):
            host = str(item.get("host") or "").strip().lower()
            if host:
                local_hosts.add(host)
    target_hosts = _host_set(target_entry)

    if {local_platform, target_platform} == {"windows", "wsl"}:
        if local_platform == "windows" and target_entry.get("wsl_root_from_windows"):
            return True
        if local_platform == "wsl" and target_entry.get("wsl_root"):
            return True
        if local_identity and target_identity and local_identity == target_identity:
            return True
        return bool(local_hosts and target_hosts and local_hosts.intersection(target_hosts))

    if local_platform == "wsl" and target_platform == "wsl":
        if local_identity and target_identity and local_identity != target_identity:
            return False
        local_anchor = wsl_unc_anchor(local_entry.get("wsl_root_from_windows") or "")
        target_anchor = wsl_unc_anchor(target_entry.get("wsl_root_from_windows") or target_properties.get("wsl_root_from_windows") or "")
        if local_anchor and target_anchor and local_anchor == target_anchor:
            return True
        if local_identity and target_identity and local_identity == target_identity:
            return True
    return False


def build_route_candidates(
    *,
    target_entry: dict[str, Any],
    remote_port: int,
    same_host: bool,
    address_candidates: list[dict[str, Any]] | None = None,
    peer_host: str = "",
) -> list[RouteCandidate]:
    candidates: list[RouteCandidate] = []
    seen: set[str] = set()

    def add(host: Any, scope: str, source: str, *, allow_loopback: bool = False) -> None:
        host_text = str(host or "").strip()
        if not host_text or host_text in {"0.0.0.0", "localhost"}:
            return
        is_loopback = host_text == "127.0.0.1"
        if is_loopback and not (allow_loopback or same_host):
            return
        if host_text in seen:
            return
        seen.add(host_text)
        candidates.append(RouteCandidate(host_text, int(remote_port), scope, source, same_host=is_loopback or same_host))

    loopback = str(target_entry.get("same_host_loopback") or "").strip()
    if same_host:
        add(loopback or "127.0.0.1", "same_host", "same_host_loopback", allow_loopback=True)

    for item in address_candidates or []:
        if not isinstance(item, dict):
            continue
        scope = str(item.get("scope") or "").strip().lower()
        host = item.get("host")
        if scope == "same_host":
            add(host, scope, item.get("source") or "address_candidate", allow_loopback=same_host)
        elif scope in {"lan", "routable", "peer"}:
            add(host, scope, item.get("source") or "address_candidate")

    for key, scope in (("lan_ip", "lan"), ("tailscale_ip", "overlay"), ("api_host", "configured")):
        add(target_entry.get(key), scope, key)

    add(peer_host, "peer", "canonical_peer")
    return candidates


def validate_same_host_port_conflicts(instances: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(instances, dict):
        return []
    conflicts: list[dict[str, Any]] = []
    items = [(key, entry) for key, entry in instances.items() if isinstance(entry, dict)]
    for idx, (left_key, left) in enumerate(items):
        left_port = int(left.get("remote_port") or 0)
        if left_port <= 0:
            continue
        for right_key, right in items[idx + 1:]:
            right_port = int(right.get("remote_port") or 0)
            if right_port != left_port:
                continue
            if not same_machine_hint(local_entry=left, target_entry=right, target_platform=str(right.get("platform") or "")):
                continue
            conflicts.append(
                {
                    "level": "error",
                    "type": "same_host_remote_port_conflict",
                    "instances": [
                        str(left.get("instance_id") or left_key).upper(),
                        str(right.get("instance_id") or right_key).upper(),
                    ],
                    "remote_port": left_port,
                    "message": f"same-host instances share Remote port {left_port}",
                }
            )
    return conflicts
