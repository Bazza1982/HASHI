"""
Abstract peer discovery interface for Hashi Remote.

This abstraction layer exists so that the transport mechanism can be swapped
without touching any other part of the system:

  Today:   LanDiscovery  → mDNS (zeroconf), works on same LAN
  Future:  TailscaleDiscovery → Tailscale DNS, works across internet
           RelayDiscovery     → Central relay server, no direct connection needed

To add a new backend, subclass PeerDiscovery and implement advertise/discover/stop.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


INVALID_INSTANCE_IDS = {"UNKNOWN"}


def normalize_instance_id(value: object) -> str:
    """Return the canonical peer identity form used across Remote discovery."""
    return str(value or "").strip().upper()


def is_valid_instance_id(value: object) -> bool:
    instance_id = normalize_instance_id(value)
    return bool(instance_id) and instance_id not in INVALID_INSTANCE_IDS


@dataclass
class PeerInfo:
    """Represents a discovered HASHI peer instance."""

    instance_id: str        # e.g. "HASHI1", "HASHI9"
    display_name: str       # Human-readable name
    host: str               # IP or hostname to connect to
    port: int               # Hashi Remote peer port (default 8766)
    workbench_port: int     # Workbench API port (default 18800)
    platform: str           # "wsl", "windows", "linux", "macos"
    version: str = "unknown"
    hashi_version: str = "unknown"
    display_handle: str = ""
    protocol_version: str = "1.0"
    capabilities: list[str] = field(default_factory=list)
    properties: dict = field(default_factory=dict)

    @property
    def hchat_url(self) -> str:
        """URL to deliver an hchat message to this peer."""
        return f"http://{self.host}:{self.port}/hchat"

    @property
    def health_url(self) -> str:
        return f"http://{self.host}:{self.port}/health"

    @property
    def workbench_url(self) -> str:
        return f"http://{self.host}:{self.workbench_port}/api/chat"

    def to_dict(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "display_name": self.display_name,
            "display_handle": self.display_handle or f"@{self.instance_id.lower()}",
            "host": self.host,
            "port": self.port,
            "workbench_port": self.workbench_port,
            "platform": self.platform,
            "version": self.version,
            "hashi_version": self.hashi_version,
            "protocol_version": self.protocol_version,
            "capabilities": list(self.capabilities or []),
            "properties": dict(self.properties or {}),
        }


class PeerDiscovery(ABC):
    """Abstract base class for peer discovery backends."""

    @abstractmethod
    async def advertise(self, info: PeerInfo) -> bool:
        """Start advertising this instance to other peers. Returns True on success."""
        ...

    @abstractmethod
    async def discover(self) -> list[PeerInfo]:
        """Return a list of currently known peers (excluding self)."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop advertising and cleanup resources."""
        ...

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """Human-readable name of this backend, e.g. 'LAN/mDNS'."""
        ...
