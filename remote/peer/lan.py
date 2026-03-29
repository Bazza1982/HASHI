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

from zeroconf import IPVersion, ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

from .base import PeerDiscovery, PeerInfo

logger = logging.getLogger(__name__)

HASHI_SERVICE_TYPE = "_hashi._tcp.local."
HASHI_REMOTE_VERSION = "1.0.0"


def _get_local_ip() -> str:
    """Get the primary non-loopback local IP address."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _service_info_to_peer(info: ServiceInfo, self_instance_id: str) -> Optional[PeerInfo]:
    """Convert a zeroconf ServiceInfo into a PeerInfo. Returns None if it's ourselves."""
    try:
        props = {
            k.decode() if isinstance(k, bytes) else k:
            v.decode() if isinstance(v, bytes) else v
            for k, v in (info.properties or {}).items()
        }

        instance_id = props.get("instance_id", "unknown").upper()
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

    def _start_advertising(self, info: PeerInfo) -> None:
        hostname = socket.gethostname()
        local_ip = _get_local_ip()

        props = {
            "instance_id": info.instance_id,
            "display_name": info.display_name,
            "platform": info.platform,
            "workbench_port": str(info.workbench_port),
            "version": HASHI_REMOTE_VERSION,
            "hashi_version": info.hashi_version,
        }
        props_bytes = {k: v.encode() for k, v in props.items()}

        service_name = f"{info.instance_id} - Hashi Remote.{HASHI_SERVICE_TYPE}"

        self._service_info = ServiceInfo(
            type_=HASHI_SERVICE_TYPE,
            name=service_name,
            port=info.port,
            properties=props_bytes,
            server=f"{hostname}.local.",
            addresses=[socket.inet_aton(local_ip)],
        )

        self._zeroconf = Zeroconf(ip_version=IPVersion.V4Only)

        # Start browser to discover other peers
        self._listener = _HashiListener(self._self_id, self._on_peers_changed)
        self._browser = ServiceBrowser(self._zeroconf, HASHI_SERVICE_TYPE, self._listener)

        # Register ourselves
        self._zeroconf.register_service(self._service_info)
        self._advertising = True
        logger.info(
            "LanDiscovery: advertising as %s @ %s:%d",
            info.instance_id, local_ip, info.port,
        )

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
