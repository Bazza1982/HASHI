"""
Tailscale peer discovery for Hashi Remote.

This backend uses `tailscale status --json` or a pre-exported JSON file to
discover online HASHI peers across tailnet / internet-friendly networks.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

from remote.live_endpoints import read_live_endpoints

from .base import PeerDiscovery, PeerInfo

logger = logging.getLogger(__name__)


class TailscaleDiscovery(PeerDiscovery):
    def __init__(
        self,
        self_instance_id: str,
        hashi_root: Path,
        on_peers_changed: Optional[Callable] = None,
        poll_seconds: int = 15,
    ):
        self._self_id = self_instance_id.upper()
        self._hashi_root = hashi_root
        self._on_peers_changed = on_peers_changed
        self._poll_seconds = max(5, poll_seconds)
        self._self_info: Optional[PeerInfo] = None
        self._peers: dict[str, PeerInfo] = {}
        self._poll_task: Optional[asyncio.Task] = None
        self._running = False

    @property
    def backend_name(self) -> str:
        return "Tailscale"

    async def advertise(self, info: PeerInfo) -> bool:
        self._self_info = info
        if not self._tailscale_available():
            logger.warning("TailscaleDiscovery: tailscale binary/status file not available")
            return False
        self._running = True
        await self._refresh_once()
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("TailscaleDiscovery: polling every %ss", self._poll_seconds)
        return True

    async def update_advertisement(self, info: PeerInfo) -> bool:
        self._self_info = info
        return self._running

    async def discover(self) -> list[PeerInfo]:
        return list(self._peers.values())

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._refresh_once()
            except Exception as exc:
                logger.warning("TailscaleDiscovery: refresh failed: %s", exc)
            await asyncio.sleep(self._poll_seconds)

    async def _refresh_once(self) -> None:
        peers = self._load_peers()
        new_map = {peer.instance_id.upper(): peer for peer in peers}
        if new_map != self._peers:
            self._peers = new_map
            if self._on_peers_changed:
                self._on_peers_changed(list(self._peers.values()))

    def _tailscale_available(self) -> bool:
        status_file = os.getenv("HASHI_TAILSCALE_STATUS_JSON")
        if status_file and Path(status_file).exists():
            return True
        return shutil.which("tailscale") is not None

    def _load_status_json(self) -> dict:
        status_file = os.getenv("HASHI_TAILSCALE_STATUS_JSON")
        if status_file:
            return json.loads(Path(status_file).read_text(encoding="utf-8"))
        proc = subprocess.run(
            ["tailscale", "status", "--json"],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(proc.stdout)

    def _load_instances(self) -> dict:
        instances_path = self._hashi_root / "instances.json"
        if not instances_path.exists():
            return {}
        try:
            data = json.loads(instances_path.read_text(encoding="utf-8-sig"))
            return data.get("instances", {})
        except Exception:
            return {}

    def _load_peers(self) -> list[PeerInfo]:
        status = self._load_status_json()
        peers = []
        instances = self._load_instances()
        live_endpoints = read_live_endpoints(self._hashi_root)
        for node in status.get("Peer", {}).values():
            if not node.get("Online"):
                continue
            instance_id = self._infer_instance_id(node)
            if not instance_id or instance_id == self._self_id:
                continue

            instance_info = instances.get(instance_id.lower(), {})
            live_info = live_endpoints.get(instance_id.lower(), {})
            host = self._pick_host(node)
            if not host:
                continue
            port = int(live_info.get("port") or instance_info.get("announced_port") or instance_info.get("remote_port") or 8766)

            peers.append(
                PeerInfo(
                    instance_id=instance_id,
                    display_name=live_info.get("display_name") or node.get("HostName") or instance_info.get("display_name") or instance_id,
                    host=host,
                    port=port,
                    workbench_port=int(live_info.get("workbench_port") or instance_info.get("workbench_port") or 18800),
                    platform=live_info.get("platform") or instance_info.get("platform", "unknown"),
                    hashi_version=node.get("OS", "unknown"),
                    display_handle=f"@{instance_id.lower()}",
                    protocol_version=str(live_info.get("protocol_version") or instance_info.get("protocol_version") or "1.0"),
                    capabilities=list(live_info.get("capabilities") or instance_info.get("capabilities") or []),
                    properties={
                        "discovery": "tailscale",
                        "live_endpoint_source": "cache" if live_info else "seed",
                        "host_identity": str(live_info.get("host_identity") or instance_info.get("host_identity") or ""),
                        "environment_kind": str(live_info.get("environment_kind") or instance_info.get("environment_kind") or ""),
                    },
                )
            )
        return peers

    def _infer_instance_id(self, node: dict) -> Optional[str]:
        candidates = [
            node.get("HostName", ""),
            node.get("DNSName", ""),
            node.get("Name", ""),
        ]
        for value in candidates:
            match = re.search(r"(hashi\d+)", value.lower())
            if match:
                return match.group(1).upper()
        tags = node.get("Tags") or []
        for tag in tags:
            match = re.search(r"hashi\d+", str(tag).lower())
            if match:
                return match.group(0).upper()
        return None

    def _pick_host(self, node: dict) -> Optional[str]:
        ips = node.get("TailscaleIPs") or []
        if ips:
            return ips[0]
        dns_name = node.get("DNSName")
        if dns_name:
            return dns_name.rstrip(".")
        host_name = node.get("HostName")
        if host_name:
            return host_name
        return None
