"""Peer discovery exports with lazy backend imports."""

from .base import PeerDiscovery, PeerInfo
from .registry import PeerRegistry

__all__ = [
    "LanDiscovery",
    "PeerDiscovery",
    "PeerInfo",
    "PeerRegistry",
    "TailscaleDiscovery",
    "create_lan_discovery",
]


def __getattr__(name: str):
    if name in {"LanDiscovery", "create_lan_discovery"}:
        from .lan import LanDiscovery, create_lan_discovery

        mapping = {
            "LanDiscovery": LanDiscovery,
            "create_lan_discovery": create_lan_discovery,
        }
        return mapping[name]
    if name == "TailscaleDiscovery":
        from .tailscale import TailscaleDiscovery

        return TailscaleDiscovery
    raise AttributeError(name)
