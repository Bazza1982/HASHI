"""Peer discovery — abstract interface + LAN/Tailscale implementations."""

from .base import PeerDiscovery, PeerInfo
from .lan import LanDiscovery, create_lan_discovery
from .registry import PeerRegistry
from .tailscale import TailscaleDiscovery

__all__ = ["PeerDiscovery", "PeerInfo", "LanDiscovery", "TailscaleDiscovery", "create_lan_discovery", "PeerRegistry"]
