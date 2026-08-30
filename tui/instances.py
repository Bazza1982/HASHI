"""Instance discovery and connection targets for the HASHI TUI.

The repository that owns ``tui.py`` is always the launch instance.  Other
instances are discovered exclusively through that instance's local Hashi
Remote sidecar.  A peer is switchable only after a successful protocol
handshake and only when it advertises the restricted TUI proxy capability.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from orchestrator.runtime_defaults import (
    DEFAULT_HASHI_REMOTE_PORT,
    DEFAULT_WORKBENCH_PORT,
)
from remote.local_http import local_http_hosts

logger = logging.getLogger(__name__)
TUI_PROXY_CAPABILITY = "tui_proxy_v1"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError) as exc:
        logger.warning("TUI instance config unreadable: path=%s error=%s", path, exc)
        return {}


def load_launch_instance(bridge_home: Path) -> tuple[str, int]:
    """Return the identity and Workbench port owned by this TUI repository."""
    config = _read_json(Path(bridge_home) / "agents.json")
    global_cfg = config.get("global") if isinstance(config.get("global"), dict) else {}
    instance_id = str(global_cfg.get("instance_id") or "HASHI").strip().upper()
    try:
        workbench_port = int(global_cfg.get("workbench_port") or DEFAULT_WORKBENCH_PORT)
    except (TypeError, ValueError):
        workbench_port = DEFAULT_WORKBENCH_PORT
    return instance_id, workbench_port


def local_workbench_urls(workbench_port: int) -> list[str]:
    """Build local-only Workbench candidates, including WSL host aliases."""
    urls: list[str] = []
    for host in (*local_http_hosts(), "localhost", "127.0.0.1"):
        url = f"http://{host}:{int(workbench_port)}"
        if url not in urls:
            urls.append(url)
    return urls


@dataclass(frozen=True)
class InstanceTarget:
    """A verified route that can become the TUI's active instance."""

    instance_id: str
    display_name: str
    current: bool
    available: bool
    transport: str
    workbench_urls: tuple[str, ...] = ()
    remote_url: str = ""
    handshake_state: str = ""
    live_status: str = ""
    route_kind: str = ""
    reason: str = ""


