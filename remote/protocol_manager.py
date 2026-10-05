"""
Protocol manager for Hashi Remote peer-to-peer messaging.

This is the service-owned control plane for:
  - peer handshake
  - active agent directory exchange
  - merged peer state inspection
  - protocol message ingress
  - durable Session/Run reply correlation
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import json
import logging
import socket
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

from orchestrator.hchat_attachment_contract import (
    HCHAT_ATTACHMENT_CLAIM_KEY,
    canonical_hchat_attachment_manifest,
)
from orchestrator.runtime_defaults import DEFAULT_HASHI_REMOTE_PORT, DEFAULT_WORKBENCH_PORT
from orchestrator.service_endpoints import (
    ServiceEndpointError,
    load_service_endpoint,
)
from remote.routing import build_route_candidates, same_machine_hint, validate_same_host_port_conflicts
from remote.local_http import local_http_hosts, local_http_url
from remote.live_endpoints import read_live_endpoints
from remote.peer.base import is_valid_instance_id
from remote.security.shared_token import (
    HEADER_NONCE,
    build_auth_headers,
    load_shared_token,
    verify_response_auth,
)

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


def _is_loopback_host(value: str | None) -> bool:
    host = str(value or "").strip().lower()
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _pick_best_payload_host(
    client_ip: str | None,
    address_candidates: list[dict] | None = None,
    observed_candidates: list[dict] | None = None,
) -> str:
    preferred_scopes = {"lan", "overlay", "routable", "peer"}
    direct = str(client_ip or "").strip()
    if direct and not _is_loopback_host(direct):
        return direct

    for items in (observed_candidates or [], address_candidates or []):
        for item in items:
            if not isinstance(item, dict):
                continue
            host = str(item.get("host") or "").strip()
            scope = str(item.get("scope") or "").strip().lower()
            if host and scope in preferred_scopes and not _is_loopback_host(host):
                return host

    return direct or "127.0.0.1"

PROTOCOL_VERSION = "2.0"
DEFAULT_REMOTE_PORT = DEFAULT_HASHI_REMOTE_PORT
DEFAULT_CAPABILITIES = [
    "handshake_v2",
    "agent_directory_v1",
    "protocol_message_v1",
    "agent_reply_v1",
    "rescue_control",
    "tui_proxy_v1",
    "message_source_context_v1",
    "private_authorization_proof_v1",
]
TERMINAL_INFLIGHT_STATES = {
    "reply_sent",
    "reply_unknown",
    "reply_failed",
    "failed",
    # Read-only compatibility for records written before reply wall-clock
    # ceilings were retired. New protocol messages never enter this state.
    "timed_out",
    "abandoned_after_restart",
    "reply_delivered_locally",
}


def build_default_capabilities(*, rescue_start_enabled: bool = False) -> list[str]:
    capabilities = list(DEFAULT_CAPABILITIES)
    if rescue_start_enabled:
        capabilities.extend(("rescue_start", "rescue_restart", "rescue_reboot"))
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
        use_tls: bool = True,
        discovery_status_provider: Callable[[], dict[str, Any]] | None = None,
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
        self._use_tls = bool(use_tls)
        self._discovery_status_provider = discovery_status_provider
        self._bootstrap_retry_seconds = 60.0
        self._state_dir = Path.home() / ".hashi-remote"
        self._state_dir.mkdir(parents=True, exist_ok=True)
        instance_key = str(instance_info.get("instance_id") or "hashi").lower()
        self._inflight_path = self._state_dir / f"protocol_inflight_{instance_key}.json"
        self._outbound_path = self._state_dir / f"protocol_outbound_{instance_key}.json"
        self._inflight: dict[str, dict[str, Any]] = self._load_json(self._inflight_path).get("messages", {})
        if self._mark_nonterminal_inflight_abandoned_after_restart():
            self._save_inflight()
        self._shared_token = load_shared_token(self._hashi_root)
        self._credential_generation = 1
        self._last_credential_reload_at = 0
        self._credential_config_state = "configured" if self._shared_token else "absent"
        self._credential_config_error = ""
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
        self._workbench_identity_cache: dict[
            tuple[str, int], tuple[float, bool]
        ] = {}

    def get_protocol_status(self) -> dict:
        peers = []
        peer_trust_incomplete = False
        if self._peer_registry:
            for peer in self._peer_registry.get_peers():
                peers.append(self._peer_registry.get_peer_state(peer.instance_id))
                peer_trust_incomplete = peer_trust_incomplete or str(
                    (peer.properties or {}).get("handshake_state") or "handshake_pending"
                ).strip().lower() != "handshake_accepted"
        local_profile = self._local_network_profile()
        active_inflight_count = sum(
            1
            for item in self._inflight.values()
            if str((item or {}).get("state") or "") not in TERMINAL_INFLIGHT_STATES
        )
        total_inflight_count = len(self._inflight)
        discovery = {}
        discovery_status_provider = getattr(self, "_discovery_status_provider", None)
        if callable(discovery_status_provider):
            try:
                discovery = dict(discovery_status_provider() or {})
            except Exception as exc:
                discovery = {
                    "readiness": "degraded",
                    "ready": False,
                    "last_error": f"status unavailable: {type(exc).__name__}",
                }
        return {
            "protocol_version": PROTOCOL_VERSION,
            "display_handle": self.display_handle,
            "capabilities": list(self._capabilities),
            "remote_supervisor": dict(self._instance_info.get("remote_supervisor") or {}),
            "local_agents": self.get_local_agents_snapshot(),
            "local_agent_directory": self.get_local_agent_directory_state(),
            "local_network_profile": local_profile,
            "route_diagnostics": self.get_route_diagnostics(),
            "discovery": discovery,
            "credential": {
                "configured": bool(getattr(self, "_shared_token", None)),
                "generation": int(getattr(self, "_credential_generation", 1)),
                "last_reload_at": int(getattr(self, "_last_credential_reload_at", 0)),
                "rehandshake_required": bool(
                    getattr(self, "_force_handshake", False)
                    or (
                        getattr(self, "_shared_token", None)
                        and peer_trust_incomplete
                    )
                ),
                "configuration_state": str(
                    getattr(self, "_credential_config_state", "unknown")
                ),
                "configuration_error": str(
                    getattr(self, "_credential_config_error", "")
                ),
            },
            "peers": peers,
            "inflight_count": active_inflight_count,
            "inflight_total_count": total_inflight_count,
            "inflight_terminal_count": total_inflight_count - active_inflight_count,
            "max_allowed_ttl": self._max_allowed_ttl,
        }

    def reload_shared_token(self, token: str | None) -> bool:
        """Adopt a credential change and invalidate every established trust result."""
        normalized = str(token or "").strip() or None
        if normalized == self._shared_token:
            return False
        self._shared_token = normalized
        self._credential_generation += 1
        self._last_credential_reload_at = int(time.time())
        self._force_handshake = True
        if self._peer_registry:
            for peer in self._peer_registry.get_peers():
                self._peer_registry.mark_handshake_result(
                    peer.instance_id,
                    state="rehydrate_required",
                )
        return True

    def record_shared_token_config_state(self, state: str, error: str = "") -> None:
        allowed_states = {
            "absent",
            "configured",
            "configured_environment",
            "configured_file",
            "invalid",
        }
        normalized = str(state or "unknown").strip().lower()
        self._credential_config_state = normalized if normalized in allowed_states else "unknown"
        # The loader emits classifications only; never surface exception text or bytes.
        self._credential_config_error = str(error or "")[:120]

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
            workbench_port=int(self._instance_info.get("workbench_port") or DEFAULT_WORKBENCH_PORT),
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
        ok = any(
            self._probe_local_workbench(host, port, timeout=0.4)
            for host, port in self._local_workbench_routes()
        )
        self._core_health_cache = (now, ok)
        return ok

    def _local_workbench_routes(self) -> list[tuple[str, int]]:
        """Resolve the current local Workbench receipt before config fallback."""

        routes: list[tuple[str, int]] = []

        def add(host: Any, port: Any) -> None:
            text = str(host or "").strip()
            try:
                number = int(port or 0)
            except (TypeError, ValueError):
                return
            route = (text, number)
            if text and 1 <= number <= 65535 and route not in routes:
                routes.append(route)

        instance_id = str(
            (getattr(self, "_instance_info", {}) or {}).get("instance_id") or ""
        ).strip().upper()
        root = getattr(self, "_hashi_root", None)
        if instance_id and root is not None:
            try:
                endpoint = load_service_endpoint(
                    Path(root) / "state" / "service_endpoints.json",
                    "workbench",
                    expected_instance=instance_id,
                )
                add(endpoint.host, endpoint.port)
                for host in local_http_hosts():
                    add(host, endpoint.port)
            except (ServiceEndpointError, TypeError, ValueError):
                pass
        try:
            configured_port = int(
                (getattr(self, "_instance_info", {}) or {}).get("workbench_port")
                or getattr(self, "_workbench_port", DEFAULT_WORKBENCH_PORT)
                or DEFAULT_WORKBENCH_PORT
            )
        except (TypeError, ValueError):
            configured_port = DEFAULT_WORKBENCH_PORT
        for host in local_http_hosts():
            add(host, configured_port)
        return routes

    def _probe_local_workbench(
        self,
        host: str,
        port: int,
        *,
        timeout: float = 0.6,
    ) -> bool:
        """Require the exact local instance identity, not merely an open port."""

        key = (str(host), int(port))
        now = time.monotonic()
        cache = getattr(self, "_workbench_identity_cache", {})
        cached = cache.get(key)
        if cached is not None and now - cached[0] <= 1.0:
            return cached[1]
        expected = str(
            (getattr(self, "_instance_info", {}) or {}).get("instance_id") or ""
        ).strip().upper()
        ok = False
        if expected:
            try:
                health = self._get_json(
                    local_http_url(port, "/api/health", host=host),
                    timeout=timeout,
                )
                actual = str(health.get("instance_id") or "").strip().upper()
                endpoint = health.get("workbench_endpoint")
                endpoint_owner = (
                    str(endpoint.get("instance_id") or "").strip().upper()
                    if isinstance(endpoint, dict)
                    else actual
                )
                ok = (
                    health.get("ok") is True
                    and actual == expected
                    and endpoint_owner == expected
                )
            except Exception:
                ok = False
        cache[key] = (now, ok)
        self._workbench_identity_cache = cache
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
            existing_peer = self._peer_registry.get_peer(instance_id) if self._peer_registry else None
            if existing_peer and self._bootstrap_existing_peer_is_healthy(existing_peer):
                continue  # Already known via a healthy discovery path.
            if existing_peer and self._bootstrap_existing_peer_has_live_discovery(existing_peer):
                continue  # Live discovery already owns this peer; don't re-inject fallback routes.
            live_entry = live_endpoints.get(instance_id.lower(), {})
            probe_ports = self._bootstrap_probe_ports(entry, live_entry)
            if not probe_ports:
                logger.debug("Bootstrap: %s has no live or fallback probe ports, skipping", instance_id)
                continue
            seen_hosts = self._candidate_hosts_for_entry(entry)

            for host in seen_hosts:
                selected_port = None
                for remote_port in probe_ports:
                    reachable = await asyncio.get_running_loop().run_in_executor(
                        None,
                        lambda h=host, p=int(remote_port), iid=instance_id: self._probe_instance_route(h, p, iid, timeout=2),
                    )
                    if reachable:
                        selected_port = int(remote_port)
                        break
                if selected_port:
                    from remote.peer.base import PeerInfo
                    peer = PeerInfo(
                        instance_id=instance_id,
                        display_name=str(live_entry.get("display_name") or entry.get("display_name") or instance_id),
                        host=host,
                        port=selected_port,
                        workbench_port=int(
                            live_entry.get("workbench_port")
                            or entry.get("workbench_port")
                            or DEFAULT_WORKBENCH_PORT
                        ),
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
        add(live.get("remote_port"))
        add(entry.get("announced_port"))
        add(entry.get("remote_port"))
        platform = str(live.get("platform") or entry.get("platform") or "").strip().lower()
        environment = str(live.get("environment_kind") or entry.get("environment_kind") or "").strip().lower()
        if platform in {"windows", "linux", "darwin", "macos"} or environment in {"windows", "linux", "darwin", "macos"}:
            add(DEFAULT_REMOTE_PORT)
        return ports

    def _bootstrap_existing_peer_is_healthy(self, peer) -> bool:
        properties = dict(getattr(peer, "properties", None) or {})
        live_status = str(properties.get("live_status") or "").strip().lower()
        handshake_state = str(properties.get("handshake_state") or "").strip().lower()
        if live_status == "online":
            return True
        return handshake_state == "handshake_accepted"

    def _bootstrap_existing_peer_has_live_discovery(self, peer) -> bool:
        properties = dict(getattr(peer, "properties", None) or {})
        preferred = str(properties.get("preferred_backend") or properties.get("discovery") or "").strip().lower()
        return preferred in {"lan", "tailscale"}

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
        workbench_port = int(entry.get("workbench_port") or DEFAULT_WORKBENCH_PORT)
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
        semaphore = asyncio.Semaphore(4)

        async def refresh(peer) -> bool:
            async with semaphore:
                return await self._refresh_single_peer_liveness(peer)

        await asyncio.gather(*(refresh(peer) for peer in self._peer_registry.get_peers()))

    async def _refresh_single_peer_liveness(self, peer, *, timeout: float = 4) -> bool:
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
                        lambda u=url: self._get_json(u, timeout=timeout),
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
                    workbench_port = int(
                        instance.get("workbench_port")
                        or peer.workbench_port
                        or DEFAULT_WORKBENCH_PORT
                    )
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

    async def refresh_peer_liveness_for_status(
        self,
        *,
        max_age_seconds: int = 75,
        timeout: float = 1.0,
        max_peers: int = 6,
    ) -> dict[str, Any]:
        """Refresh stale/offline peer liveness before rendering operator status."""
        if not self._peer_registry:
            return {"checked": 0, "refreshed": 0, "skipped": 0}
        now = int(time.time())
        checked = 0
        refreshed = 0
        skipped = 0
        for peer in self._peer_registry.get_peers():
            props = peer.properties or {}
            live_status = str(props.get("live_status") or "").strip().lower()
            try:
                last_seen_ok = int(props.get("last_seen_ok") or 0)
            except Exception:
                last_seen_ok = 0
            age = now - last_seen_ok if last_seen_ok > 0 else None
            needs_refresh = live_status in {"", "unknown", "stale", "offline"}
            if age is None or age > max_age_seconds:
                needs_refresh = True
            if not needs_refresh:
                skipped += 1
                continue
            if checked >= max_peers:
                skipped += 1
                continue
            checked += 1
            if await self._refresh_single_peer_liveness(peer, timeout=timeout):
                refreshed += 1
        if checked:
            logger.info("Status liveness refresh: checked=%s refreshed=%s skipped=%s", checked, refreshed, skipped)
        return {"checked": checked, "refreshed": refreshed, "skipped": skipped}

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
            # A periodic revalidation must not erase the last accepted trust
            # state while the new handshake is in flight.  The previous code
            # briefly published zero trusted peers, so an unrelated startup
            # health probe could latch a false Remote degradation.  First-time
            # handshakes still expose their in-progress state as before.
            if state != "handshake_accepted":
                self._peer_registry.mark_handshake_result(
                    peer.instance_id,
                    state="handshake_in_progress",
                )
            local_profile = self._local_network_profile()
            payload = {
                "from_instance": self._instance_info.get("instance_id"),
                "display_name": self._instance_info.get("display_name"),
                "display_handle": self.display_handle,
                "protocol_version": PROTOCOL_VERSION,
                "capabilities": list(getattr(self, "_capabilities", DEFAULT_CAPABILITIES)),
                "hashi_version": self._instance_info.get("hashi_version", "unknown"),
                "agents": self.get_local_agents_snapshot(),
                "agent_directory": self.get_local_agent_directory_state(),
                "remote_port": self._instance_info.get("remote_port") or 0,
                "workbench_port": (
                    self._instance_info.get("workbench_port") or DEFAULT_WORKBENCH_PORT
                ),
                "platform": self._instance_info.get("platform") or "unknown",
                "host_identity": local_profile.get("host_identity"),
                "environment_kind": local_profile.get("environment_kind"),
                "remote_supervisor": dict(self._instance_info.get("remote_supervisor") or {}),
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
                        # A known same-host loopback is an observer-local route,
                        # not a new LAN advertisement. Publishing it as fallback
                        # made every refresh fight the unchanged LAN observation.
                        known_loopback = str((peer.properties or {}).get("same_host_loopback") or "").strip()
                        same_host_route = host in {"127.0.0.1", "localhost", "::1"} and (
                            host == known_loopback or self._same_machine_hint(
                                self._load_instances().get(peer.instance_id.lower(), {}),
                            )
                        )
                        if host != peer.host and not same_host_route:
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
                            hashi_version=str(result.get("hashi_version") or "unknown"),
                            display_name=str(
                                result.get("display_name")
                                or result.get("display_handle")
                                or peer.instance_id
                            ),
                            display_handle=str(
                                result.get("display_handle")
                                or f"@{peer.instance_id.lower()}"
                            ),
                            remote_port=result.get("remote_port"),
                            workbench_port=result.get("workbench_port"),
                            platform=str(result.get("platform") or "unknown"),
                            host_identity=str(result.get("host_identity") or ""),
                            environment_kind=str(
                                result.get("environment_kind") or ""
                            ),
                            address_candidates=list(
                                result.get("address_candidates") or []
                            ),
                            observed_candidates=list(
                                result.get("observed_candidates") or []
                            ),
                            remote_agents=list(result.get("agents") or []),
                            remote_agent_directory=dict(result.get("agent_directory") or {}),
                            remote_supervisor=dict(result.get("remote_supervisor") or {}),
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
            address_candidates = list(payload.get("address_candidates") or [])
            observed_candidates = list(payload.get("observed_candidates") or [])
            effective_host = _pick_best_payload_host(
                client_ip,
                address_candidates=address_candidates,
                observed_candidates=observed_candidates,
            )
            peer = PeerInfo(
                instance_id=from_instance,
                display_name=str(
                    payload.get("display_name")
                    or payload.get("display_handle")
                    or from_instance
                ),
                host=effective_host,
                port=remote_port,
                workbench_port=int(payload.get("workbench_port") or DEFAULT_WORKBENCH_PORT),
                platform=str(payload.get("platform") or "unknown"),
                hashi_version=str(payload.get("hashi_version") or "unknown"),
                display_handle=str(payload.get("display_handle") or f"@{from_instance.lower()}"),
                protocol_version=str(payload.get("protocol_version") or PROTOCOL_VERSION),
                capabilities=list(payload.get("capabilities") or []),
                properties={
                    "discovery": "handshake_inbound",
                    "address_candidates": address_candidates,
                    "observed_candidates": observed_candidates,
                    "host_identity": _normalize_identity(payload.get("host_identity") or ""),
                    "environment_kind": str(payload.get("environment_kind") or "").strip().lower(),
                    "remote_supervisor": dict(payload.get("remote_supervisor") or {}),
                    "agent_snapshot_version": str((payload.get("agent_directory") or {}).get("version") or ""),
                    "directory_state": str((payload.get("agent_directory") or {}).get("directory_state") or ""),
                },
            )
            self._peer_registry.on_peers_changed([peer])
            logger.info(
                "Handshake: reverse-registered %s @ %s:%d",
                from_instance, effective_host, remote_port,
            )

        local_profile = self._local_network_profile()
        return {
            "status": "handshake_accept",
            "instance_id": self._instance_info.get("instance_id"),
            "display_name": self._instance_info.get("display_name"),
            "display_handle": self.display_handle,
            "protocol_version": PROTOCOL_VERSION,
            "capabilities": list(self._capabilities),
            "hashi_version": self._instance_info.get("hashi_version", "unknown"),
            "agents": self.get_local_agents_snapshot(),
            "agent_directory": self.get_local_agent_directory_state(),
            "remote_port": self._instance_info.get("remote_port") or 0,
            "workbench_port": (
                self._instance_info.get("workbench_port") or DEFAULT_WORKBENCH_PORT
            ),
            "platform": self._instance_info.get("platform") or "unknown",
            "host_identity": local_profile.get("host_identity"),
            "environment_kind": local_profile.get("environment_kind"),
            "remote_supervisor": dict(self._instance_info.get("remote_supervisor") or {}),
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
            if state in {"reply_unknown", "reply_failed"}:
                return 409, self._error_payload(
                    "delivery_outcome_unknown",
                    "Prior reply outcome is unknown and will not be replayed",
                    retryable=False,
                    payload=payload,
                )

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
            from orchestrator.agent_move.service import moved_agent_destination

            moved = moved_agent_destination(self._hashi_root, to_agent)
            if moved:
                error = self._error_payload(
                    "agent_moved",
                    f"Target agent '{to_agent}' moved to {moved['address']}; refresh the Agent directory",
                    retryable=False,
                    payload=payload,
                )
                error["body"]["details"] = {"moved_to": moved["address"]}
                return 410, error
            return 404, self._error_payload("target_agent_not_found", f"Target agent '{to_agent}' not found", retryable=False, payload=payload)

        body = payload.get("body") or {}
        attachments = None
        if payload.get("_local_attachment_manifest_verified") is True:
            try:
                attachments = canonical_hchat_attachment_manifest(
                    body.get("attachments") or []
                )
            except ValueError as exc:
                return 400, self._error_payload(
                    "invalid_attachment_manifest",
                    str(exc),
                    retryable=False,
                    payload=payload,
                )
        prompt_text = self._render_remote_message_prompt(
            from_agent, from_instance, body
        )
        enqueue_kwargs = {
            "exchange_kind": "message",
            "message_id": message_id,
            "conversation_id": conversation_id,
            "from_instance": from_instance,
            "from_agent": from_agent,
            "to_instance": local_instance,
            "to_agent": to_agent,
            "route_trace": route_trace,
            "authenticated_peer": str(
                payload.get("_network_authenticated_instance") or ""
            ),
            "network_authentication": str(
                payload.get("_network_authentication") or "not_verified"
            ),
            "private_authorization_proofs": list(
                payload.get("private_authorization_proofs") or []
            ),
            "authorization_resources": list(
                payload.get("authorization_resources") or []
            ),
            "authorization_content_text": str(
                payload.get("_private_authorization_content_text")
                if payload.get("_private_authorization_content_text") is not None
                else body.get("text")
                or ""
            ),
        }
        if attachments:
            enqueue_kwargs["attachments"] = attachments
        local_acceptance = await self._enqueue_local_prompt(
            to_agent,
            prompt_text,
            **enqueue_kwargs,
        )
        if isinstance(local_acceptance, dict):
            request_id = str(local_acceptance.get("request_id") or "")
            session_id = str(local_acceptance.get("session_id") or "")
            run_id = str(local_acceptance.get("run_id") or "")
        else:
            request_id = str(local_acceptance or "")
            session_id = ""
            run_id = ""
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
            "session_id": session_id,
            "run_id": run_id,
            "state": "delivered_to_local_queue",
            "reply_target_agent": from_agent,
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
            "session_id": session_id or None,
            "run_id": run_id or None,
            "normalized_ttl": normalized_ttl,
        }

    async def _handle_agent_reply(self, payload: dict) -> tuple[int, dict]:
        message_id = str(payload.get("message_id") or "").strip()
        conversation_id = str(payload.get("conversation_id") or "").strip()
        in_reply_to = str(payload.get("in_reply_to") or "").strip()
        to_agent = str(payload.get("to_agent") or "").strip().lower()
        from_agent = str(payload.get("from_agent") or "").strip().lower()
        from_instance = str(payload.get("from_instance") or "").strip().upper()
        body = payload.get("body") or {}
        route_trace = [str(x).upper() for x in (payload.get("route_trace") or []) if str(x).strip()]
        local_instance = str(self._instance_info.get("instance_id") or "").strip().upper()
        normalized_ttl = min(max(int(payload.get("ttl") or self._max_allowed_ttl), 0), self._max_allowed_ttl)

        if normalized_ttl <= 0:
            return 400, self._error_payload("delivery_expired", "TTL expired or invalid", retryable=False, payload=payload)
        if not all([message_id, conversation_id, in_reply_to, from_instance, from_agent, to_agent]):
            return 400, self._error_payload("invalid_reply", "Missing required agent_reply fields", retryable=False, payload=payload)
        if local_instance in route_trace:
            return 409, self._error_payload("loop_detected", "Local instance already present in reply route_trace", retryable=False, payload=payload)
        if from_instance == local_instance:
            return 409, self._error_payload("loop_detected", "Refusing local self-bounce agent_reply", retryable=False, payload=payload)

        existing_reply = self._inflight.get(message_id)
        if existing_reply:
            state = str(existing_reply.get("state") or "")
            if state in TERMINAL_INFLIGHT_STATES:
                return 409, self._error_payload("duplicate_message", "Reply message already terminal", retryable=False, payload=payload)
            return 202, {
                "ok": True,
                "message_type": "ack",
                "message_id": message_id,
                "conversation_id": conversation_id,
                "accepted": True,
                "state": state,
                "request_id": existing_reply.get("request_id"),
                "normalized_ttl": existing_reply.get("ttl", normalized_ttl),
            }

        correlation_state = "missing_legacy_allowed"
        correlated = self._inflight.get(in_reply_to)
        outbound_correlation = None
        if correlated:
            correlated_conversation_id = str(correlated.get("conversation_id") or "").strip()
            if correlated_conversation_id and correlated_conversation_id != conversation_id:
                return 409, self._error_payload("reply_correlation_mismatch", "Reply conversation_id does not match correlation record", retryable=False, payload=payload)
            correlation_state = "matched"
        else:
            outbound_correlation = self._outbound_correlation_for(in_reply_to)
            if outbound_correlation:
                correlated_conversation_id = str(outbound_correlation.get("conversation_id") or "").strip()
                if correlated_conversation_id and correlated_conversation_id != conversation_id:
                    return 409, self._error_payload("reply_correlation_mismatch", "Reply conversation_id does not match outbound correlation record", retryable=False, payload=payload)
                correlation_state = "matched_outbound"

        local_agents = {item["agent_name"] for item in self.get_local_agents_snapshot()}
        if to_agent not in local_agents:
            from orchestrator.agent_move.service import moved_agent_destination

            moved = moved_agent_destination(self._hashi_root, to_agent)
            if moved:
                error = self._error_payload(
                    "agent_moved",
                    f"Reply target '{to_agent}' moved to {moved['address']}; refresh the Agent directory",
                    retryable=False,
                    payload=payload,
                )
                error["body"]["details"] = {"moved_to": moved["address"]}
                return 410, error
            return 404, self._error_payload("target_agent_unavailable", f"Reply target '{to_agent}' is unavailable", retryable=True, payload=payload)
        prompt_text = self._render_remote_reply_prompt(from_agent, from_instance, body)
        local_acceptance = await self._enqueue_local_prompt(
            to_agent,
            prompt_text,
            exchange_kind="reply",
            message_id=message_id,
            conversation_id=conversation_id,
            from_instance=from_instance,
            from_agent=from_agent,
            terminal_response_text=str((body or {}).get("text") or "").strip(),
        )
        if isinstance(local_acceptance, dict):
            request_id = str(local_acceptance.get("request_id") or "")
            session_id = str(local_acceptance.get("session_id") or "")
            run_id = str(local_acceptance.get("run_id") or "")
        else:
            request_id = str(local_acceptance or "")
            session_id = ""
            run_id = ""
        if not request_id:
            return 502, self._error_payload("local_enqueue_failed", "Failed to inject reply into local agent", retryable=True, payload=payload)
        now = int(time.time())
        if correlated:
            correlated["state"] = "reply_delivered_locally"
            correlated["reply_message_id"] = message_id
            correlated["reply_delivered_at"] = now
        if outbound_correlation:
            self._mark_outbound_reply_delivered(in_reply_to, reply_message_id=message_id, delivered_at=now)
        self._inflight[message_id] = {
            "message_id": message_id,
            "conversation_id": conversation_id,
            "in_reply_to": in_reply_to,
            "from_instance": from_instance,
            "from_agent": from_agent,
            "to_instance": local_instance,
            "to_agent": to_agent,
            "request_id": request_id,
            "session_id": session_id,
            "run_id": run_id,
            "state": "reply_delivered_locally",
            "correlation_state": correlation_state,
            "route_trace": route_trace,
            "ttl": normalized_ttl,
            "created_at": now,
            "updated_at": now,
        }
        self._save_inflight()
        return 202, {
            "ok": True,
            "message_type": "ack",
            "message_id": message_id,
            "accepted": True,
            "state": "reply_delivered_locally",
            "request_id": request_id,
            "session_id": session_id or None,
            "run_id": run_id or None,
            "in_reply_to": in_reply_to,
            "conversation_id": conversation_id,
            "correlation_state": correlation_state,
            "normalized_ttl": normalized_ttl,
        }

    def _render_remote_message_prompt(self, from_agent: str, from_instance: str, body: dict) -> str:
        text = str((body or {}).get("text") or "").strip()
        return (
            f"System exchange message from {from_agent}@{from_instance}:\n{text}\n\n"
            "Protocol rule: respond once in your normal assistant response. "
            "Do not send Hchat, protocol messages, acknowledgements, or a "
            "separate reply; the protocol returns this response automatically."
        )

    def _render_remote_reply_prompt(self, from_agent: str, from_instance: str, body: dict) -> str:
        text = str((body or {}).get("text") or "").strip()
        return (
            f"System exchange reply from {from_agent}@{from_instance}:\n{text}\n\n"
            "Terminal protocol notice: show the reply body above verbatim in "
            "your normal assistant response so the user can see it. Do not answer "
            "the peer, summarize the body, acknowledge, confirm, or send any "
            "Hchat/protocol message. This reply closes the exchange."
        )

    async def _enqueue_local_prompt(
        self,
        agent_name: str,
        text: str,
        *,
        exchange_kind: str,
        message_id: str,
        conversation_id: str,
        from_instance: str,
        from_agent: str,
        to_instance: str | None = None,
        to_agent: str | None = None,
        route_trace: list[str] | None = None,
        authenticated_peer: str | None = None,
        network_authentication: str = "not_verified",
        private_authorization_proofs: list[dict[str, Any]] | None = None,
        authorization_resources: list[str] | None = None,
        authorization_content_text: str | None = None,
        terminal_response_text: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, str] | None:
        terminal = exchange_kind == "reply"
        request_metadata = {
            "system_exchange": True,
            "system_exchange_kind": exchange_kind,
            "system_exchange_terminal": terminal,
            "protocol_message_id": message_id,
            "protocol_conversation_id": conversation_id,
            "protocol_from_instance": from_instance,
            "protocol_from_agent": from_agent,
            "connector_id": "remote",
            "ingress_transport": "remote.protocol",
            "session_surface": "remote",
            "session_channel_key": (
                f"{str(from_instance).strip().upper()}:"
                f"{str(conversation_id).strip()}"
            ),
        }
        resolved_target_instance = str(
            to_instance
            or getattr(self, "_instance_info", {}).get("instance_id")
            or ""
        ).strip().upper()
        resolved_target_agent = str(to_agent or agent_name).strip().casefold()
        resolved_peer = str(authenticated_peer or "").strip().upper()
        relay_chain = [
            str(item).strip().upper()
            for item in route_trace or ()
            if str(item).strip().upper()
            not in {str(from_instance).strip().upper(), resolved_target_instance}
        ]
        request_metadata.update(
            {
                "_message_source_reserved": "hchat",
                "_hchat_context": {
                    "from_agent": from_agent,
                    "from_instance": from_instance,
                    "to_agent": resolved_target_agent,
                    "to_instance": resolved_target_instance,
                    "authenticated_peer": resolved_peer,
                    "network_authentication": network_authentication,
                    "sender_assurance": (
                        "shared_network_member_declared"
                        if resolved_peer
                        else "declared"
                    ),
                    "relay_chain": relay_chain,
                    "origin_instance": {
                        "id": from_instance,
                        "assurance": (
                            "shared_network_hmac"
                            if resolved_peer == str(from_instance).strip().upper()
                            else "declared"
                        ),
                    },
                },
            }
        )
        normalized_attachments = (
            canonical_hchat_attachment_manifest(attachments)
            if attachments
            else None
        )
        if normalized_attachments:
            request_metadata[HCHAT_ATTACHMENT_CLAIM_KEY] = normalized_attachments
        if private_authorization_proofs:
            from orchestrator.private_authorization import (
                authorization_content_sha256,
            )

            content_digest = authorization_content_sha256(
                text if authorization_content_text is None else authorization_content_text
            )
            request_metadata["_private_authorization_proofs"] = [
                dict(item)
                for item in private_authorization_proofs
                if isinstance(item, dict)
            ]
            request_metadata["_private_authorization_binding"] = {
                "message_id": message_id,
                "from_instance": from_instance,
                "from_agent": from_agent,
                "to_instance": resolved_target_instance,
                "to_agent": resolved_target_agent,
                "content_sha256": content_digest,
                "resources": list(authorization_resources or []),
            }
            request_metadata["_private_authorization_content_sha256"] = (
                content_digest
            )
        from orchestrator.message_context import seal_connector_evidence

        connector_claims = {
            key: request_metadata[key]
            for key in (
                "_message_source_reserved",
                "_hchat_context",
                "_private_authorization_proofs",
                "_private_authorization_binding",
                "_private_authorization_content_sha256",
                HCHAT_ATTACHMENT_CLAIM_KEY,
            )
            if key in request_metadata
        }
        connector_root = getattr(self, "_hashi_root", None)
        connector_evidence = (
            seal_connector_evidence(
                connector_root,
                claims=connector_claims,
                prompt=text,
            )
            if connector_root is not None
            else None
        )
        for key in connector_claims:
            request_metadata.pop(key, None)
        if connector_evidence is not None:
            request_metadata["_connector_evidence"] = connector_evidence
        if terminal:
            # A reply is presentation-only.  Freezing the tool catalogue at
            # the Workbench boundary prevents a model-generated ACK/Hchat
            # from starting a new cross-instance conversation.
            request_metadata["tool_allowlist"] = []
            normalized_terminal_text = str(terminal_response_text or "").strip()
            if normalized_terminal_text:
                # The provider may absorb the reply for session continuity,
                # but the user-visible terminal body remains protocol-owned.
                request_metadata["system_exchange_terminal_text"] = (
                    normalized_terminal_text
                )
        payload = {
            "agent": agent_name,
            "text": text,
            "source": f"protocol:{exchange_kind}",
            "request_metadata": request_metadata,
            "idempotency_key": f"protocol:{exchange_kind}:{message_id}",
        }
        if normalized_attachments:
            payload["remote_attachments"] = normalized_attachments
        last_exc = None
        for host, port in self._local_workbench_routes():
            if not self._probe_local_workbench(host, port):
                continue
            url = local_http_url(port, "/api/chat", host=host)
            try:
                result = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda u=url: self._post_json(u, payload, timeout=10),
                )
                if result.get("ok"):
                    request_id = str(result.get("request_id") or "").strip()
                    if not request_id:
                        continue
                    return {
                        key: value
                        for key, value in {
                            "request_id": request_id,
                            "session_id": str(result.get("session_id") or "").strip(),
                            "run_id": str(result.get("run_id") or "").strip(),
                            "message_id": str(result.get("message_id") or "").strip(),
                        }.items()
                        if value
                    }
            except Exception as exc:
                last_exc = exc
        if last_exc:
            logger.warning("Protocol local enqueue failed: %s", last_exc)
        return None

    async def _resolve_request_identity(
        self, agent_name: str, request_id: str
    ) -> dict[str, str] | None:
        """Recover a legacy in-flight item's durable Session and Run IDs."""

        encoded_agent = quote(str(agent_name), safe="")
        encoded_request = quote(str(request_id), safe="")
        path = (
            f"/api/agents/{encoded_agent}/requests/{encoded_request}"
            "/activity?after_sequence=0&limit=1"
        )
        for host, port in self._local_workbench_routes():
            if not self._probe_local_workbench(host, port):
                continue
            url = local_http_url(port, path, host=host)
            try:
                result = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda u=url: self._get_json(u, timeout=10),
                )
                session_id = str(result.get("session_id") or "").strip()
                run_id = str(result.get("run_id") or "").strip()
                if result.get("ok") and session_id and run_id:
                    return {
                        "session_id": session_id,
                        "run_id": run_id,
                        "request_id": str(
                            result.get("request_id") or request_id
                        ).strip(),
                    }
            except Exception:
                continue
        return None

    async def _poll_session_result(
        self, agent_name: str, session_id: str, run_id: str
    ) -> dict[str, Any]:
        """Read the exact canonical Run and final Message, never transcript text."""

        last_exc = None
        path = (
            f"/api/v1/sessions/{quote(str(session_id), safe='')}"
            f"/runs/{quote(str(run_id), safe='')}"
        )
        for host, port in self._local_workbench_routes():
            if not self._probe_local_workbench(host, port):
                continue
            url = local_http_url(port, path, host=host)
            try:
                result = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda u=url: self._get_json(u, timeout=10),
                )
                run = result.get("run") if isinstance(result, dict) else None
                if not result.get("ok") or not isinstance(run, dict):
                    raise RuntimeError("canonical Run lookup returned no Run")
                if str(run.get("session_id") or "") != str(session_id):
                    raise RuntimeError("canonical Run Session mismatch")
                if str(run.get("run_id") or "") != str(run_id):
                    raise RuntimeError("canonical Run identity mismatch")
                final_message = result.get("final_message")
                text = ""
                attachments: list[dict[str, Any]] = []
                if isinstance(final_message, dict):
                    if str(final_message.get("message_id") or "") != str(
                        run.get("final_message_id") or ""
                    ):
                        raise RuntimeError("canonical final Message mismatch")
                    text = str(final_message.get("text") or "")
                    for raw_part in final_message.get("content") or ():
                        if not isinstance(raw_part, dict):
                            continue
                        part_type = str(
                            raw_part.get("type") or ""
                        ).strip().casefold()
                        if part_type in {"attachment", "media"}:
                            attachment_id = str(
                                raw_part.get("attachment_id") or ""
                            ).strip()
                            if not attachment_id:
                                continue
                            attachments.append(
                                {
                                    "kind": "attachment",
                                    "attachment_id": attachment_id,
                                    "filename": str(
                                        raw_part.get("filename") or "attachment"
                                    ),
                                    "mime_type": str(
                                        raw_part.get("mime_type")
                                        or "application/octet-stream"
                                    ),
                                    "size_bytes": int(
                                        raw_part.get("size_bytes") or 0
                                    ),
                                    "sha256": str(
                                        raw_part.get("sha256") or ""
                                    ).strip().casefold(),
                                    "caption": str(
                                        raw_part.get("caption") or ""
                                    ),
                                }
                            )
                        elif part_type == "audio":
                            asset_id = str(
                                raw_part.get("asset_id") or ""
                            ).strip()
                            if not asset_id:
                                continue
                            audio_format = str(
                                raw_part.get("format") or "ogg"
                            ).strip().casefold()
                            attachments.append(
                                {
                                    "kind": "audio_asset",
                                    "asset_id": asset_id,
                                    "filename": str(
                                        raw_part.get("filename")
                                        or f"assistant-audio.{audio_format}"
                                    ),
                                    "mime_type": str(
                                        raw_part.get("mime_type")
                                        or f"audio/{audio_format}"
                                    ),
                                    "size_bytes": int(
                                        raw_part.get("size_bytes") or 0
                                    ),
                                    "sha256": str(
                                        raw_part.get("sha256") or ""
                                    ).strip().casefold(),
                                    "caption": str(
                                        raw_part.get("caption") or ""
                                    ),
                                }
                            )
                return {
                    "state": str(run.get("state") or "queued"),
                    "request_id": str(run.get("request_id") or ""),
                    "session_id": str(session_id),
                    "run_id": str(run_id),
                    "text": text,
                    "attachments": attachments,
                    "error_code": str(run.get("error_code") or ""),
                }
            except Exception as exc:
                last_exc = exc
        raise last_exc or RuntimeError("canonical Run poll failed")

    async def _process_superloop_receipts(self) -> None:
        root = getattr(self, "_hashi_root", None)
        if root is None or not (root / "superloops" / "loops").is_dir():
            return
        from orchestrator.superloop_receipts import SuperloopReceiptService
        from orchestrator.superloop_store import SuperloopStore

        receipts = [dict(item) for item in self._inflight.values() if item.get("state") == "reply_delivered_locally"]
        def enqueue(payload: dict) -> str | None:
            for host, port in self._local_workbench_routes():
                if not self._probe_local_workbench(host, port):
                    continue
                try:
                    result = self._post_json(local_http_url(port, "/api/chat", host=host), payload, timeout=10)
                    if result.get("ok") and result.get("request_id"):
                        return str(result["request_id"])
                except Exception:
                    continue
            return None

        def activity(agent: str, request_id: str) -> dict | None:
            path = f"/api/agents/{quote(agent, safe='')}/requests/{quote(request_id, safe='')}/activity?limit=1"
            for host, port in self._local_workbench_routes():
                if not self._probe_local_workbench(host, port):
                    continue
                try:
                    result = self._get_json(local_http_url(port, path, host=host), timeout=10)
                    if result.get("ok"):
                        return result
                except Exception:
                    continue
            return None

        def resolve_session(agent: str, request_id: str) -> str | None:
            result = activity(agent, request_id) or {}
            return str(result["session_id"]) if result.get("session_id") else None

        try:
            service = getattr(self, "_superloop_receipt_service", None)
            if service is None:
                service = self._superloop_receipt_service = SuperloopReceiptService(
                    SuperloopStore(root / "superloops"), local_instance=str(self._instance_info.get("instance_id") or ""),
                )
            await asyncio.get_running_loop().run_in_executor(None, service.process, receipts, enqueue, resolve_session, activity)
        except Exception:
            logger.exception("Superloop receipt review deferred; durable receipt retained")

    async def _process_inflight_once(self) -> None:
        await self._process_superloop_receipts()
        if not self._inflight:
            return
        dirty = False
        now = time.time()
        for message_id, item in list(self._inflight.items()):
            state = str(item.get("state") or "")
            if state in TERMINAL_INFLIGHT_STATES:
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
        changed = False
        session_id = str(item.get("session_id") or "").strip()
        run_id = str(item.get("run_id") or "").strip()
        if not session_id or not run_id:
            identity = await self._resolve_request_identity(
                agent_name, str(item.get("request_id") or "")
            )
            if identity is None:
                return False
            session_id = identity["session_id"]
            run_id = identity["run_id"]
            item.update(identity)
            changed = True
        result = await self._poll_session_result(agent_name, session_id, run_id)
        state = str(result.get("state") or "queued")
        if state == "completed":
            reply_text = str(result.get("text") or "").strip()
            reply_attachments = [
                dict(item)
                for item in result.get("attachments") or ()
                if isinstance(item, dict)
            ]
            if not reply_text and not reply_attachments:
                item["state"] = "failed"
                item["error_code"] = "canonical_final_message_missing"
                item["updated_at"] = int(now)
                return True
            item["reply_attachments"] = reply_attachments
            delivery = await self._dispatch_agent_reply(item, reply_text)
            item["reply_text"] = reply_text
            item["state"] = (
                "reply_sent"
                if delivery["sent"]
                else "reply_failed"
                if delivery["known_failure"]
                else "reply_unknown"
            )
            item.pop("reply_failure_known", None)
            item["updated_at"] = int(now)
            return True
        if state in {"failed", "stopped", "superseded", "interrupted"}:
            item["state"] = "failed"
            item["error_code"] = str(
                result.get("error_code") or f"remote_run_{state}"
            )
            item["updated_at"] = int(now)
            return True
        if item.get("state") != state:
            item["state"] = state
            item["updated_at"] = int(now)
            changed = True
        return changed

    def _frontend_session_store(self):
        """Open the PAO Session database used by the local Function runtime."""

        from orchestrator.session_store import SessionStore

        cached = getattr(self, "_fc_session_store", None)
        if isinstance(cached, SessionStore):
            return cached
        root = Path(self._hashi_root)
        cached = SessionStore(
            root / "state" / "sessions.sqlite3",
            instance_id=str(
                self._instance_info.get("instance_id") or "HASHI"
            ),
            attachment_root=root / "media" / "session_attachments",
        )
        self._fc_session_store = cached
        return cached

    async def _dispatch_agent_reply(
        self,
        item: dict[str, Any],
        reply_text: str,
    ) -> dict[str, Any]:
        """Claim the canonical FC Event before the Remote transport effect."""

        async def legacy_compatibility() -> dict[str, Any]:
            sent = bool(await self._send_agent_reply(item, reply_text))
            known = bool(item.get("reply_failure_known"))
            return {
                "sent": sent,
                "known_failure": known,
                "state": "delivered" if sent else "failed" if known else "unknown",
            }

        if not getattr(self, "_hashi_root", None):
            return await legacy_compatibility()
        session_id = str(item.get("session_id") or "").strip()
        request_id = str(item.get("request_id") or "").strip()
        from_instance = str(item.get("from_instance") or "").strip().upper()
        conversation_id = str(item.get("conversation_id") or "").strip()
        if not all((session_id, request_id, from_instance, conversation_id)):
            return await legacy_compatibility()

        from orchestrator.session_store import SessionNotFound

        store = self._frontend_session_store()
        try:
            session = store.get_session(session_id)
        except SessionNotFound:
            return await legacy_compatibility()
        owner_id = str(session["owner_id"])
        channel_key = f"{from_instance}:{conversation_id}"
        claim_result = store.claim_run_delivery_outbox(
            request_id=request_id,
            owner_id=owner_id,
            surface="remote",
            channel_key=channel_key,
            worker_id=f"fc-remote-{request_id}",
        )
        if claim_result is None:
            return await legacy_compatibility()
        claim_state = str(claim_result.get("state") or "").casefold()
        if claim_state != "claimed" or "claim" not in claim_result:
            if claim_state == "completed":
                event_id = str(claim_result.get("event_id") or "")
                receipts = (
                    store.frontend_delivery_receipts(
                        session_id=session_id,
                        owner_id=owner_id,
                        event_id=event_id,
                    )
                    if event_id
                    else []
                )
                sent = any(
                    row.get("status") in {"accepted", "delivered", "duplicate"}
                    for row in receipts
                )
                return {
                    "sent": sent,
                    "known_failure": not sent,
                    "state": "already_completed",
                }
            return {
                "sent": False,
                "known_failure": claim_state
                in {"failed", "suppressed", "missing_outbox"},
                "state": claim_state or "unknown",
            }

        from orchestrator.frontend_dispatch import (
            FrontendDispatcher,
            OutcomeConnectorAdapter,
        )

        transport_outcome: dict[str, Any] = {}

        async def send_standard_event(event, *, endpoint_id):
            del endpoint_id
            text_blocks = [
                str(block.get("text") or "")
                for block in event.get("content_blocks") or ()
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            semantic_text = "\n".join(text_blocks).strip()
            media_ids = [
                str(block.get("attachment_id") or "")
                for block in event.get("content_blocks") or ()
                if isinstance(block, dict) and block.get("type") == "media_ref"
            ]
            expected_media_ids = [
                str(
                    attachment.get("attachment_id")
                    or attachment.get("asset_id")
                    or ""
                )
                for attachment in item.get("reply_attachments") or ()
                if isinstance(attachment, dict)
            ]
            if media_ids != expected_media_ids:
                raise ValueError("Remote attachment projection mismatch")
            if not semantic_text and not media_ids:
                raise ValueError("standard frontend Event has no reply content")
            sent = bool(await self._send_agent_reply(item, semantic_text))
            known_failure = bool(item.get("reply_failure_known"))
            outcome = {
                "attempted": True,
                "delivered": sent,
                "state": (
                    "delivered" if sent else "failed" if known_failure else "unknown"
                ),
                "message_id": (
                    f"{item.get('message_id')}:reply" if sent else None
                ),
            }
            transport_outcome.update(outcome)
            return outcome

        dispatcher = FrontendDispatcher(
            store,
            worker_id=f"fc-remote-{request_id}",
            adapters={
                "remote": OutcomeConnectorAdapter("remote", send_standard_event)
            },
        )
        result = await dispatcher.dispatch_claimed_task(
            claim_result["claim"],
            session_id=session_id,
            owner_id=owner_id,
        )
        if transport_outcome.get("delivered"):
            return {"sent": True, "known_failure": False, "state": "delivered"}
        status = str(result.get("status") or "unknown")
        known_failure = bool(item.get("reply_failure_known")) or status in {
            "failed",
            "suppressed",
        }
        return {
            "sent": False,
            "known_failure": known_failure,
            "state": "failed" if known_failure else "unknown",
        }

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
        reply_attachments = [
            dict(attachment)
            for attachment in item.get("reply_attachments") or ()
            if isinstance(attachment, dict)
        ]
        if reply_attachments:
            peer = (
                self._peer_registry.get_peer(instance_id)
                if self._peer_registry
                else None
            )
            capabilities = {
                str(value)
                for value in getattr(peer, "capabilities", ()) or ()
            }
            if "message_attachments_v1" not in capabilities:
                item["reply_failure_known"] = True
                item["reply_error_code"] = "attachment_capability_missing"
                logger.warning(
                    "Cannot send attachment reply %s: peer %s lacks message_attachments_v1",
                    payload["message_id"],
                    instance_id,
                )
                return False
            return await self._send_agent_reply_attachments(
                item=item,
                route=route,
                payload=payload,
                attachments=reply_attachments,
            )
        urls = self._candidate_urls(route["host"], route["port"], "/protocol/message")
        if not urls:
            return False
        url = urls[0]
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._post_json(url, payload, timeout=10),
            )
            if self._response_confirms_terminal_duplicate(result, payload):
                logger.info(
                    "Reply %s was already terminal at %s; accepting duplicate rejection as delivery success",
                    payload["message_id"],
                    route.get("instance_id"),
                )
                return True
            if self._response_is_error(result):
                item["reply_failure_known"] = True
                item["reply_error_code"] = "reply_rejected"
                return False
            return bool(result.get("ok", True))
        except Exception as exc:
            logger.warning(
                "Reply send via %s has unknown outcome; no alternate replay: %s",
                url,
                exc,
            )
            return False

    def _get_bytes(self, url: str, timeout: int = 10) -> bytes:
        request = urllib_request.Request(url, method="GET")
        context = (
            ssl._create_unverified_context()
            if str(url).startswith("https://")
            else None
        )
        with urllib_request.urlopen(
            request, timeout=timeout, context=context
        ) as response:
            return bytes(response.read())

    async def _read_local_reply_attachment(
        self,
        *,
        session_id: str,
        attachment: dict[str, Any],
    ) -> bytes:
        kind = str(attachment.get("kind") or "").strip().casefold()
        identifier = str(
            attachment.get("attachment_id")
            if kind == "attachment"
            else attachment.get("asset_id")
            or ""
        ).strip()
        if not identifier:
            raise ValueError("reply attachment identity is missing")
        if kind == "attachment":
            path = (
                f"/api/v1/sessions/{quote(session_id, safe='')}"
                f"/attachments/{quote(identifier, safe='')}/content"
            )
        elif kind == "audio_asset":
            path = (
                f"/api/v1/sessions/{quote(session_id, safe='')}"
                f"/audio-assets/{quote(identifier, safe='')}"
            )
        else:
            raise ValueError("reply attachment kind is unsupported")
        last_error: Exception | None = None
        for host, port in self._local_workbench_routes():
            if not self._probe_local_workbench(host, port):
                continue
            url = local_http_url(port, path, host=host)
            try:
                payload = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda target=url: self._get_bytes(target, timeout=10),
                )
                declared_size = int(attachment.get("size_bytes") or 0)
                if declared_size and len(payload) != declared_size:
                    raise ValueError("reply attachment size changed")
                declared_digest = str(
                    attachment.get("sha256") or ""
                ).strip().casefold()
                if (
                    declared_digest
                    and hashlib.sha256(payload).hexdigest() != declared_digest
                ):
                    raise ValueError("reply attachment content changed")
                return payload
            except Exception as exc:
                last_error = exc
        raise last_error or RuntimeError("reply attachment bytes are unavailable")

    async def _send_agent_reply_attachments(
        self,
        *,
        item: dict[str, Any],
        route: dict[str, Any],
        payload: dict[str, Any],
        attachments: list[dict[str, Any]],
    ) -> bool:
        """Upload all managed reply bytes, then commit one correlated reply."""

        prepared: list[tuple[dict[str, Any], bytes]] = []
        try:
            for attachment in attachments:
                prepared.append(
                    (
                        attachment,
                        await self._read_local_reply_attachment(
                            session_id=str(item.get("session_id") or ""),
                            attachment=attachment,
                        ),
                    )
                )
        except Exception as exc:
            item["reply_failure_known"] = True
            item["reply_error_code"] = "reply_attachment_unavailable"
            logger.warning("Reply attachment preparation failed: %s", exc)
            return False

        def _url(path: str) -> str:
            candidates = self._candidate_urls(
                route["host"], route["port"], path
            )
            if not candidates:
                raise RuntimeError("reply attachment route is unavailable")
            return candidates[0]

        staged: list[dict[str, Any]] = []
        for index, (attachment, content) in enumerate(prepared, start=1):
            attachment_id = f"att-{index}"
            upload_payload = {
                "message_id": payload["message_id"],
                "from_instance": payload["from_instance"],
                "attachment_id": attachment_id,
                "filename": Path(
                    str(attachment.get("filename") or f"attachment-{index}")
                ).name,
                "mime_type": str(
                    attachment.get("mime_type") or "application/octet-stream"
                ),
                "content_b64": base64.b64encode(content).decode("ascii"),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            try:
                result = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda target=_url("/attachments/upload"), body=upload_payload: self._post_json(
                        target, body, timeout=15
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "Reply attachment upload has unknown outcome; no replay: %s",
                    exc,
                )
                return False
            if self._response_is_error(result) or not result.get("ok"):
                item["reply_failure_known"] = True
                item["reply_error_code"] = "reply_attachment_upload_rejected"
                await self._cancel_reply_attachment_uploads(
                    route=route,
                    message_id=str(payload["message_id"]),
                    from_instance=str(payload["from_instance"]),
                    staged=staged,
                )
                return False
            pending = dict(result.get("attachment") or {})
            staged.append(
                {
                    "attachment_id": attachment_id,
                    "pending_upload_id": str(
                        pending.get("pending_upload_id") or ""
                    ),
                    "filename": upload_payload["filename"],
                    "mime_type": upload_payload["mime_type"],
                    "size_bytes": len(content),
                    "sha256": upload_payload["sha256"],
                    "caption": str(attachment.get("caption") or ""),
                }
            )

        commit_payload = {**payload, "attachments": staged}
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._post_json(
                    _url("/protocol/message-with-attachments"),
                    commit_payload,
                    timeout=15,
                ),
            )
        except Exception as exc:
            logger.warning(
                "Attachment reply commit has unknown outcome; no replay or cancel: %s",
                exc,
            )
            return False
        if self._response_confirms_terminal_duplicate(result, payload):
            return True
        if self._response_is_error(result) or not result.get("ok", True):
            item["reply_failure_known"] = True
            item["reply_error_code"] = "reply_attachment_commit_rejected"
            await self._cancel_reply_attachment_uploads(
                route=route,
                message_id=str(payload["message_id"]),
                from_instance=str(payload["from_instance"]),
                staged=staged,
            )
            return False
        return True

    async def _cancel_reply_attachment_uploads(
        self,
        *,
        route: dict[str, Any],
        message_id: str,
        from_instance: str,
        staged: list[dict[str, Any]],
    ) -> None:
        pending_ids = [
            str(item.get("pending_upload_id") or "")
            for item in staged
            if str(item.get("pending_upload_id") or "")
        ]
        if not pending_ids:
            return
        candidates = self._candidate_urls(
            route["host"], route["port"], "/attachments/upload/cancel"
        )
        if not candidates:
            return
        body = {
            "message_id": message_id,
            "from_instance": from_instance,
            "pending_upload_ids": pending_ids,
            "reason": "reply_commit_rejected",
        }
        try:
            await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._post_json(candidates[0], body, timeout=10),
            )
        except Exception as exc:
            logger.warning("Reply attachment cleanup failed safely: %s", exc)

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

    def _mark_nonterminal_inflight_abandoned_after_restart(self) -> bool:
        """Make restart adoption explicit instead of leaving old inflight state ambiguous."""
        changed = False
        now = int(time.time())
        for message_id, item in list(self._inflight.items()):
            if not isinstance(item, dict):
                continue
            state = str(item.get("state") or "")
            if state in TERMINAL_INFLIGHT_STATES:
                continue
            item["previous_state"] = state or "unknown"
            item["state"] = "abandoned_after_restart"
            item["abandoned_reason"] = "sidecar_restart_without_rebind"
            item["updated_at"] = now
            self._inflight[message_id] = item
            changed = True
        return changed

    def _load_outbound_correlations(self) -> dict[str, dict[str, Any]]:
        data = self._load_json(self._outbound_path)
        messages = data.get("messages", {}) if isinstance(data, dict) else {}
        return messages if isinstance(messages, dict) else {}

    def _save_outbound_correlations(self, messages: dict[str, dict[str, Any]]) -> None:
        self._outbound_path.write_text(
            json.dumps({"messages": messages}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _outbound_correlation_for(self, message_id: str) -> dict[str, Any] | None:
        item = self._load_outbound_correlations().get(str(message_id or "").strip())
        return item if isinstance(item, dict) else None

    def _mark_outbound_reply_delivered(self, message_id: str, *, reply_message_id: str, delivered_at: int) -> None:
        messages = self._load_outbound_correlations()
        item = messages.get(str(message_id or "").strip())
        if not isinstance(item, dict):
            return
        item["state"] = "reply_delivered_locally"
        item["reply_message_id"] = reply_message_id
        item["reply_delivered_at"] = delivered_at
        item["updated_at"] = delivered_at
        messages[str(message_id).strip()] = item
        self._save_outbound_correlations(messages)

    def _get_json(self, url: str, timeout: int = 10) -> dict:
        req = urllib_request.Request(url, method="GET")
        context = ssl._create_unverified_context() if str(url).startswith("https://") else None
        with urllib_request.urlopen(req, timeout=timeout, context=context) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post_json(self, url: str, payload: dict, timeout: int = 10) -> dict:
        body_bytes = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        path = urlsplit(url).path
        if self._shared_token and path in {
            "/attachments/upload",
            "/attachments/upload/cancel",
            "/protocol/announce",
            "/protocol/handshake",
            "/protocol/message",
            "/protocol/message-with-attachments",
        }:
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
                result = json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8")
            try:
                result = json.loads(body) if body else {}
            except Exception:
                raise
            if isinstance(result, dict):
                result["__http_status"] = exc.code
        if (
            self._shared_token
            and path in {"/protocol/announce", "/protocol/handshake"}
            and isinstance(result, dict)
            and str(result.get("status") or "").lower() == "handshake_accept"
        ):
            response_auth = result.get("response_auth")
            authenticated_payload = {
                key: value
                for key, value in result.items()
                if key not in {"response_auth", "__http_status"}
            }
            if not verify_response_auth(
                shared_token=self._shared_token,
                request_nonce=str(headers.get(HEADER_NONCE) or ""),
                payload=authenticated_payload,
                response_auth=response_auth,
            ):
                raise RuntimeError("protocol handshake response authentication failed")
            result.pop("response_auth", None)
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

    @staticmethod
    def _response_confirms_terminal_duplicate(result: dict, payload: dict) -> bool:
        """Recognize an authenticated peer's idempotent terminal reply receipt."""
        if not isinstance(result, dict) or not isinstance(payload, dict):
            return False
        try:
            status = int(result.get("__http_status") or 200)
        except (TypeError, ValueError):
            return False
        body = result.get("body")
        if status != 409 or result.get("message_type") != "error" or not isinstance(body, dict):
            return False
        return (
            body.get("code") == "duplicate_message"
            and body.get("retryable") is False
            and str(body.get("failed_message_id") or "")
            == str(payload.get("message_id") or "")
            and str(body.get("conversation_id") or "")
            == str(payload.get("conversation_id") or "")
        )

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

    def _probe_instance_route(self, host: str, port: int, expected_instance_id: str, timeout: int = 2) -> bool:
        expected = str(expected_instance_id or "").strip().upper()
        for url in self._candidate_urls(host, port, "/health"):
            try:
                health = self._get_json(url, timeout=timeout)
            except Exception:
                continue
            if not health or not health.get("ok", True):
                continue
            instance = health.get("instance") or {}
            actual = str(instance.get("instance_id") or "").strip().upper()
            if actual and actual == expected:
                return True
            logger.debug("Bootstrap probe ignored %s for expected %s via %s", actual or "unknown", expected, url)
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
