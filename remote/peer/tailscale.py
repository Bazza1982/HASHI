"""
Tailscale peer discovery — STUB (not yet implemented).

═══════════════════════════════════════════════════════════════════════
  READY FOR TAILSCALE EXTENSION
  ─────────────────────────────
  When the time comes, implement TailscaleDiscovery below.

  Why Tailscale works perfectly for Hashi Remote:
    - All Tailscale machines appear on a virtual LAN (100.x.x.x IPs)
    - mDNS (LanDiscovery) already works across Tailscale's MagicDNS
    - But TailscaleDiscovery can be smarter: query the Tailscale API
      to enumerate all online nodes with "hashi" tags, no mDNS needed
    - Free tier: 3 users / 100 devices — more than enough for home use

  Implementation plan:
    1. pip install tailscale (or use `tailscale status --json`)
    2. Parse output to find nodes with tag:hashi
    3. Build PeerInfo from each node's IP + port
    4. Register/deregister as node connects/disconnects
    5. Swap in TailscaleDiscovery in main.py when config.discovery == "tailscale"

  API docs: https://tailscale.com/api
═══════════════════════════════════════════════════════════════════════
"""

import logging
from .base import PeerDiscovery, PeerInfo

logger = logging.getLogger(__name__)


class TailscaleDiscovery(PeerDiscovery):
    """
    Tailscale-based discovery for internet-wide Hashi Remote connectivity.

    NOT YET IMPLEMENTED — this is a stub that raises NotImplementedError.
    When implemented, swap this in via config: discovery: tailscale
    """

    def __init__(self, self_instance_id: str):
        self._self_id = self_instance_id

    @property
    def backend_name(self) -> str:
        return "Tailscale"

    async def advertise(self, info: PeerInfo) -> bool:
        raise NotImplementedError(
            "TailscaleDiscovery is not yet implemented. "
            "Use LanDiscovery for now, or contribute the implementation!"
        )

    async def discover(self) -> list[PeerInfo]:
        raise NotImplementedError("TailscaleDiscovery not yet implemented.")

    async def stop(self) -> None:
        pass