class InstanceResolver:
    """Discover trusted peers through the launch instance's local Remote."""

    def __init__(self, bridge_home: Path, current_workbench_urls: list[str] | tuple[str, ...]):
        self.bridge_home = Path(bridge_home).resolve()
        self.launch_instance_id, _port = load_launch_instance(self.bridge_home)
        self.current_workbench_urls = tuple(current_workbench_urls)
        self._remote_url: str | None = None

    def current_target(self) -> InstanceTarget:
        return InstanceTarget(
            instance_id=self.launch_instance_id,
            display_name=self.launch_instance_id,
            current=True,
            available=True,
            transport="direct",
            workbench_urls=self.current_workbench_urls,
            live_status="online",
            route_kind="local",
        )

    def _remote_ports(self) -> list[int]:
        ports: list[int] = []

        def add(value: object) -> None:
            try:
                port = int(value or 0)
            except (TypeError, ValueError):
                return
            if 0 < port <= 65535 and port not in ports:
                ports.append(port)

        instances = _read_json(self.bridge_home / "instances.json")
        entries = instances.get("instances") if isinstance(instances.get("instances"), dict) else instances
        own_entry = entries.get(self.launch_instance_id.lower(), {}) if isinstance(entries, dict) else {}
        if isinstance(own_entry, dict):
            add(own_entry.get("remote_port"))

        live = _read_json(self.bridge_home / "state" / "remote_live_endpoints.json")
        endpoints = live.get("endpoints") if isinstance(live.get("endpoints"), dict) else {}
        own_live = endpoints.get(self.launch_instance_id.lower(), {})
        if isinstance(own_live, dict):
            add(own_live.get("remote_port") or own_live.get("port"))

        agents = _read_json(self.bridge_home / "agents.json")
        global_cfg = agents.get("global") if isinstance(agents.get("global"), dict) else {}
        add(global_cfg.get("remote_port"))
        add(DEFAULT_HASHI_REMOTE_PORT)
        return ports

    def _remote_candidates(self) -> list[str]:
        candidates: list[str] = []
        for port in self._remote_ports():
            for host in (*local_http_hosts(), "localhost", "127.0.0.1"):
                url = f"http://{host}:{port}"
                if url not in candidates:
                    candidates.append(url)
        return candidates

    async def _find_local_remote(self) -> str | None:
        candidates = []
        if self._remote_url:
            candidates.append(self._remote_url)
        candidates.extend(url for url in self._remote_candidates() if url not in candidates)
        timeout = aiohttp.ClientTimeout(total=1.5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for base_url in candidates:
                try:
                    async with session.get(f"{base_url}/health") as response:
                        data = await response.json(content_type=None)
                    remote_id = str((data.get("instance") or {}).get("instance_id") or "").upper()
                    if response.status == 200 and data.get("ok") and remote_id == self.launch_instance_id:
                        self._remote_url = base_url
                        return base_url
                    if response.status == 200 and remote_id:
                        logger.warning(
                            "TUI ignored wrong local Remote: expected=%s actual=%s url=%s",
                            self.launch_instance_id,
                            remote_id,
                            base_url,
                        )
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    logger.debug("TUI local Remote candidate unavailable: url=%s", base_url)
        self._remote_url = None
        return None

    async def discover(self, *, refresh: bool = True) -> list[InstanceTarget]:
        """Return the launch instance plus peers trusted by its local Remote."""
        targets = [self.current_target()]
        remote_url = await self._find_local_remote()
        if not remote_url:
            logger.warning("TUI instance discovery unavailable: local Hashi Remote is offline")
            return targets

        query = "?refresh=1" if refresh else ""
        try:
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{remote_url}/peers{query}") as response:
                    data = await response.json(content_type=None)
            if response.status != 200 or not data.get("ok"):
                logger.warning("TUI peer discovery failed: status=%s url=%s", response.status, remote_url)
                return targets
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning("TUI peer discovery failed: url=%s error=%s", remote_url, exc)
            return targets

        for peer in data.get("peers") or []:
            if not isinstance(peer, dict):
                continue
            instance_id = str(peer.get("instance_id") or "").strip().upper()
            if not instance_id or instance_id == self.launch_instance_id:
                continue
            properties = peer.get("properties") if isinstance(peer.get("properties"), dict) else {}
            handshake = str(properties.get("handshake_state") or peer.get("handshake_state") or "").lower()
            live_status = str(properties.get("live_status") or peer.get("live_status") or "unknown").lower()
            capabilities = {
                str(item).strip() for item in (peer.get("capabilities") or []) if str(item).strip()
            }
            handshake_ok = handshake == "handshake_accepted"
            live_ok = live_status not in {"offline", "unknown"}
            proxy_ok = TUI_PROXY_CAPABILITY in capabilities
            available = handshake_ok and live_ok and proxy_ok
            if not handshake_ok:
                reason = "handshake required"
            elif not live_ok:
                reason = f"Remote {live_status}"
            elif not proxy_ok:
                reason = "peer needs TUI proxy upgrade/restart"
            else:
                reason = ""
            targets.append(
                InstanceTarget(
                    instance_id=instance_id,
                    display_name=str(peer.get("display_name") or instance_id),
                    current=False,
                    available=available,
                    transport="remote",
                    remote_url=remote_url,
                    handshake_state=handshake,
                    live_status=live_status,
                    route_kind=str(peer.get("route_kind") or properties.get("preferred_backend") or "remote"),
                    reason=reason,
                )
            )
        targets[1:] = sorted(targets[1:], key=lambda item: item.instance_id)
        logger.info("TUI instance discovery complete: peers=%d remote=%s", len(targets) - 1, remote_url)
        return targets
