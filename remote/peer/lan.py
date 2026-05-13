"""
LAN peer discovery using mDNS / Zeroconf.

Adapted from Lily Remote (agent/discovery/mdns.py) — the original by Barry & XiaoLei 🌸.
Service type changed from _lilyremote._tcp.local. to _hashi._tcp.local.

Each Hashi Remote instance advertises itself on the LAN, and discovers others.
No configuration needed — plug and play on the same network.
"""

import asyncio
import json
import logging
import socket
from typing import Optional, Callable

from zeroconf import IPVersion, InterfaceChoice, ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

from .base import PeerDiscovery, PeerInfo, is_valid_instance_id, normalize_instance_id

logger = logging.getLogger(__name__)

HASHI_SERVICE_TYPE = "_hashi._tcp.local."
HASHI_REMOTE_VERSION = "1.0.0"
_SCOPE_TO_CODE = {
    "same_host": "s",
    "lan": "l",
    "overlay": "o",
    "host_virtual": "v",
    "routable": "r",
    "peer": "p",
}
_CODE_TO_SCOPE = {value: key for key, value in _SCOPE_TO_CODE.items()}


def _normalize_host_identity(value: str) -> str:
    value = str(value or "").strip().lower()
    return "".join(ch for ch in value if ch.isalnum())


def _encode_candidate_records(items: list[dict]) -> str:
    packed = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        host = str(item.get("host") or "").strip()
        scope = str(item.get("scope") or "").strip()
        source = str(item.get("source") or "").strip()
        if not host:
            continue
        packed.append([host, _SCOPE_TO_CODE.get(scope, scope[:1] or "?"), source[:2] or "?"])
    return json.dumps(packed, separators=(",", ":"))


def _decode_candidate_records(raw: str) -> list[dict]:
    try:
        data = json.loads(raw or "[]")
    except Exception:
        return []
    items: list[dict] = []
    for item in data:
        if isinstance(item, dict):
            host = str(item.get("host") or "").strip()
            scope = str(item.get("scope") or "").strip()
            source = str(item.get("source") or "").strip()
        elif isinstance(item, list) and item:
            host = str(item[0] or "").strip() if len(item) >= 1 else ""
            scope = _CODE_TO_SCOPE.get(str(item[1] or "").strip(), str(item[1] or "").strip()) if len(item) >= 2 else ""
            source = str(item[2] or "").strip() if len(item) >= 3 else ""
        else:
            continue
        if host:
            items.append({"host": host, "scope": scope or "unknown", "source": source or "peer"})
    return items


def _get_local_ip() -> str:
    candidates, _observed = _collect_ipv4_candidates()
    for item in candidates:
        if str(item.get("scope") or "") in {"lan", "overlay", "routable"}:
            return str(item.get("host") or "127.0.0.1")
    return "127.0.0.1"


def _classify_address_scope(ip_str: str, adapter_name: str = "") -> str:
    import ipaddress

    try:
        addr = ipaddress.IPv4Address(ip_str)
    except Exception:
        return "unknown"
    if addr.is_unspecified or addr.is_link_local or addr.is_multicast or addr.is_reserved:
        return "unknown"
    if str(ip_str).startswith("255."):
        return "unknown"
    if addr.is_loopback:
        return "same_host"
    adapter_hint = str(adapter_name or "").strip().lower()
    if adapter_hint.startswith("lo"):
        return "host_virtual"
    if adapter_hint and any(token in adapter_hint for token in ("wsl", "hyper-v", "vethernet", "virtualbox", "vmware", "host-only")):
        return "host_virtual"
    if addr in ipaddress.IPv4Network("100.64.0.0/10"):
        return "overlay"
    if addr.is_private:
        return "lan"
    return "routable"


