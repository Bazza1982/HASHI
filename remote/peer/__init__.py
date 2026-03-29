"""Peer discovery — abstract interface + LAN (mDNS) implementation."""

from .base import PeerDiscovery, PeerInfo
from .lan import LanDiscovery, create_lan_discovery
from .registry import PeerRegistry

__all__ = ["PeerDiscovery", "PeerInfo", "LanDiscovery", "create_lan_discovery", "PeerRegistry"]
