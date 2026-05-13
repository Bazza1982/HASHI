"""Peer discovery exports with lazy backend imports."""

from .base import PeerDiscovery, PeerInfo, is_valid_instance_id, normalize_instance_id

__all__ = [
    "LanDiscovery",
    "PeerDiscovery",
    "PeerInfo",
    "PeerRegistry",
    "TailscaleDiscovery",
    "create_lan_discovery",
    "is_valid_instance_id",
    "normalize_instance_id",
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
    if name == "PeerRegistry":
        from .registry import PeerRegistry

        return PeerRegistry
    raise AttributeError(name)