def _collect_ipv4_candidates() -> tuple[list[dict], list[dict]]:
    import ipaddress
    import subprocess

    seen: set[str] = set()
    observed_seen: set[tuple[str, str]] = set()
    routing_items: list[tuple[int, dict]] = []
    observed_items: list[tuple[int, dict]] = []

    def _priority(host: str, scope: str) -> int:
        try:
            addr = ipaddress.IPv4Address(host)
        except Exception:
            return 90
        if addr.is_loopback:
            return 0
        if scope == "lan" and addr in ipaddress.IPv4Network("192.168.0.0/16"):
            return 10
        if scope == "lan" and addr in ipaddress.IPv4Network("10.0.0.0/8"):
            return 20
        if scope == "host_virtual":
            return 30
        if scope == "lan" and addr in ipaddress.IPv4Network("172.16.0.0/12"):
            return 35
        if scope == "overlay":
            return 40
        return 50

    def _record_observed(host: str, scope: str, source: str) -> None:
        key = (host, scope)
        if key in observed_seen:
            return
        observed_seen.add(key)
        observed_items.append(
            (
                _priority(host, scope),
                {
                    "host": host,
                    "scope": scope,
                    "source": source,
                },
            )
        )

    def _add(host: str, source: str, adapter_name: str = "") -> None:
        host = str(host or "").strip()
        if not host or host in seen:
            return
        scope = _classify_address_scope(host, adapter_name=adapter_name)
        if scope == "unknown":
            return
        _record_observed(host, scope, source)
        if scope == "host_virtual":
            return
        seen.add(host)
        routing_items.append(
            (
                _priority(host, scope),
                {
                    "host": host,
                    "scope": scope,
                    "source": source,
                },
            )
        )

    _add("127.0.0.1", "loopback")

    try:
        import ifaddr

        for adapter in ifaddr.get_adapters():
            adapter_name = str(getattr(adapter, "nice_name", "") or getattr(adapter, "name", "") or "")
            for addr in adapter.ips:
                if isinstance(addr.ip, str):
                    _add(addr.ip, "interface_scan", adapter_name=adapter_name)
    except Exception:
        pass

    try:
        result = subprocess.run(["ip", "-4", "addr", "show"], capture_output=True, text=True, timeout=3)
        current_adapter = ""
        for line in result.stdout.splitlines():
            if line and not line.startswith(" "):
                try:
                    current_adapter = line.split(":", 2)[1].strip()
                except Exception:
                    current_adapter = ""
                continue
            line = line.strip()
            if line.startswith("inet "):
                _add(line.split()[1].split("/")[0], "ip_addr_show", adapter_name=current_adapter)
    except Exception:
        pass

    routing_items.sort(key=lambda item: (item[0], item[1]["host"]))
    observed_items.sort(key=lambda item: (item[0], item[1]["host"]))
    return (
        [item for _, item in routing_items],
        [item for _, item in observed_items],
    )


def build_local_network_profile(info: PeerInfo) -> dict:
    candidates, observed_candidates = _collect_ipv4_candidates()
    environment_kind = str(info.platform or "unknown").lower()
    return {
        "host_identity": _normalize_host_identity(socket.gethostname()),
        "environment_kind": environment_kind,
        "address_candidates": candidates,
        "observed_candidates": observed_candidates,
    }


def _service_info_to_peer(info: ServiceInfo, self_instance_id: str) -> Optional[PeerInfo]:
    """Convert a zeroconf ServiceInfo into a PeerInfo. Returns None if it's ourselves."""
    try:
        props = {
            k.decode() if isinstance(k, bytes) else k:
            v.decode() if isinstance(v, bytes) else v
            for k, v in (info.properties or {}).items()
        }
        address_candidates = _decode_candidate_records(props.get("address_candidates_json", "[]") or "[]")
        observed_candidates = _decode_candidate_records(props.get("observed_candidates_json", "[]") or "[]")

        instance_id = normalize_instance_id(props.get("instance_id"))
        if not is_valid_instance_id(instance_id):
            logger.debug(
                "LanDiscovery: ignoring service without valid instance_id: server=%s port=%s",
                getattr(info, "server", ""),
                getattr(info, "port", ""),
            )
            return None
        if instance_id == self_instance_id.upper():
            return None  # Don't include ourselves

        addresses = info.parsed_addresses()
        host = addresses[0] if addresses else info.server.rstrip(".")

        return PeerInfo(
            instance_id=instance_id,
            display_name=props.get("display_name", instance_id),
            host=host,
            port=info.port,
            workbench_port=int(props.get("workbench_port", 18800)),
            platform=props.get("platform", "unknown"),
            version=props.get("version", "unknown"),
            hashi_version=props.get("hashi_version", "unknown"),
            display_handle=props.get("display_handle", f"@{instance_id.lower()}"),
            protocol_version=props.get("protocol_version", "1.0"),
            capabilities=[c for c in props.get("capabilities", "").split(",") if c],
            properties={
                "discovery": "lan",
                "host_identity": _normalize_host_identity(props.get("host_identity", "")),
                "environment_kind": props.get("environment_kind", "").strip().lower(),
                "agent_snapshot_version": props.get("agent_snapshot_version", ""),
                "directory_state": props.get("directory_state", ""),
                "address_candidates": address_candidates,
                "observed_candidates": observed_candidates,
            },
        )
    except Exception as e:
        logger.warning("Failed to parse ServiceInfo: %s", e)
        return None


