"""
Peer Registry — syncs discovered peers back into instances.json.

When LanDiscovery finds a new HASHI instance on the LAN, this registry
writes its real IP and port into instances.json so hchat_send.py can
route messages to it using the actual network address (not 127.0.0.1).

This is the bridge between mDNS discovery and HASHI's existing routing.
"""

import json
import logging
from pathlib import Path
from typing import Optional

from .base import PeerInfo

logger = logging.getLogger(__name__)


class PeerRegistry:
    """
    Maintains the live peer list and syncs it to instances.json.

    When a peer is discovered via mDNS, we update instances.json with:
      - lan_ip: the real IP address on the local network
      - remote_port: the Hashi Remote peer port (8766)
      - active: true

    hchat_send.py reads lan_ip (if present) instead of api_host,
    enabling true cross-machine message delivery.
    """

    def __init__(self, hashi_root: Path, self_instance_id: str):
        self._root = hashi_root
        self._self_id = self_instance_id.upper()
        self._instances_path = hashi_root / "instances.json"
        self._peers: dict[str, PeerInfo] = {}

    def on_peers_changed(self, peers: list[PeerInfo]) -> None:
        """Callback for LanDiscovery — called whenever peers list changes."""
        self._peers = {p.instance_id.upper(): p for p in peers}
        self._sync_to_instances_json()

    def get_peers(self) -> list[PeerInfo]:
        return list(self._peers.values())

    def get_peer(self, instance_id: str) -> Optional[PeerInfo]:
        return self._peers.get(instance_id.upper())

    def _sync_to_instances_json(self) -> None:
        """Write discovered peer IPs into instances.json for hchat routing."""
        if not self._instances_path.exists():
            logger.warning("instances.json not found at %s", self._instances_path)
            return

        try:
            data = json.loads(self._instances_path.read_text(encoding="utf-8"))
            instances = data.get("instances", {})

            changed = False
            for iid, peer in self._peers.items():
                key = iid.lower()
                if key not in instances:
                    # New instance discovered on LAN — add it
                    instances[key] = {
                        "display_name": peer.display_name,
                        "platform": peer.platform,
                        "workbench_port": peer.workbench_port,
                        "api_host": peer.host,
                        "lan_ip": peer.host,
                        "remote_port": peer.port,
                        "active": True,
                        "_discovery": "mdns",
                    }
                    logger.info("Registry: added new peer %s @ %s", iid, peer.host)
                    changed = True
                else:
                    # Update IP/port if changed
                    entry = instances[key]
                    if entry.get("lan_ip") != peer.host or entry.get("remote_port") != peer.port:
                        entry["lan_ip"] = peer.host
                        entry["remote_port"] = peer.port
                        entry["api_host"] = peer.host
                        entry["active"] = True
                        entry["_discovery"] = "mdns"
                        logger.info("Registry: updated peer %s → %s:%d", iid, peer.host, peer.port)
                        changed = True

            if changed:
                data["instances"] = instances
                self._instances_path.write_text(
                    json.dumps(data, indent=4, ensure_ascii=False),
                    encoding="utf-8",
                )
                logger.info("Registry: instances.json updated (%d peers)", len(self._peers))

        except Exception as e:
            logger.error("Registry: failed to sync instances.json: %s", e)
