"""
Peer Registry — syncs discovered peers back into instances.json.

When LanDiscovery finds a new HASHI instance on the LAN, this registry
writes its real IP and port into instances.json so hchat_send.py can
route messages to it using the actual network address (not 127.0.0.1).

This is the bridge between mDNS discovery and HASHI's existing routing.
"""

import dataclasses
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .base import PeerInfo

logger = logging.getLogger(__name__)


def _normalize_identity(value: str) -> str:
    value = str(value or "").strip().lower()
    return "".join(ch for ch in value if ch.isalnum())


class PeerRegistry:
    """
    Maintains the live peer list and syncs it to instances.json.

    When a peer is discovered, we update instances.json with:
      - lan_ip or tailscale_ip depending on discovery backend
      - remote_port: the Hashi Remote peer port
      - workbench_port: the peer's Workbench API port
      - last_seen: unix timestamp
      - active: true

    hchat_send.py reads lan_ip (if present) instead of api_host,
    enabling true cross-machine message delivery.
    """

    def __init__(self, hashi_root: Path, self_instance_id: str):
        self._root = hashi_root
        self._self_id = self_instance_id.upper()
        self._instances_path = hashi_root / "instances.json"
        self._state_dir = Path.home() / ".hashi-remote"
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self._state_dir / f"peers_state_{self._self_id.lower()}.json"
        self._observations: dict[str, dict[str, PeerInfo]] = {}
        self._peers: dict[str, PeerInfo] = {}
        self._load_state()

    def on_peers_changed(self, peers: list[PeerInfo]) -> None:
        """Callback for LanDiscovery — called whenever peers list changes."""
        backend = None
        if peers:
            backend = str(peers[0].properties.get("discovery", "") or "").lower() or None
        if not backend:
            backend = "unknown"
        current_ids = set()
        for peer in peers:
            iid = peer.instance_id.upper()
            current_ids.add(iid)
            self._observations.setdefault(iid, {})[backend] = peer
        # Only discovery backends that report a full current snapshot should
        # prune peers that are missing from this callback. Incremental sources
        # like bootstrap/handshake_inbound would otherwise delete each other.
        if backend in {"lan", "tailscale", "unknown"}:
            for iid, by_backend in list(self._observations.items()):
                if backend in by_backend and iid not in current_ids:
                    del by_backend[backend]
                if not by_backend:
                    del self._observations[iid]
        self._rebuild_canonical_peers()
        self._sync_to_instances_json()
        self._save_state()

    def get_peers(self) -> list[PeerInfo]:
        return list(self._peers.values())

    def get_peer(self, instance_id: str) -> Optional[PeerInfo]:
        return self._peers.get(instance_id.upper())

    def get_peer_state(self, instance_id: str) -> dict:
        iid = instance_id.upper()
        peer = self._peers.get(iid)
        backends = self._observations.get(iid, {})
        return {
            "instance_id": iid,
            "canonical": peer.to_dict() if peer else None,
            "observations": {
                name: info.to_dict()
                for name, info in backends.items()
            },
        }

    def mark_handshake_result(
        self,
        instance_id: str,
        *,
        state: str,
        protocol_version: str | None = None,
        capabilities: list[str] | None = None,
        last_error: str | None = None,
        remote_agents: list[dict] | None = None,
    ) -> None:
        iid = instance_id.upper()
        peer = self._peers.get(iid)
        if peer is None:
            return
        fallback = (self._observations.get(iid) or {}).get("bootstrap_fallback")
        if state == "handshake_accepted" and fallback and str(fallback.host or "").strip() in {"127.0.0.1", "localhost"}:
            peer = dataclasses.replace(fallback)
        props = dict(peer.properties or {})
        props["handshake_state"] = state
        props["last_handshake_at"] = int(time.time())
        props.setdefault("preferred_backend", props.get("discovery"))
        props.setdefault("alternate_backends", [])
        if state == "handshake_accepted":
            props.pop("last_error", None)
        elif last_error:
            props["last_error"] = last_error
        if remote_agents is not None:
            props["remote_agents"] = remote_agents
        if protocol_version:
            peer.protocol_version = protocol_version
        if capabilities is not None:
            peer.capabilities = list(capabilities)
        peer.properties = props
        self._peers[iid] = peer
        self._sync_to_instances_json()
        self._save_state()

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            for iid, item in (data.get("peers") or {}).items():
                canonical = item.get("canonical")
                observations = item.get("observations") or {}
                if canonical:
                    self._peers[iid.upper()] = PeerInfo(**canonical)
                self._observations[iid.upper()] = {
                    name: PeerInfo(**obs)
                    for name, obs in observations.items()
                }
        except Exception as exc:
            logger.warning("Registry: failed to load peer state: %s", exc)

    def _save_state(self) -> None:
        try:
            data = {
                "peers": {
                    iid: {
                        "canonical": peer.to_dict(),
                        "observations": {
                            name: info.to_dict()
                            for name, info in self._observations.get(iid, {}).items()
                        },
                    }
                    for iid, peer in self._peers.items()
                }
            }
            self._state_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            logger.warning("Registry: failed to save peer state: %s", exc)

    def _select_preferred_backend(self, observations: dict[str, PeerInfo]) -> str | None:
        fallback = observations.get("bootstrap_fallback")
        if fallback and str(fallback.host or "").strip() in {"127.0.0.1", "localhost"}:
            return "bootstrap_fallback"
        for backend in ("lan", "tailscale"):
            if backend in observations:
                return backend
        for backend in sorted(observations):
            return backend
        return None

    def _extract_address_candidates(self, peer: PeerInfo, observations: dict[str, PeerInfo]) -> list[dict]:
        candidates: list[dict] = []
        seen: set[tuple[str, str]] = set()

        def _add(host: str, scope: str, source: str) -> None:
            host = str(host or "").strip()
            scope = str(scope or "").strip() or "unknown"
            if not host:
                return
            key = (host, scope)
            if key in seen:
                return
            seen.add(key)
            candidates.append({"host": host, "scope": scope, "source": source})

        for item in (peer.properties or {}).get("address_candidates") or []:
            if isinstance(item, dict):
                _add(item.get("host"), item.get("scope"), item.get("source") or "peer")
        for backend, info in observations.items():
            scope = "lan" if backend == "lan" else "overlay" if backend == "tailscale" else "peer"
            _add(info.host, scope, backend)
        _add(peer.host, "peer", "canonical")
        return candidates

    def _extract_observed_candidates(self, peer: PeerInfo, observations: dict[str, PeerInfo]) -> list[dict]:
        candidates: list[dict] = []
        seen: set[tuple[str, str]] = set()

        def _add(host: str, scope: str, source: str) -> None:
            host = str(host or "").strip()
            scope = str(scope or "").strip() or "unknown"
            if not host:
                return
            key = (host, scope)
            if key in seen:
                return
            seen.add(key)
            candidates.append({"host": host, "scope": scope, "source": source})

        for field in ("observed_candidates", "address_candidates"):
            for item in (peer.properties or {}).get(field) or []:
                if isinstance(item, dict):
                    _add(item.get("host"), item.get("scope"), item.get("source") or "peer")
        for backend, info in observations.items():
            for field in ("observed_candidates", "address_candidates"):
                for item in (info.properties or {}).get(field) or []:
                    if isinstance(item, dict):
                        _add(item.get("host"), item.get("scope"), item.get("source") or backend)
            scope = "lan" if backend == "lan" else "overlay" if backend == "tailscale" else "peer"
            _add(info.host, scope, backend)
        _add(peer.host, "peer", "canonical")
        return candidates

    def _merged_property(self, observations: dict[str, PeerInfo], key: str) -> str:
        for backend in ("bootstrap_fallback", "handshake_inbound", "lan", "tailscale", "bootstrap"):
            info = observations.get(backend)
            if not info:
                continue
            value = (info.properties or {}).get(key)
            if value:
                return value
        return ""

    def _rebuild_canonical_peers(self) -> None:
        canonical: dict[str, PeerInfo] = {}
        for iid, by_backend in self._observations.items():
            selected = self._select_preferred_backend(by_backend)
            if not selected:
                continue
            chosen = by_backend[selected]
            props = dict(chosen.properties or {})
            props["preferred_backend"] = selected
            props["alternate_backends"] = sorted(name for name in by_backend if name != selected)
            merged = PeerInfo(
                instance_id=chosen.instance_id,
                display_name=chosen.display_name,
                host=chosen.host,
                port=chosen.port,
                workbench_port=chosen.workbench_port,
                platform=chosen.platform,
                version=chosen.version,
                hashi_version=chosen.hashi_version,
                display_handle=chosen.display_handle or f"@{chosen.instance_id.lower()}",
                protocol_version=chosen.protocol_version,
                capabilities=list(chosen.capabilities or []),
                properties=props,
            )
            previous = self._peers.get(iid)
            if previous:
                prev_props = dict(previous.properties or {})
                for key in ("handshake_state", "last_handshake_at", "last_error", "remote_agents"):
                    if key in prev_props and key not in merged.properties:
                        merged.properties[key] = prev_props[key]
            canonical[iid] = merged
        self._peers = canonical

    def _sync_to_instances_json(self) -> None:
        """Write discovered peer IPs into instances.json for hchat routing."""
        if not self._instances_path.exists():
            logger.warning("instances.json not found at %s", self._instances_path)
            return

        try:
            data = json.loads(self._instances_path.read_text(encoding="utf-8"))
            instances = data.get("instances", {})
            local_entry = instances.get(self._self_id.lower(), {})
            local_platform = str(local_entry.get("platform") or "").lower()
            local_hosts = {
                str(local_entry.get("api_host") or "").strip().lower(),
                str(local_entry.get("lan_ip") or "").strip().lower(),
                str(local_entry.get("tailscale_ip") or "").strip().lower(),
            }
            local_hosts.discard("")
            local_host_identity = _normalize_identity(local_entry.get("host_identity") or "")

            changed = False
            for iid, peer in self._peers.items():
                key = iid.lower()
                preferred = peer.properties.get("preferred_backend") or peer.properties.get("discovery", "lan")
                host_key = "tailscale_ip" if preferred == "tailscale" else "lan_ip"
                now = int(time.time())
                observations = self._observations.get(iid, {})
                peer_platform = str(peer.platform or "").lower()
                peer_hosts = {
                    str(peer.host or "").strip().lower(),
                    str((observations.get("lan").host if observations.get("lan") else "") or "").strip().lower(),
                    str((observations.get("tailscale").host if observations.get("tailscale") else "") or "").strip().lower(),
                }
                peer_hosts.discard("")
                existing_entry = instances.get(key, {})
                address_candidates = self._extract_address_candidates(peer, observations)
                observed_candidates = self._extract_observed_candidates(peer, observations)
                host_identity = _normalize_identity((peer.properties or {}).get("host_identity") or self._merged_property(observations, "host_identity") or "")
                environment_kind = str((peer.properties or {}).get("environment_kind") or self._merged_property(observations, "environment_kind") or "").strip().lower()
                same_machine_hint = False
                if {local_platform, peer_platform} == {"windows", "wsl"}:
                    if local_platform == "windows" and existing_entry.get("wsl_root_from_windows"):
                        same_machine_hint = True
                    elif local_platform == "wsl" and existing_entry.get("wsl_root"):
                        same_machine_hint = True
                    elif local_host_identity and host_identity and local_host_identity == host_identity:
                        same_machine_hint = True
                    elif local_hosts and peer_hosts and local_hosts.intersection(peer_hosts):
                        same_machine_hint = True
                same_host_loopback = "127.0.0.1" if same_machine_hint else None
                handshake_state = str(peer.properties.get("handshake_state") or "")
                is_active = handshake_state not in {"handshake_timed_out", "handshake_rejected", "unreachable"}
                if key not in instances:
                    # New instance discovered on LAN — add it
                    instances[key] = {
                        "display_name": peer.display_name,
                        "instance_id": iid,
                        "platform": peer.platform,
                        "workbench_port": peer.workbench_port,
                        "api_host": peer.host,
                        host_key: peer.host,
                        "remote_port": peer.port,
                        "active": is_active,
                        "_discovery": preferred,
                        "protocol_version": peer.protocol_version,
                        "capabilities": list(peer.capabilities or []),
                        "handshake_state": handshake_state,
                        "last_seen": now,
                        "address_candidates": address_candidates,
                        "observed_candidates": observed_candidates,
                    }
                    if host_identity:
                        instances[key]["host_identity"] = host_identity
                    if environment_kind:
                        instances[key]["environment_kind"] = environment_kind
                    if "lan" in observations:
                        instances[key]["lan_ip"] = observations["lan"].host
                    if "tailscale" in observations:
                        instances[key]["tailscale_ip"] = observations["tailscale"].host
                    if same_host_loopback:
                        instances[key]["same_host_loopback"] = same_host_loopback
                    logger.info("Registry: added new peer %s @ %s", iid, peer.host)
                    changed = True
                else:
                    # Update IP/port if changed
                    entry = instances[key]
                    existing_lan_ip = entry.get("lan_ip")
                    existing_tailscale_ip = entry.get("tailscale_ip")
                    effective_host = peer.host
                    if str(peer.host or "").strip() in {"127.0.0.1", "localhost"}:
                        if host_key == "lan_ip" and existing_lan_ip and existing_lan_ip not in {"127.0.0.1", "localhost"}:
                            effective_host = existing_lan_ip
                        if host_key == "tailscale_ip" and existing_tailscale_ip and existing_tailscale_ip not in {"127.0.0.1", "localhost"}:
                            effective_host = existing_tailscale_ip
                    if (
                        entry.get(host_key) != effective_host
                        or entry.get("remote_port") != peer.port
                        or entry.get("workbench_port") != peer.workbench_port
                        or entry.get("_discovery") != preferred
                        or entry.get("active") != is_active
                        or entry.get("handshake_state") != handshake_state
                        or entry.get("same_host_loopback") != same_host_loopback
                        or entry.get("address_candidates") != address_candidates
                        or entry.get("observed_candidates") != observed_candidates
                        or entry.get("host_identity") != host_identity
                        or entry.get("environment_kind") != environment_kind
                    ):
                        entry[host_key] = effective_host
                        entry["remote_port"] = peer.port
                        entry["workbench_port"] = peer.workbench_port
                        entry["api_host"] = peer.host
                        entry["active"] = is_active
                        entry["_discovery"] = preferred
                        entry["protocol_version"] = peer.protocol_version
                        entry["capabilities"] = list(peer.capabilities or [])
                        entry["handshake_state"] = handshake_state
                        entry["last_seen"] = now
                        entry["address_candidates"] = address_candidates
                        entry["observed_candidates"] = observed_candidates
                        if host_identity:
                            entry["host_identity"] = host_identity
                        else:
                            entry.pop("host_identity", None)
                        if environment_kind:
                            entry["environment_kind"] = environment_kind
                        else:
                            entry.pop("environment_kind", None)
                        if "lan" in observations:
                            entry["lan_ip"] = observations["lan"].host
                        if "tailscale" in observations:
                            entry["tailscale_ip"] = observations["tailscale"].host
                        if same_host_loopback:
                            entry["same_host_loopback"] = same_host_loopback
                        else:
                            entry.pop("same_host_loopback", None)
                        logger.info("Registry: updated peer %s → %s:%d", iid, peer.host, peer.port)
                        changed = True
                    else:
                        refreshed = False
                        if entry.get("observed_candidates") != observed_candidates:
                            entry["observed_candidates"] = observed_candidates
                            refreshed = True
                        if entry.get("last_seen") != now:
                            entry["last_seen"] = now
                            refreshed = True
                        if refreshed:
                            changed = True

            if changed:
                data["instances"] = instances
                self._instances_path.write_text(
                    json.dumps(data, indent=4, ensure_ascii=False),
                    encoding="utf-8",
                )
                logger.info("Registry: instances.json updated (%d peers)", len(self._peers))

        except Exception as e:
            logger.error("Registry: failed to sync instances.json: %s", e)