class _HashiListener(ServiceListener):
    """Zeroconf listener that tracks discovered HASHI peers."""

    def __init__(self, self_instance_id: str, on_change: Optional[Callable] = None):
        self._self_id = self_instance_id
        self._peers: dict[str, PeerInfo] = {}
        self._on_change = on_change

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if info:
            peer = _service_info_to_peer(info, self._self_id)
            if peer:
                self._peers[peer.instance_id] = peer
                logger.info("Discovered peer: %s @ %s:%d", peer.instance_id, peer.host, peer.port)
                if self._on_change:
                    self._on_change(list(self._peers.values()))

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.add_service(zc, type_, name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        # Extract instance_id from service name if possible
        for iid, peer in list(self._peers.items()):
            if iid.lower() in name.lower():
                del self._peers[iid]
                logger.info("Peer left: %s", iid)
                if self._on_change:
                    self._on_change(list(self._peers.values()))
                break

    def get_peers(self) -> list[PeerInfo]:
        return list(self._peers.values())


class LanDiscovery(PeerDiscovery):
    """
    mDNS-based LAN discovery for Hashi Remote.

    Uses zeroconf to both advertise this instance and discover others on the
    same local network. No configuration needed.

    Tailscale note: When Tailscale is used, all machines appear as if on the
    same LAN — this discovery backend works transparently with Tailscale too.
    """

    def __init__(self, self_instance_id: str, on_peers_changed: Optional[Callable] = None):
        self._self_id = self_instance_id
        self._on_peers_changed = on_peers_changed
        self._zeroconf: Optional[Zeroconf] = None
        self._service_info: Optional[ServiceInfo] = None
        self._listener: Optional[_HashiListener] = None
        self._browser: Optional[ServiceBrowser] = None
        self._advertising = False

    @property
    def backend_name(self) -> str:
        return "LAN/mDNS"

    async def advertise(self, info: PeerInfo) -> bool:
        """Register this instance on the LAN via mDNS."""
        if self._advertising:
            logger.warning("LanDiscovery: already advertising")
            return True

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._start_advertising, info)
            return self._advertising
        except Exception as e:
            logger.error("LanDiscovery: advertise failed: %s", e)
            return False

    async def update_advertisement(self, info: PeerInfo) -> bool:
        """Refresh mDNS properties without restarting Remote."""
        if not self._advertising or not self._zeroconf or not self._service_info:
            return await self.advertise(info)
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._update_service_info, info)
            return True
        except Exception as e:
            logger.warning("LanDiscovery: advertisement update failed: %s", e)
            return False

    def _service_info_for_peer(self, info: PeerInfo) -> ServiceInfo:
        hostname = socket.gethostname()
        local_ip = _get_local_ip()
        network_profile = build_local_network_profile(info)
        extra = dict(info.properties or {})

        props = {
            "instance_id": info.instance_id,
            "display_name": info.display_name,
            "display_handle": info.display_handle or f"@{info.instance_id.lower()}",
            "platform": info.platform,
            "workbench_port": str(info.workbench_port),
            "version": HASHI_REMOTE_VERSION,
            "hashi_version": info.hashi_version,
            "protocol_version": info.protocol_version or "1.0",
            "capabilities": ",".join(info.capabilities or []),
            "host_identity": str(network_profile.get("host_identity") or ""),
            "environment_kind": str(network_profile.get("environment_kind") or ""),
            "agent_snapshot_version": str(extra.get("agent_snapshot_version") or ""),
            "directory_state": str(extra.get("directory_state") or ""),
            "address_candidates_json": _encode_candidate_records(network_profile.get("address_candidates") or []),
            "observed_candidates_json": _encode_candidate_records(network_profile.get("observed_candidates") or []),
        }
        props_bytes = {k: v.encode() for k, v in props.items()}
        service_name = f"{info.instance_id} - Hashi Remote.{HASHI_SERVICE_TYPE}"
        return ServiceInfo(
            type_=HASHI_SERVICE_TYPE,
            name=service_name,
            port=info.port,
            properties=props_bytes,
            server=f"{hostname}.local.",
            addresses=[socket.inet_aton(local_ip)],
        )

    def _start_advertising(self, info: PeerInfo) -> None:
        self._service_info = self._service_info_for_peer(info)

        # Bind to all interfaces so mDNS multicast reaches LAN even when
        # Tailscale is the default route (which would otherwise shadow the LAN NIC).
        self._zeroconf = Zeroconf(ip_version=IPVersion.V4Only, interfaces=InterfaceChoice.All)

        # Start browser to discover other peers
        self._listener = _HashiListener(self._self_id, self._on_peers_changed)
        self._browser = ServiceBrowser(self._zeroconf, HASHI_SERVICE_TYPE, self._listener)

        # Register ourselves
        self._zeroconf.register_service(self._service_info)
        self._advertising = True
        logger.info(
            "LanDiscovery: advertising as %s @ %s:%d",
            info.instance_id, _get_local_ip(), info.port,
        )

    def _update_service_info(self, info: PeerInfo) -> None:
        if not self._zeroconf:
            return
        self._service_info = self._service_info_for_peer(info)
        self._zeroconf.update_service(self._service_info)
        logger.info("LanDiscovery: refreshed advertisement for %s", info.instance_id)

    async def discover(self) -> list[PeerInfo]:
        """Return currently known peers."""
        if self._listener:
            return self._listener.get_peers()
        return []

    async def stop(self) -> None:
        if not self._advertising:
            return
        try:
            if self._zeroconf and self._service_info:
                self._zeroconf.unregister_service(self._service_info)
            if self._zeroconf:
                self._zeroconf.close()
        except Exception as e:
            logger.warning("LanDiscovery: error during stop: %s", e)
        finally:
            self._advertising = False
            self._zeroconf = None
            self._service_info = None
            self._browser = None
            self._listener = None
            logger.info("LanDiscovery: stopped")


def create_lan_discovery(
    self_instance_id: str,
    on_peers_changed: Optional[Callable] = None,
) -> LanDiscovery:
    return LanDiscovery(self_instance_id, on_peers_changed)
