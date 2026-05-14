"""
Protocol manager for Hashi Remote peer-to-peer messaging.

This is the service-owned control plane for:
  - peer handshake
  - active agent directory exchange
  - merged peer state inspection
  - protocol message ingress
  - transcript-based reply correlation
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import socket
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

from remote.routing import build_route_candidates, same_machine_hint, validate_same_host_port_conflicts
from remote.local_http import local_http_hosts, local_http_url
from remote.live_endpoints import read_live_endpoints
from remote.peer.base import is_valid_instance_id
from remote.security.shared_token import build_auth_headers, load_shared_token

logger = logging.getLogger(__name__)


def _normalize_identity(value: str) -> str:
    value = str(value or "").strip().lower()
    return "".join(ch for ch in value if ch.isalnum())


def _wsl_unc_anchor(value: str) -> str:
    text = str(value or "").strip().lower().replace("/", "\\")
    while text.startswith("\\\\\\"):
        text = text[1:]
    if not text.startswith("\\\\wsl$\\"):
        return ""
    parts = [part for part in text.split("\\") if part]
    if len(parts) < 2:
        return ""
    return f"\\\\{parts[0]}\\{parts[1]}\\"

PROTOCOL_VERSION = "2.0"
DEFAULT_CAPABILITIES = [
    "handshake_v2",
    "agent_directory_v1",
    "protocol_message_v1",
    "agent_reply_v1",
    "rescue_control",
]


def build_default_capabilities(*, rescue_start_enabled: bool = False) -> list[str]:
    capabilities = list(DEFAULT_CAPABILITIES)
    if rescue_start_enabled:
        capabilities.append("rescue_start")
    return capabilities


class ProtocolManager:
    def __init__(
        self,
        *,
        hashi_root: Path,
        instance_info: dict,
        peer_registry,
        workbench_port: int,
        local_capabilities: list[str] | None = None,
        max_allowed_ttl: int = 8,
        handshake_timeout_seconds: int = 8,
        poll_interval_seconds: float = 0.5,
        settle_window_seconds: float = 2.0,
        reply_soft_timeout_seconds: int = 45,
        reply_hard_timeout_seconds: int = 180,
        use_tls: bool = True,
    ):
        self._hashi_root = hashi_root
        self._instance_info = instance_info
        self._peer_registry = peer_registry
        self._workbench_port = workbench_port
        self._capabilities = list(local_capabilities or DEFAULT_CAPABILITIES)
        self._max_allowed_ttl = max(1, int(max_allowed_ttl))
        self._handshake_timeout_seconds = max(2, int(handshake_timeout_seconds))
        self._poll_interval_seconds = max(0.2, float(poll_interval_seconds))
        self._settle_window_seconds = max(0.5, float(settle_window_seconds))
        self._reply_soft_timeout_seconds = max(5, int(reply_soft_timeout_seconds))
        self._reply_hard_timeout_seconds = max(self._reply_soft_timeout_seconds, int(reply_hard_timeout_seconds))
        self._use_tls = bool(use_tls)
        self._bootstrap_retry_seconds = 60.0
        self._state_dir = Path.home() / ".hashi-remote"
        self._state_dir.mkdir(parents=True, exist_ok=True)
        instance_key = str(instance_info.get("instance_id") or "hashi").lower()
        self._inflight_path = self._state_dir / f"protocol_inflight_{instance_key}.json"
        self._inflight: dict[str, dict[str, Any]] = self._load_json(self._inflight_path).get("messages", {})
        self._shared_token = load_shared_token(self._hashi_root)
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_bootstrap_run = 0.0
        self._last_handshake_run = 0.0
        self._last_refresh_run = 0.0
        self._last_agent_snapshot_version = ""
        self._last_agent_directory_state = "core_offline"
        self._force_handshake = False
        self._agent_snapshot_cache: list[dict[str, Any]] = []
        self._agent_snapshot_meta: dict[str, Any] = {
            "version": "",
            "directory_state": "core_offline",
            "updated_at": 0,
        }
        self._core_health_cache: tuple[float, bool] = (0.0, False)

    def get_protocol_status(self) -> dict:
        peers = []
        if self._peer_registry:
            for peer in self._peer_registry.get_peers():
                peers.append(self._peer_registry.get_peer_state(peer.instance_id))
        local_profile = self._local_network_profile()
        return {
            "protocol_version": PROTOCOL_VERSION,
            "display_handle": self.display_handle,
            "capabilities": list(self._capabilities),
            "remote_supervisor": dict(self._instance_info.get("remote_supervisor") or {}),
            "local_agents": self.get_local_agents_snapshot(),
            "local_agent_directory": self.get_local_agent_directory_state(),
            "local_network_profile": local_profile,
            "route_diagnostics": self.get_route_diagnostics(),
            "peers": peers,
            "inflight_count": len(self._inflight),
            "max_allowed_ttl": self._max_allowed_ttl,
            "reply_soft_timeout_seconds": self._reply_soft_timeout_seconds,
            "reply_hard_timeout_seconds": self._reply_hard_timeout_seconds,
        }

    @property
    def display_handle(self) -> str:
        return f"@{str(self._instance_info.get('instance_id', 'hashi')).lower()}"

    def _local_network_profile(self) -> dict:
        from remote.peer.base import PeerInfo

        info = PeerInfo(
            instance_id=str(self._instance_info.get("instance_id") or "HASHI"),
            display_name=str(self._instance_info.get("display_name") or self._instance_info.get("instance_id") or "HASHI"),
            host=str(self._instance_info.get("api_host") or "127.0.0.1"),
            port=int(self._instance_info.get("remote_port") or 0),
            workbench_port=int(self._instance_info.get("workbench_port") or 18800),
            platform=str(self._instance_info.get("platform") or "unknown"),
            hashi_version=str(self._instance_info.get("hashi_version") or "unknown"),
            display_handle=self.display_handle,
            protocol_version=PROTOCOL_VERSION,
            capabilities=list(self._capabilities),
        )
        try:
            from remote.peer.lan import build_local_network_profile
        except ModuleNotFoundError:
            host_identity = _normalize_identity(socket.gethostname())
            return {
                "host_identity": host_identity,
                "environment_kind": str(info.platform or "unknown").lower(),
                "address_candidates": [{"host": "127.0.0.1", "scope": "same_host", "source": "fallback"}],
                "observed_candidates": [{"host": "127.0.0.1", "scope": "same_host", "source": "fallback"}],
            }
        return build_local_network_profile(info)

    def _core_online(self) -> bool:
        now = time.time()
        cached_at, cached_value = getattr(self, "_core_health_cache", (0.0, False))
        if now - cached_at <= 5:
            return cached_value
        port = int((getattr(self, "_instance_info", {}) or {}).get("workbench_port") or getattr(self, "_workbench_port", 18800) or 18800)
        ok = False
        for host in local_http_hosts():
            try:
                with urllib_request.urlopen(local_http_url(port, "/api/health", host=host), timeout=0.4) as resp:
                    ok = 200 <= int(getattr(resp, "status", 200)) < 300
                    break
            except Exception:
                continue
        self._core_health_cache = (now, ok)
        return ok

    def _agent_snapshot_version(self, agents_path: Path, raw: bytes) -> str:
        try:
            stat = agents_path.stat()
            basis = f"{stat.st_mtime_ns}:{stat.st_size}:".encode() + raw
        except Exception:
            basis = raw
        return hashlib.sha256(basis).hexdigest()[:16]

    def _agent_directory_state(self, *, agents_readable: bool, core_online: bool, has_cache: bool) -> str:
        if core_online and agents_readable:
            return "fresh"
        if agents_readable or has_cache:
            return "stale"
        return "core_offline"

    def get_local_agent_directory_state(self) -> dict[str, Any]:
        if not hasattr(self, "_agent_snapshot_meta"):
            self._agent_snapshot_meta = {"version": "", "directory_state": "core_offline", "updated_at": 0}
        self.get_local_agents_snapshot()
        return dict(self._agent_snapshot_meta)

    def get_local_agents_snapshot(self) -> list[dict]:
        if not getattr(self, "_hashi_root", None):
            self._agent_snapshot_meta = {"version": "", "directory_state": "core_offline", "updated_at": int(time.time())}
            return []
        agents_path = self._hashi_root / "agents.json"
        core_online = self._core_online()
        cache = list(getattr(self, "_agent_snapshot_cache", []) or [])
        if not agents_path.exists():
            directory_state = self._agent_directory_state(agents_readable=False, core_online=core_online, has_cache=bool(cache))
            snapshot = [dict(item, directory_state=directory_state) for item in cache]
            self._agent_snapshot_meta = {
                "version": getattr(self, "_last_agent_snapshot_version", ""),
                "directory_state": directory_state,
                "updated_at": int(time.time()),
            }
            return snapshot
        try:
            raw = agents_path.read_bytes()
            data = json.loads(raw.decode("utf-8-sig"))
        except Exception:
            directory_state = self._agent_directory_state(agents_readable=False, core_online=core_online, has_cache=bool(cache))
            snapshot = [dict(item, directory_state=directory_state) for item in cache]
            self._agent_snapshot_meta = {
                "version": getattr(self, "_last_agent_snapshot_version", ""),
                "directory_state": directory_state,
                "updated_at": int(time.time()),
            }
            return snapshot
        version = self._agent_snapshot_version(agents_path, raw)
        directory_state = self._agent_directory_state(agents_readable=True, core_online=core_online, has_cache=bool(cache))
        snapshot = []
        for agent in data.get("agents", []):
            if not agent.get("is_active", True):
                continue
            snapshot.append(
                {
                    "agent_name": agent["name"],
                    "agent_address": f"{agent['name']}@{str(self._instance_info.get('instance_id', 'HASHI')).lower()}",
                    "display_name": agent.get("display_name", agent["name"]),
                    "is_active": True,
                    "directory_state": directory_state,
                    "updated_at": int(time.time()),
                    "agent_snapshot_version": version,
                }
            )
        self._last_agent_snapshot_version = version
        self._agent_snapshot_cache = [dict(item) for item in snapshot]
        self._agent_snapshot_meta = {
            "version": version,
            "directory_state": directory_state,
            "updated_at": int(time.time()),
        }
        return snapshot

    def _refresh_local_agent_snapshot_if_changed(self) -> bool:
        if not getattr(self, "_hashi_root", None):
            return False
        previous = getattr(self, "_last_agent_snapshot_version", "")
        previous_state = getattr(self, "_last_agent_directory_state", "")
        self.get_local_agents_snapshot()
        current = getattr(self, "_last_agent_snapshot_version", "")
        current_state = str((getattr(self, "_agent_snapshot_meta", {}) or {}).get("directory_state") or "")
        changed = bool(previous and current and (previous != current or previous_state != current_state))
        self._last_agent_directory_state = current_state
        if changed:
            self._force_handshake = True
            logger.info("Agent directory snapshot changed: %s/%s -> %s/%s", previous, previous_state, current, current_state)
        return changed

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        # Reset any stale handshake_in_progress states left over from a previous run.
        # These would otherwise block the handshake cycle indefinitely.
        if self._peer_registry:
            for peer in self._peer_registry.get_peers():
                state = str((peer.properties or {}).get("handshake_state") or "")
                if state == "handshake_in_progress":
                    self._peer_registry.mark_handshake_result(peer.instance_id, state="handshake_pending")
        # Bootstrap known peers from instances.json before first handshake cycle.
        # This ensures peers are reachable even when mDNS multicast fails
        # (e.g. WSL2 → physical LAN boundary).
        asyncio.create_task(self._bootstrap_known_peers(initial_delay=2.0))
        self._task = asyncio.create_task(self._control_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _bootstrap_known_peers(self, *, initial_delay: float = 0.0) -> None:
        """
        Probe peers listed in instances.json and register reachable ones.

        This is a fallback for environments where mDNS multicast doesn't cross
        network boundaries (e.g. WSL2 to physical LAN). Any instance that has
        a remote_port and a reachable host is injected into the peer registry so
        the normal handshake cycle can then proceed with it.
        """
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)  # Give discovery backends a moment before first bootstrap
        self._last_bootstrap_run = time.time()
        local_id = str(self._instance_info.get("instance_id") or "").upper()
        instances = self._dedupe_bootstrap_instances(self._load_instances())
        live_endpoints = read_live_endpoints(self._hashi_root)
        for key, entry in instances.items():
            if not isinstance(entry, dict):
                continue
            instance_id = str(entry.get("instance_id") or key).upper()
            if not is_valid_instance_id(instance_id):
                logger.debug("Bootstrap: skipping invalid instance identity from seed: %s", instance_id or key)
                continue
            if instance_id == local_id:
                continue
            if self._peer_registry and self._peer_registry.get_peer(instance_id):
                continue  # Already known via mDNS
            live_entry = live_endpoints.get(instance_id.lower(), {})
            probe_ports = self._bootstrap_probe_ports(entry, live_entry)
            if not probe_ports:
                logger.debug("Bootstrap: %s has no live or fallback probe ports, skipping", instance_id)
                continue
            seen_hosts = self._candidate_hosts_for_entry(entry)

            for host in seen_hosts:
                selected_port = None
                for remote_port in probe_ports:
                    if self._probe_route(host, int(remote_port), timeout=2):
                        selected_port = int(remote_port)
                        break
                if selected_port:
                    from remote.peer.base import PeerInfo
                    peer = PeerInfo(
                        instance_id=instance_id,
                        display_name=str(live_entry.get("display_name") or entry.get("display_name") or instance_id),
                        host=host,
                        port=selected_port,
                        workbench_port=int(live_entry.get("workbench_port") or entry.get("workbench_port") or 18800),
                        platform=str(live_entry.get("platform") or entry.get("platform") or "unknown"),
                        hashi_version=str(entry.get("hashi_version") or "unknown"),
                        display_handle=f"@{instance_id.lower()}",
                        protocol_version=str(live_entry.get("protocol_version") or entry.get("protocol_version") or "1.0"),
                        capabilities=list(live_entry.get("capabilities") or entry.get("capabilities") or []),
                        properties={
                            "discovery": "bootstrap",
                            "live_endpoint_source": "cache" if live_entry else "seed",
                            "address_candidates": list(entry.get("address_candidates") or []),
                            "observed_candidates": list(entry.get("observed_candidates") or []),
                            "host_identity": _normalize_identity(live_entry.get("host_identity") or entry.get("host_identity") or ""),
                            "environment_kind": str(live_entry.get("environment_kind") or entry.get("environment_kind") or "").strip().lower(),
                        },
                    )
                    if self._peer_registry:
                        self._peer_registry.on_peers_changed([peer])
                        logger.info("Bootstrap: registered %s @ %s:%d", instance_id, host, selected_port)
                    break
                else:
                    logger.debug("Bootstrap: %s @ %s ports=%s not reachable", instance_id, host, probe_ports)

    def _bootstrap_probe_ports(self, entry: dict, live_entry: dict | None = None) -> list[int]:
        ports: list[int] = []

        def add(value: Any) -> None:
            try:
                port = int(value or 0)
            except Exception:
                return
            if port > 0 and port not in ports:
                ports.append(port)

        live = live_entry or {}
        add(live.get("port"))
        add(entry.get("announced_port"))
        add(entry.get("remote_port"))
        return ports

    def _bootstrap_entry_score(self, entry: dict) -> tuple[int, int, str]:
        caps = list(entry.get("capabilities") or [])
        try:
            protocol = int(float(entry.get("protocol_version") or 0) * 100)
        except Exception:
            protocol = 0
        score = 0
        score += protocol
        score += len(caps) * 10
        if _normalize_identity(entry.get("host_identity") or ""):
            score += 25
        if str(entry.get("environment_kind") or "").strip().lower():
            score += 10
        if entry.get("same_host_loopback"):
            score += 5
        return score, len(caps), str(entry.get("instance_id") or "").upper()

    def _bootstrap_entry_primary_host(self, entry: dict) -> str:
        if not isinstance(entry, dict):
            return ""
        for key in ("lan_ip", "api_host", "tailscale_ip"):
            host = str(entry.get(key) or "").strip().lower()
            if host and host not in {"127.0.0.1", "localhost", "0.0.0.0"}:
                return host
        for item in entry.get("address_candidates") or []:
            if not isinstance(item, dict):
                continue
            host = str(item.get("host") or "").strip().lower()
            scope = str(item.get("scope") or "").strip().lower()
            if host and host not in {"127.0.0.1", "localhost", "0.0.0.0"} and scope in {"lan", "peer", "overlay", "routable"}:
                return host
        hosts = self._candidate_hosts_for_entry(entry)
        for host in hosts:
            host = str(host or "").strip().lower()
            if host and host not in {"127.0.0.1", "localhost", "0.0.0.0"}:
                return host
        return ""

    def _bootstrap_entry_endpoint_key(self, entry: dict) -> tuple[str, int, int] | None:
        if not isinstance(entry, dict):
            return None
        remote_port = int(entry.get("remote_port") or 0)
        if remote_port <= 0:
            return None
        workbench_port = int(entry.get("workbench_port") or 18800)
        primary_host = self._bootstrap_entry_primary_host(entry)
        if not primary_host:
            return None
        return primary_host, remote_port, workbench_port

    def _dedupe_bootstrap_instances(self, instances: dict) -> dict:
        if not isinstance(instances, dict):
            return {}
        best_by_endpoint: dict[tuple[str, int, int], tuple[str, dict]] = {}
        for key, entry in instances.items():
            if not isinstance(entry, dict):
                continue
            endpoint_key = self._bootstrap_entry_endpoint_key(entry)
            if endpoint_key is None:
                continue
            chosen = best_by_endpoint.get(endpoint_key)
            if chosen is None or self._bootstrap_entry_score(entry) > self._bootstrap_entry_score(chosen[1]):
                best_by_endpoint[endpoint_key] = (key, entry)
        keep_keys = {key for key, _entry in best_by_endpoint.values()}
        deduped: dict[str, dict] = {}
        for key, entry in instances.items():
            if not isinstance(entry, dict):
                continue
            endpoint_key = self._bootstrap_entry_endpoint_key(entry)
            if endpoint_key is not None and key not in keep_keys:
                instance_id = str(entry.get("instance_id") or key).upper()
                winner = best_by_endpoint[endpoint_key][1]
                winner_id = str(winner.get("instance_id") or best_by_endpoint[endpoint_key][0]).upper()
                logger.info("Bootstrap: skipping duplicate alias %s in favor of %s", instance_id, winner_id)
                continue
            deduped[key] = entry
        return deduped

    async def _control_loop(self) -> None:
        while self._running:
            try:
                now = time.time()
                if now - self._last_bootstrap_run >= self._bootstrap_retry_seconds:
                    await self._bootstrap_known_peers()
                    self._last_bootstrap_run = now
                if now - self._last_refresh_run >= 30:
                    await self._refresh_peer_liveness_once()
                    self._last_refresh_run = now
                self._refresh_local_agent_snapshot_if_changed()
                if now - self._last_handshake_run >= 5:
                    await self._handshake_once()
                    self._last_handshake_run = now
                await self._process_inflight_once()
            except Exception as exc:
                logger.warning("Protocol control loop failed: %s", exc)
            await asyncio.sleep(self._poll_interval_seconds)

    async def _refresh_peer_liveness_once(self) -> None:
        if not self._peer_registry:
            return
        for peer in self._peer_registry.get_peers():
            await self._refresh_single_peer_liveness(peer)

    async def _refresh_single_peer_liveness(self, peer) -> bool:
        if not self._peer_registry or peer is None:
            return False
        candidate_hosts = self._candidate_hosts_for_peer(peer)
        last_exc = None
        refreshed = False
        for host in candidate_hosts:
            for url in self._candidate_urls(host, peer.port, "/health"):
                try:
                    health = await asyncio.get_running_loop().run_in_executor(
                        None,
                        lambda u=url: self._get_json(u, timeout=4),
                    )
                    if not health or not health.get("ok", True):
                        continue
                    instance = health.get("instance") or {}
                    remote_instance = str(instance.get("instance_id") or peer.instance_id).strip().upper()
                    if remote_instance and remote_instance != peer.instance_id.upper():
                        logger.debug("Liveness refresh ignored mismatched peer %s via %s", remote_instance, url)
                        continue
                    network_profile = health.get("local_network_profile") or {}
                    remote_port = int(instance.get("remote_port") or peer.port or 0)
                    workbench_port = int(instance.get("workbench_port") or peer.workbench_port or 18800)
                    self._peer_registry.mark_refresh_result(
                        peer.instance_id,
                        ok=True,
                        host=host,
                        port=remote_port,
                        workbench_port=workbench_port,
                        address_candidates=list(network_profile.get("address_candidates") or []),
                        observed_candidates=list(network_profile.get("observed_candidates") or []),
                        host_identity=str(network_profile.get("host_identity") or ""),
                        environment_kind=str(network_profile.get("environment_kind") or ""),
                    )
                    refreshed = True
                    break
                except Exception as exc:
                    last_exc = exc
                    logger.debug("Liveness refresh: %s via %s failed: %s", peer.instance_id, url, exc)
            if refreshed:
                break
        if not refreshed:
            self._peer_registry.mark_refresh_result(
                peer.instance_id,
                ok=False,
                last_error=str(last_exc or f"all health probes failed: {candidate_hosts}"),
            )
        return refreshed

    async def _handshake_once(self) -> None:
        if not self._peer_registry:
            return
        for peer in self._peer_registry.get_peers():
            state = str((peer.properties or {}).get("handshake_state") or "handshake_pending")
            last_handshake_at = float((peer.properties or {}).get("last_handshake_at") or 0)
            should_revalidate = state == "handshake_accepted" and (time.time() - last_handshake_at) >= 30
            should_revalidate = should_revalidate or bool(getattr(self, "_force_handshake", False))
            if state == "handshake_in_progress":
                continue
            if state == "handshake_accepted" and not should_revalidate:
                continue
            self._peer_registry.mark_handshake_result(peer.instance_id, state="handshake_in_progress")
            local_profile = self._local_network_profile()
            payload = {
                "from_instance": self._instance_info.get("instance_id"),
                "display_handle": self.display_handle,
                "protocol_version": PROTOCOL_VERSION,
                "capabilities": list(getattr(self, "_capabilities", DEFAULT_CAPABILITIES)),
                "hashi_version": self._instance_info.get("hashi_version", "unknown"),
                "agents": self.get_local_agents_snapshot(),
                "agent_directory": self.get_local_agent_directory_state(),
                "remote_port": self._instance_info.get("remote_port") or 0,
                "workbench_port": self._instance_info.get("workbench_port") or 18800,
                "platform": self._instance_info.get("platform") or "unknown",
                "host_identity": local_profile.get("host_identity"),
                "environment_kind": local_profile.get("environment_kind"),
                "address_candidates": list(local_profile.get("address_candidates") or []),
                "observed_candidates": list(local_profile.get("observed_candidates") or []),
            }
            candidate_hosts = self._candidate_hosts_for_peer(peer)

            succeeded = False
            for host in candidate_hosts:
                for url in self._candidate_urls(host, peer.port, "/protocol/handshake"):
                    try:
                        result = await asyncio.get_running_loop().run_in_executor(
                            None,
                            lambda u=url: self._post_json(u, payload, timeout=self._handshake_timeout_seconds),
                        )
                        remote_instance = str(result.get("instance_id") or "").strip().upper()
                        if remote_instance and remote_instance != peer.instance_id.upper():
                            logger.warning(
                                "Handshake: expected %s via %s but %s responded; ignoring alias endpoint",
                                peer.instance_id,
                                url,
                                remote_instance,
                            )
                            continue
                        if str(result.get("status") or "").lower() == "handshake_reject":
                            self._peer_registry.mark_handshake_result(
                                peer.instance_id,
                                state="handshake_rejected",
                                last_error=str(result.get("reason") or "rejected"),
                            )
                            succeeded = True
                            break
                        # If a fallback host worked, re-register peer with the working host
                        if host != peer.host:
                            from remote.peer.base import PeerInfo
                            updated = dataclasses.replace(peer, host=host)
                            updated.properties = {
                                key: value
                                for key, value in dict(peer.properties or {}).items()
                                if key not in {
                                    "preferred_backend",
                                    "alternate_backends",
                                    "handshake_state",
                                    "last_handshake_at",
                                    "last_error",
                                    "remote_agents",
                                }
                            }
                            updated.properties["discovery"] = "bootstrap_fallback"
                            self._peer_registry.on_peers_changed([updated])
                            logger.info("Handshake: switched %s host from %s to %s", peer.instance_id, peer.host, host)
                        self._peer_registry.mark_handshake_result(
                            peer.instance_id,
                            state="handshake_accepted",
                            protocol_version=str(result.get("protocol_version") or PROTOCOL_VERSION),
                            capabilities=list(result.get("capabilities") or []),
                            remote_agents=list(result.get("agents") or []),
                            remote_agent_directory=dict(result.get("agent_directory") or {}),
                        )
                        succeeded = True
                        break
                    except Exception as exc:
                        logger.debug("Handshake: %s via %s failed: %s", peer.instance_id, url, exc)
                if succeeded:
                    break

            if not succeeded:
                self._peer_registry.mark_handshake_result(
                    peer.instance_id,
                    state="handshake_timed_out",
                    last_error=f"all hosts unreachable: {candidate_hosts}",
                )
        self._force_handshake = False

    def handle_handshake(self, payload: dict) -> dict:
        from_instance = str(payload.get("from_instance") or "").strip().upper()
        if not from_instance:
            return {"status": "handshake_reject", "reason": "missing from_instance"}
        if from_instance == str(self._instance_info.get("instance_id") or "").upper():
            return {"status": "handshake_reject", "reason": "self handshake rejected"}

        # Reverse-register the sender as a peer so we can reach them back.
        # The sender's IP comes from the HTTP request (_client_ip injected by server.py).
        client_ip = str(payload.get("_client_ip") or "").strip()
        remote_port = int(payload.get("remote_port") or 0)
        if client_ip and remote_port and self._peer_registry:
            from remote.peer.base import PeerInfo
            peer = PeerInfo(
                instance_id=from_instance,
                display_name=str(payload.get("display_handle") or from_instance),
                host=client_ip,
                port=remote_port,
                workbench_port=int(payload.get("workbench_port") or 18800),
                platform=str(payload.get("platform") or "unknown"),
                hashi_version=str(payload.get("hashi_version") or "unknown"),
                display_handle=str(payload.get("display_handle") or f"@{from_instance.lower()}"),
                protocol_version=str(payload.get("protocol_version") or PROTOCOL_VERSION),
                capabilities=list(payload.get("capabilities") or []),
                properties={
                    "discovery": "handshake_inbound",
                    "address_candidates": list(payload.get("address_candidates") or []),
                    "observed_candidates": list(payload.get("observed_candidates") or []),
                    "host_identity": _normalize_identity(payload.get("host_identity") or ""),
                    "environment_kind": str(payload.get("environment_kind") or "").strip().lower(),
                    "agent_snapshot_version": str((payload.get("agent_directory") or {}).get("version") or ""),
                    "directory_state": str((payload.get("agent_directory") or {}).get("directory_state") or ""),
                },
            )
            self._peer_registry.on_peers_changed([peer])
            logger.info(
                "Handshake: reverse-registered %s @ %s:%d",
                from_instance, client_ip, remote_port,
            )

        local_profile = self._local_network_profile()
        return {
            "status": "handshake_accept",
            "instance_id": self._instance_info.get("instance_id"),
            "display_handle": self.display_handle,
            "protocol_version": PROTOCOL_VERSION,
            "capabilities": list(self._capabilities),
            "hashi_version": self._instance_info.get("hashi_version", "unknown"),
            "agents": self.get_local_agents_snapshot(),
            "agent_directory": self.get_local_agent_directory_state(),
            "remote_port": self._instance_info.get("remote_port") or 0,
            "workbench_port": self._instance_info.get("workbench_port") or 18800,
            "platform": self._instance_info.get("platform") or "unknown",
            "host_identity": local_profile.get("host_identity"),
            "environment_kind": local_profile.get("environment_kind"),
            "address_candidates": list(local_profile.get("address_candidates") or []),
            "observed_candidates": list(local_profile.get("observed_candidates") or []),
        }

    async def handle_protocol_message(self, payload: dict) -> tuple[int, dict]:
        message_type = str(payload.get("message_type") or "agent_message").strip().lower()
        if message_type == "agent_reply":
            return await self._handle_agent_reply(payload)

        normalized_ttl = min(max(int(payload.get("ttl") or self._max_allowed_ttl), 0), self._max_allowed_ttl)
        if normalized_ttl <= 0:
            return 400, self._error_payload("delivery_expired", "TTL expired or invalid", retryable=False, payload=payload)

        message_id = str(payload.get("message_id") or "").strip()
        conversation_id = str(payload.get("conversation_id") or "").strip()
        from_instance = str(payload.get("from_instance") or "").strip().upper()
        from_agent = str(payload.get("from_agent") or "").strip().lower()
        to_agent = str(payload.get("to_agent") or "").strip().lower()
        route_trace = [str(x).upper() for x in (payload.get("route_trace") or []) if str(x).strip()]
        local_instance = str(self._instance_info.get("instance_id") or "").upper()

        if not all([message_id, conversation_id, from_instance, from_agent, to_agent]):
            return 400, self._error_payload("invalid_message", "Missing required protocol message fields", retryable=False, payload=payload)
        if local_instance in route_trace:
            return 409, self._error_payload("loop_detected", "Local instance already present in route_trace", retryable=False, payload=payload)

        existing = self._inflight.get(message_id)
        if existing:
            state = str(existing.get("state") or "")
            if state in {"reply_sent", "completed"}:
                return 409, self._error_payload("duplicate_message", "Message already completed", retryable=False, payload=payload)
            if state in {"delivery_in_progress", "delivered_to_local_queue", "assistant_started", "assistant_streaming"}:
                return 202, {
                    "ok": True,
                    "message_type": "ack",
                    "message_id": message_id,
                    "conversation_id": conversation_id,
                    "accepted": True,
                    "state": state,
                    "request_id": existing.get("request_id"),
                    "normalized_ttl": existing.get("ttl", normalized_ttl),
                }

        # If message is addressed to a different instance, forward it there.
        to_instance = str(payload.get("to_instance") or "").strip().upper()
        if to_instance and to_instance != local_instance:
            peer = self._peer_registry.get_peer(to_instance) if self._peer_registry else None
            if peer is None:
                return 404, self._error_payload(
                    "target_instance_not_found",
                    f"Target instance '{to_instance}' not in peer registry",
                    retryable=True, payload=payload,
                )
            live_status = str((peer.properties or {}).get("live_status") or "").strip().lower()
            if live_status in {"stale", "offline"}:
                await self._refresh_single_peer_liveness(peer)
                peer = self._peer_registry.get_peer(to_instance) if self._peer_registry else peer
            # Add ourselves to route_trace before forwarding
            forward_payload = dict(payload)
            forward_payload["route_trace"] = list(route_trace) + [local_instance]
            forward_payload["hop_count"] = int(payload.get("hop_count") or 0) + 1
            forward_payload["ttl"] = normalized_ttl - 1
            fwd_hosts = self._candidate_hosts_for_peer(peer)
            fwd_exc = None
            for fwd_host in fwd_hosts:
                for fwd_url in self._candidate_urls(fwd_host, peer.port, "/protocol/message"):
                    try:
                        result = await asyncio.get_running_loop().run_in_executor(
                            None,
                            lambda u=fwd_url: self._post_json(u, forward_payload, timeout=4),
                        )
                        if self._response_is_error(result):
                            raise RuntimeError(result)
                        return 202, result
                    except Exception as exc:
                        fwd_exc = exc
                        logger.debug("Forward: %s via %s failed: %s", to_instance, fwd_url, exc)
            return 502, self._error_payload(
                "forward_failed",
                f"Failed to forward to {to_instance} (tried {fwd_hosts}): {fwd_exc}",
                retryable=True, payload=payload,
            )

        local_agents = {item["agent_name"] for item in self.get_local_agents_snapshot()}
        if to_agent not in local_agents:
            return 404, self._error_payload("target_agent_not_found", f"Target agent '{to_agent}' not found", retryable=False, payload=payload)

        prompt_text = self._render_remote_message_prompt(from_agent, from_instance, payload.get("body") or {})
        start_offset = await self._get_transcript_offset(to_agent)
        request_id = await self._enqueue_local_prompt(to_agent, prompt_text)
        if not request_id:
            return 502, self._error_payload("local_enqueue_failed", "Workbench enqueue failed", retryable=True, payload=payload)

        self._inflight[message_id] = {
            "message_id": message_id,
            "conversation_id": conversation_id,
            "from_instance": from_instance,
            "from_agent": from_agent,
            "to_instance": local_instance,
            "to_agent": to_agent,
            "request_id": request_id,
            "prompt_text": prompt_text,
            "state": "delivered_to_local_queue",
            "matched_user_prompt": False,
            "transcript_offset_at_enqueue": start_offset,
            "last_seen_offset": start_offset,
            "assistant_segments": [],
            "reply_target_agent": from_agent,
            "settle_deadline": 0,
            "reply_soft_deadline": time.time() + self._reply_soft_timeout_seconds,
            "reply_hard_deadline": time.time() + self._reply_hard_timeout_seconds,
            "updated_at": int(time.time()),
            "ttl": normalized_ttl,
        }
        self._save_inflight()
        return 202, {
            "ok": True,
            "message_type": "ack",
            "message_id": message_id,
            "conversation_id": conversation_id,
            "accepted": True,
            "state": "delivered_to_local_queue",
            "request_id": request_id,
            "normalized_ttl": normalized_ttl,
        }

    async def _handle_agent_reply(self, payload: dict) -> tuple[int, dict]:
        to_agent = str(payload.get("to_agent") or "").strip().lower()
        from_agent = str(payload.get("from_agent") or "").strip().lower()
        from_instance = str(payload.get("from_instance") or "").strip().upper()
        body = payload.get("body") or {}
        if not to_agent:
            return 400, self._error_payload("invalid_reply", "Missing to_agent for agent_reply", retryable=False, payload=payload)
        local_agents = {item["agent_name"] for item in self.get_local_agents_snapshot()}
        if to_agent not in local_agents:
            return 404, self._error_payload("target_agent_unavailable", f"Reply target '{to_agent}' is unavailable", retryable=True, payload=payload)
        prompt_text = self._render_remote_reply_prompt(from_agent, from_instance, body)
        request_id = await self._enqueue_local_prompt(to_agent, prompt_text)
        if not request_id:
            return 502, self._error_payload("local_enqueue_failed", "Failed to inject reply into local agent", retryable=True, payload=payload)
        return 202, {
            "ok": True,
            "message_type": "ack",
            "accepted": True,
            "state": "reply_delivered_locally",
            "request_id": request_id,
            "in_reply_to": payload.get("in_reply_to"),
            "conversation_id": payload.get("conversation_id"),
        }

    def _render_remote_message_prompt(self, from_agent: str, from_instance: str, body: dict) -> str:
        text = str((body or {}).get("text") or "").strip()
        return f"System exchange message from {from_agent}@{from_instance}:\n{text}"

    def _render_remote_reply_prompt(self, from_agent: str, from_instance: str, body: dict) -> str:
        text = str((body or {}).get("text") or "").strip()
        return f"System exchange reply from {from_agent}@{from_instance}:\n{text}"

    async def _enqueue_local_prompt(self, agent_name: str, text: str) -> str | None:
        payload = {"agent": agent_name, "text": text}
        last_exc = None
        for host in local_http_hosts():
            url = local_http_url(self._workbench_port, "/api/chat", host=host)
            try:
                result = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda u=url: self._post_json(u, payload, timeout=10),
                )
                if result.get("ok"):
                    return str(result.get("request_id") or "")
            except Exception as exc:
                last_exc = exc
        if last_exc:
            logger.warning("Protocol local enqueue failed: %s", last_exc)
        return None

    async def _get_transcript_offset(self, agent_name: str) -> int:
        for host in local_http_hosts():
            url = local_http_url(self._workbench_port, f"/api/transcript/{agent_name}?limit=1", host=host)
            try:
                result = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda u=url: self._get_json(u, timeout=10),
                )
                return int(result.get("offset") or 0)
            except Exception:
                continue
        return 0

    async def _poll_transcript(self, agent_name: str, offset: int) -> dict:
        last_exc = None
        for host in local_http_hosts():
            url = local_http_url(self._workbench_port, f"/api/transcript/{agent_name}/poll?offset={offset}", host=host)
            try:
                return await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda u=url: self._get_json(u, timeout=10),
                )
            except Exception as exc:
                last_exc = exc
        raise last_exc or RuntimeError("local transcript poll failed")

    async def _process_inflight_once(self) -> None:
        if not self._inflight:
            return
        dirty = False
        now = time.time()
        for message_id, item in list(self._inflight.items()):
            state = str(item.get("state") or "")
            if state in {"reply_sent", "failed", "timed_out"}:
                continue
            if now >= float(item.get("reply_hard_deadline") or 0):
                item["state"] = "timed_out"
                item["updated_at"] = int(now)
                self._inflight[message_id] = item
                dirty = True
                continue
            if state == "reply_failed" and item.get("reply_text"):
                sent = await self._send_agent_reply(item, str(item.get("reply_text") or ""))
                item["state"] = "reply_sent" if sent else "reply_failed"
                item["updated_at"] = int(now)
                self._inflight[message_id] = item
                dirty = True
                continue
            try:
                changed = await self._advance_inflight_item(item, now=now)
                dirty = dirty or changed
                self._inflight[message_id] = item
            except Exception as exc:
                logger.warning("Failed processing inflight %s: %s", message_id, exc)
        if dirty:
            self._save_inflight()

    async def _advance_inflight_item(self, item: dict, *, now: float) -> bool:
        agent_name = str(item.get("to_agent") or "").lower()
        data = await self._poll_transcript(agent_name, int(item.get("last_seen_offset") or 0))
        item["last_seen_offset"] = int(data.get("offset") or item.get("last_seen_offset") or 0)
        messages = data.get("messages") or []
        changed = False

        for message in messages:
            role = str(message.get("role") or "")
            text = str(message.get("text") or "")
            if not text:
                continue
            if not item.get("matched_user_prompt") and role == "user" and text == item.get("prompt_text"):
                item["matched_user_prompt"] = True
                item["state"] = "matched_user_prompt"
                changed = True
                continue
            if item.get("matched_user_prompt") and role == "assistant":
                segments = list(item.get("assistant_segments") or [])
                if not segments or segments[-1] != text:
                    segments.append(text)
                    item["assistant_segments"] = segments
                    item["state"] = "assistant_streaming" if len(segments) > 1 else "assistant_started"
                    item["settle_deadline"] = now + self._settle_window_seconds
                    changed = True

        if item.get("assistant_segments") and float(item.get("settle_deadline") or 0) and now >= float(item.get("settle_deadline") or 0):
            reply_text = "\n\n".join(str(x).strip() for x in item.get("assistant_segments") or [] if str(x).strip()).strip()
            if not reply_text:
                item["state"] = "failed"
                changed = True
                return changed
            sent = await self._send_agent_reply(item, reply_text)
            item["reply_text"] = reply_text
            item["state"] = "reply_sent" if sent else "reply_failed"
            item["updated_at"] = int(now)
            changed = True
        return changed

    async def _send_agent_reply(self, item: dict, reply_text: str) -> bool:
        instance_id = str(item.get("from_instance") or "")
        peer = self._peer_registry.get_peer(instance_id) if self._peer_registry else None
        if peer is not None:
            live_status = str((peer.properties or {}).get("live_status") or "").strip().lower()
            if live_status in {"stale", "offline"}:
                await self._refresh_single_peer_liveness(peer)
        route = self._resolve_peer_route(instance_id)
        if route is None:
            logger.warning("Cannot send reply for %s: origin peer unavailable", item.get("message_id"))
            return False
        payload = {
            "message_type": "agent_reply",
            "message_id": f"{item['message_id']}:reply",
            "conversation_id": item.get("conversation_id"),
            "in_reply_to": item.get("message_id"),
            "from_instance": self._instance_info.get("instance_id"),
            "from_agent": item.get("to_agent"),
            "to_instance": item.get("from_instance"),
            "to_agent": item.get("reply_target_agent") or item.get("from_agent"),
            "body": {"text": reply_text},
            "hop_count": 0,
            "ttl": min(int(item.get("ttl") or self._max_allowed_ttl), self._max_allowed_ttl),
            "route_trace": [str(self._instance_info.get("instance_id") or "").upper()],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        last_exc = None
        for url in self._candidate_urls(route["host"], route["port"], "/protocol/message"):
            try:
                result = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda u=url: self._post_json(u, payload, timeout=10),
                )
                if self._response_is_error(result):
                    raise RuntimeError(result)
                return bool(result.get("ok", True))
            except Exception as exc:
                last_exc = exc
                logger.warning("Reply send via %s failed: %s", url, exc)
        logger.warning("Reply send failed to %s: %s", route.get("instance_id"), last_exc)
        return False

    def _error_payload(self, code: str, message: str, *, retryable: bool, payload: dict) -> dict:
        return {
            "ok": False,
            "message_type": "error",
            "body": {
                "code": code,
                "message": message,
                "retryable": bool(retryable),
                "failed_message_id": payload.get("message_id"),
                "conversation_id": payload.get("conversation_id"),
                "from_instance": payload.get("from_instance"),
                "from_agent": payload.get("from_agent"),
                "to_instance": payload.get("to_instance") or self._instance_info.get("instance_id"),
                "to_agent": payload.get("to_agent"),
                "details": {},
            },
        }

    def _get_json(self, url: str, timeout: int = 10) -> dict:
        req = urllib_request.Request(url, method="GET")
        context = ssl._create_unverified_context() if str(url).startswith("https://") else None
        with urllib_request.urlopen(req, timeout=timeout, context=context) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post_json(self, url: str, payload: dict, timeout: int = 10) -> dict:
        body_bytes = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        path = urlsplit(url).path
        if self._shared_token and path in {"/protocol/handshake", "/protocol/message"}:
            headers.update(
                build_auth_headers(
                    shared_token=self._shared_token,
                    method="POST",
                    path=path,
                    from_instance=str(self._instance_info.get("instance_id") or ""),
                    body_bytes=body_bytes,
                )
            )
        req = urllib_request.Request(
            url,
            data=body_bytes,
            headers=headers,
            method="POST",
        )
        context = ssl._create_unverified_context() if str(url).startswith("https://") else None
        try:
            with urllib_request.urlopen(req, timeout=timeout, context=context) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8")
            try:
                result = json.loads(body) if body else {}
            except Exception:
                raise
            if isinstance(result, dict):
                result["__http_status"] = exc.code
            return result

    def _response_is_error(self, result: dict) -> bool:
        if not isinstance(result, dict):
            return True
        status = int(result.get("__http_status") or 200)
        if status >= 400:
            return True
        if result.get("message_type") == "error":
            return True
        return result.get("ok", True) is False

    def _candidate_urls(self, host: str, port: int, path: str) -> list[str]:
        try:
            port_num = int(port)
        except Exception:
            port_num = 0
        schemes = ("https", "http") if self._use_tls else ("http", "https")
        return [f"{scheme}://{host}:{port_num}{path}" for scheme in schemes]

    def _load_json(self, path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            return {}

    def _load_instances(self) -> dict:
        path = self._hashi_root / "instances.json"
        data = self._load_json(path)
        return data.get("instances", {}) if isinstance(data, dict) else {}

    def _same_machine_hint(self, entry: dict) -> bool:
        if not isinstance(entry, dict):
            return False
        instances = self._load_instances()
        local_entry = instances.get(str(self._instance_info.get("instance_id") or "").lower(), {})
        local_profile = self._local_network_profile()
        local_entry = dict(local_entry)
        local_entry.setdefault("platform", self._instance_info.get("platform") or local_profile.get("environment_kind"))
        return same_machine_hint(
            local_entry=local_entry,
            target_entry=entry,
            target_platform=str(entry.get("platform") or ""),
            local_profile=local_profile,
        )

    def _candidate_hosts_for_entry(self, entry: dict) -> list[str]:
        return [candidate.host for candidate in self._route_candidates_for_entry(entry)]

    def _candidate_hosts_for_peer(self, peer) -> list[str]:
        entry = self._load_instances().get(str(peer.instance_id or "").lower(), {})
        candidates = self._route_candidates_for_peer(peer) if peer is not None else []
        return [candidate.host for candidate in candidates]

    def _route_candidates_for_entry(self, entry: dict) -> list:
        if not isinstance(entry, dict):
            return []
        port = int(entry.get("remote_port") or 0)
        if port <= 0:
            return []
        return build_route_candidates(
            target_entry=entry,
            remote_port=port,
            same_host=self._same_machine_hint(entry),
            address_candidates=list(entry.get("address_candidates") or []),
        )

    def _route_candidates_for_peer(self, peer) -> list:
        entry = self._load_instances().get(str(peer.instance_id or "").lower(), {})
        if not isinstance(entry, dict):
            entry = {}
        remote_port = int(getattr(peer, "port", 0) or entry.get("remote_port") or 0)
        if remote_port <= 0:
            return []
        return build_route_candidates(
            target_entry=entry,
            remote_port=remote_port,
            same_host=self._same_machine_hint(entry) if entry else False,
            address_candidates=list(entry.get("address_candidates") or []) + list((peer.properties or {}).get("address_candidates") or []),
            peer_host=str(peer.host or ""),
        )

    def get_route_diagnostics(self) -> dict[str, Any]:
        instances = self._load_instances()
        return {
            "port_conflicts": validate_same_host_port_conflicts(instances),
            "local_instance": str(self._instance_info.get("instance_id") or "").upper(),
        }

    def _probe_route(self, host: str, port: int, timeout: int = 2) -> bool:
        for url in self._candidate_urls(host, port, "/health"):
            req = urllib_request.Request(url, method="GET")
            try:
                context = ssl._create_unverified_context() if str(url).startswith("https://") else None
                with urllib_request.urlopen(req, timeout=timeout, context=context):
                    return True
            except HTTPError:
                return True
            except URLError:
                continue
            except Exception:
                continue
        return False

    def resolve_forward_urls(self, instance_id: str, path: str) -> list[str]:
        route = self._resolve_peer_route(instance_id)
        if route is None:
            return []
        return self._candidate_urls(route["host"], route["port"], path)

    def _resolve_peer_route(self, instance_id: str):
        peer = self._peer_registry.get_peer(str(instance_id or "")) if self._peer_registry else None
        entry = self._load_instances().get(str(instance_id or "").lower())
        if peer is not None:
            candidates = self._candidate_hosts_for_peer(peer)
            selected_host = str(peer.host or "").strip() or (candidates[0] if candidates else "")
            for host in candidates:
                if self._probe_route(host, int(peer.port)):
                    selected_host = host
                    break
            return {"host": selected_host, "port": peer.port, "instance_id": peer.instance_id}
        if not isinstance(entry, dict):
            return None
        port = entry.get("remote_port")
        if not port:
            return None
        candidates = self._candidate_hosts_for_entry(entry)
        if not candidates:
            return None
        selected_host = candidates[0]
        for host in candidates:
            if self._probe_route(host, int(port)):
                selected_host = host
                break
        return {
            "host": selected_host,
            "port": int(port),
            "instance_id": str(entry.get("instance_id") or instance_id).upper(),
        }

    def _display_network_host(self, entry: dict, peer) -> str:
        candidates: list[str] = []
        seen: set[str] = set()

        def _add(host: str) -> None:
            host = str(host or "").strip()
            if not host or host in {"127.0.0.1", "localhost", "0.0.0.0"} or host in seen:
                return
            seen.add(host)
            candidates.append(host)

        if isinstance(entry, dict):
            for key in ("lan_ip", "tailscale_ip", "api_host"):
                _add(entry.get(key))
        for item in (peer.properties or {}).get("address_candidates") or []:
            if isinstance(item, dict):
                scope = str(item.get("scope") or "").strip().lower()
                if scope in {"lan", "overlay", "routable", "peer"}:
                    _add(item.get("host"))
        return candidates[0] if candidates else ""

    def get_peer_view(self, peer) -> dict:
        data = peer.to_dict()
        entry = self._load_instances().get(str(peer.instance_id or "").lower(), {})
        route_host = str(peer.host or "").strip()
        route_port = int(peer.port or 0)
        if not route_host and isinstance(entry, dict):
            candidates = self._candidate_hosts_for_entry(entry)
            if candidates:
                route_host = candidates[0]
        if not route_port and isinstance(entry, dict):
            try:
                route_port = int(entry.get("remote_port") or 0)
            except (TypeError, ValueError):
                route_port = 0
        same_host = route_host in {"127.0.0.1", "localhost"} and self._same_machine_hint(entry)
        data["resolved_route_host"] = route_host
        data["resolved_route_port"] = route_port
        data["display_network_host"] = self._display_network_host(entry, peer)
        data["same_host"] = same_host
        data["route_kind"] = "same_host" if same_host else str((peer.properties or {}).get("preferred_backend") or (peer.properties or {}).get("discovery") or "unknown")
        return data

    def _save_inflight(self) -> None:
        self._inflight_path.write_text(
            json.dumps({"messages": self._inflight}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
