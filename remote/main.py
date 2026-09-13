"""
Hashi Remote — Main entry point.

Usage:
    # From the HASHI root directory:
    python -m remote                     # uses config.yaml auto-detected
    python -m remote --no-tls           # disable TLS (dev only)
    python -m remote --port 8766        # custom port
    python -m remote --verbose          # debug logging

Hashi Remote runs alongside HASHI and enables:
  - LAN peer discovery (mDNS)
  - Cross-machine hchat delivery
  - Remote terminal execution (auth-gated)
  - Automatic instances.json update with real peer IPs
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import socket
import sys
import threading
from pathlib import Path
from typing import Optional

import uvicorn

# Add parent to path if running as script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.agent_move.package import (
    AGENT_MOVE_CAPABILITY,
    AGENT_TRANSFER_LIFECYCLE_CAPABILITY,
)
from orchestrator.remote_lifecycle import read_disabled_state
from orchestrator.runtime_defaults import DEFAULT_WORKBENCH_PORT
from orchestrator.stable_port_allocator import (
    SERVICE_HASHI_REMOTE,
    PortAllocationError,
    StablePortAllocator,
)
from remote.api.server import create_app
from remote.live_endpoints import remove_live_endpoint, write_live_endpoint
from remote.peer.base import PeerInfo
from remote.peer.lan import LanDiscovery, build_local_network_profile
from remote.peer.registry import PeerRegistry
from remote.peer.tailscale import TailscaleDiscovery
from remote.port_selection import DEFAULT_PORT
from remote.protocol_manager import (
    PROTOCOL_VERSION,
    ProtocolManager,
    build_default_capabilities,
)
from remote.runtime_identity import (
    remove_runtime_claim,
    validate_launch_context,
    write_runtime_claim,
)
from remote.security.pairing import PairingManager
from remote.security.auth import set_shared_token
from remote.security.shared_token import load_shared_token_snapshot
from remote.security.tls import load_or_generate_cert
from remote.terminal.executor import AuthLevel, TerminalExecutor

logger = logging.getLogger(__name__)


def _build_local_capabilities(*, rescue_start_enabled: bool) -> list[str]:
    capabilities = build_default_capabilities(
        rescue_start_enabled=rescue_start_enabled
    )
    for capability in (
        "file_transfer_hmac_v1",
        "message_attachments_v1",
        AGENT_MOVE_CAPABILITY,
        AGENT_TRANSFER_LIFECYCLE_CAPABILITY,
    ):
        if capability not in capabilities:
            capabilities.append(capability)
    return capabilities


def _load_remote_config(hashi_root: Path) -> dict:
    config_path = hashi_root / "remote" / "config.yaml"
    if not config_path.exists():
        return {}
    server = {}
    security = {}
    discovery = {}
    current = None
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line:
            continue
        if not line.startswith(" "):
            key = line.rstrip(":")
            current = key
            continue
        if ":" not in line or not current:
            continue
        key, raw_value = [part.strip() for part in line.split(":", 1)]
        quoted = (
            len(raw_value) >= 2
            and raw_value[0] in {"\"", "'"}
            and raw_value[-1] == raw_value[0]
        )
        value = raw_value[1:-1] if quoted else raw_value
        target = {"server": server, "security": security, "discovery": discovery}.get(current)
        if target is None:
            continue
        normalized_value = value.lower()
        if not quoted and normalized_value in {"null", "none", "~"}:
            target[key] = None
        elif not quoted and normalized_value in {"true", "false"}:
            target[key] = normalized_value == "true"
        elif not quoted:
            try:
                target[key] = int(value)
            except ValueError:
                target[key] = value
        else:
            target[key] = value
    return {"server": server, "security": security, "discovery": discovery}


def _load_agents_config(hashi_root: Path) -> dict:
    """Load agents.json to get instance info."""
    path = hashi_root / "agents.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {}


def _resolve_configured_remote_port(hashi_root: Path, config: dict | None = None) -> int:
    cfg = _load_agents_config(hashi_root)
    global_cfg = cfg.get("global", {}) if isinstance(cfg, dict) else {}
    instance_id = str(global_cfg.get("instance_id") or "").strip().lower()
    instances_path = hashi_root / "instances.json"
    instances = {}
    if instances_path.exists():
        try:
            instances = json.loads(instances_path.read_text(encoding="utf-8")).get("instances", {}) or {}
        except Exception:
            instances = {}
    entry = instances.get(instance_id, {}) if instance_id else {}
    value = entry.get("remote_port") or global_cfg.get("remote_port")
    if value:
        try:
            return int(value)
        except Exception:
            pass
    server = (config or {}).get("server") or {}
    try:
        return int(server.get("port") or DEFAULT_PORT)
    except Exception:
        return DEFAULT_PORT


def _build_port_allocator(hashi_root: Path, *, host: str) -> StablePortAllocator:
    return StablePortAllocator(
        bridge_home=hashi_root,
        service=SERVICE_HASHI_REMOTE,
        host=host,
    )


def _print_port_assignment_status(allocator: StablePortAllocator) -> int:
    print(json.dumps(allocator.status(), indent=2, sort_keys=True))
    return 0


def _reset_port_assignment(allocator: StablePortAllocator) -> int:
    removed = allocator.reset()
    print(
        json.dumps(
            {
                "service": SERVICE_HASHI_REMOTE,
                "state_path": str(allocator.state_path),
                "removed": removed,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _load_instance_info(
    hashi_root: Path,
    *,
    instance_id: str | None = None,
    display_name: str | None = None,
    workbench_port: int | None = None,
) -> dict:
    """Extract this instance's info from agents.json global section."""
    cfg = _load_agents_config(hashi_root)
    global_cfg = cfg.get("global", {})
    return {
        "instance_id": instance_id or global_cfg.get("instance_id", "HASHI"),
        "display_name": display_name or global_cfg.get("display_name", "HASHI Instance"),
        "workbench_port": (
            workbench_port or global_cfg.get("workbench_port", DEFAULT_WORKBENCH_PORT)
        ),
        "platform": _detect_platform(),
        "hashi_version": cfg.get("version", "unknown"),
    }


def _detect_platform() -> str:
    import platform
    system = platform.system().lower()
    if system == "linux":
        # Check if running inside WSL
        try:
            release = Path("/proc/version").read_text().lower()
            if "microsoft" in release or "wsl" in release:
                return "wsl"
        except Exception:
            pass
        return "linux"
    elif system == "windows":
        return "windows"
    elif system == "darwin":
        return "macos"
    return system


class HashiRemoteApplication:
    """
    Main coordinator for Hashi Remote.

    Starts the FastAPI server, mDNS discovery, and peer registry.
    Gracefully shuts everything down on SIGINT/SIGTERM.
    """

    def __init__(
        self,
        hashi_root: Optional[Path] = None,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        use_tls: bool = True,
        lan_mode: bool = False,
        pairing_auto_approve: bool | None = None,
        pairing_token_ttl_seconds: int | None = None,
        max_terminal_level: str = "L2_WRITE",
        discovery_backend: str = "lan",
        supervised: bool = False,
        instance_id: str | None = None,
        display_name: str | None = None,
        workbench_port: int | None = None,
        control_hashi_root: Optional[Path] = None,
        verbose: bool = False,
    ):
        self._hashi_root = hashi_root or Path(__file__).resolve().parent.parent
        self._control_hashi_root = control_hashi_root or self._hashi_root
        self._host = host
        self._port = port
        self._use_tls = use_tls
        self._lan_mode = lan_mode
        self._pairing_auto_approve = (
            lan_mode if pairing_auto_approve is None else bool(pairing_auto_approve)
        )
        self._pairing_token_ttl_seconds = pairing_token_ttl_seconds
        self._max_terminal_level = AuthLevel[max_terminal_level]
        self._discovery_backend = discovery_backend
        self._supervised = supervised
        self._instance_id_override = instance_id
        self._display_name_override = display_name
        self._workbench_port_override = workbench_port
        self._verbose = verbose

        self._shutdown_event = threading.Event()
        self._uvicorn_server: Optional[uvicorn.Server] = None
        self._discoveries: list = []
        self._registry: Optional[PeerRegistry] = None
        self._protocol_manager: Optional[ProtocolManager] = None
        self._advertisement_task: Optional[asyncio.Task] = None
        self._last_advertised_agent_snapshot = ""
        self._advertised_snapshot_by_backend: dict[int, str] = {}
        self._instance_id = ""
        self._last_loaded_shared_token: str | None = None
        self._last_shared_token_revision = ""

    def _discovery_status(self) -> dict:
        backends = []
        for discovery in self._discoveries:
            get_status = getattr(discovery, "get_status", None)
            if callable(get_status):
                status = dict(get_status() or {})
            else:
                status = {
                    "backend": str(getattr(discovery, "backend_name", "unknown")),
                    "readiness": "unknown",
                    "advertising": None,
                    "browsing": None,
                }
            backends.append(status)

        peer_states: list[str] = []
        fallback_active = False
        peer_count = 0
        if self._registry:
            peers = self._registry.get_peers()
            peer_count = len(peers)
            for peer in peers:
                properties = dict(peer.properties or {})
                peer_states.append(str(properties.get("handshake_state") or "discovered"))
                preferred = str(
                    properties.get("preferred_backend")
                    or properties.get("discovery")
                    or ""
                ).lower()
                fallback_active = fallback_active or preferred.startswith("bootstrap")

        configured = bool(backends)
        ready = configured and all(item.get("readiness") == "ready" for item in backends)
        if not configured:
            readiness = "disabled"
        elif ready:
            readiness = "ready"
        elif any(item.get("readiness") == "starting" for item in backends):
            readiness = "starting"
        else:
            readiness = "degraded"
        accepted_count = sum(state == "handshake_accepted" for state in peer_states)
        rejected_count = sum(state == "handshake_rejected" for state in peer_states)
        if not peer_states:
            trust_state = "no_peers"
        elif accepted_count:
            trust_state = "accepted"
        elif rejected_count:
            trust_state = "rejected"
        else:
            trust_state = "pending"
        if readiness == "ready" and trust_state == "no_peers":
            state = "ready_empty"
        elif readiness == "ready" and trust_state == "accepted":
            state = "ready"
        elif readiness in {"starting", "disabled"}:
            state = readiness
        else:
            state = "degraded"
        return {
            "backend": self._discovery_backend,
            "readiness": readiness,
            "ready": ready,
            "backends": backends,
            "peer_count": peer_count,
            "trusted_peer_count": accepted_count,
            "rejected_peer_count": rejected_count,
            "trust_state": trust_state,
            "state": state,
            "static_seed_fallback_active": fallback_active,
        }

    def _build_self_peer(
        self,
        *,
        instance_info: dict,
        instance_id: str,
        workbench_port: int,
        local_capabilities: list[str],
        agent_directory: dict | None = None,
    ) -> PeerInfo:
        directory = dict(agent_directory or {})
        peer = PeerInfo(
            instance_id=instance_id,
            display_name=instance_info["display_name"],
            host=socket.gethostname(),
            port=self._port,
            workbench_port=workbench_port,
            platform=instance_info["platform"],
            hashi_version=instance_info["hashi_version"],
            display_handle=f"@{instance_id.lower()}",
            protocol_version=PROTOCOL_VERSION,
            capabilities=list(local_capabilities),
            properties={
                "agent_snapshot_version": str(directory.get("version") or ""),
                "directory_state": str(directory.get("directory_state") or ""),
            },
        )
        profile = build_local_network_profile(peer)
        peer.properties["host_identity"] = str(profile.get("host_identity") or "")
        peer.properties["environment_kind"] = str(profile.get("environment_kind") or "")
        return peer

    def _setup_logging(self) -> None:
        level = logging.DEBUG if self._verbose else logging.INFO
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
            handlers=[logging.StreamHandler(sys.stdout)],
        )

    def _setup_signals(self) -> None:
        def handler(signum, frame):
            logger.info("Signal %s received — shutting down", signum)
            self.shutdown()
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    async def _run_async(self) -> None:
        instance_info = _load_instance_info(
            self._hashi_root,
            instance_id=self._instance_id_override,
            display_name=self._display_name_override,
            workbench_port=self._workbench_port_override,
        )
        instance_id = instance_info["instance_id"]
        self._instance_id = str(instance_id or "").strip().upper()
        workbench_port = instance_info["workbench_port"]
        claim = write_runtime_claim(
            root=self._hashi_root,
            instance_id=instance_id,
            port=self._port,
            bind_host=self._host,
            code_root=Path(__file__).resolve().parent.parent,
            supervised=self._supervised,
        )
        instance_info["runtime_claim"] = claim

        logger.info("═" * 55)
        logger.info("  Hashi Remote v1.0.0  🌸")
        logger.info("  Instance : %s", instance_id)
        logger.info("  Platform : %s", instance_info["platform"])
        logger.info("  Peer port: %d  |  Backend API: %d", self._port, workbench_port)
        logger.info("  LAN mode : %s", "on" if self._lan_mode else "off")
        logger.info(
            "  Pairing  : %s",
            "one-click" if self._pairing_auto_approve else "approval-required",
        )
        logger.info(
            "  Pair TTL : %s",
            (
                f"{self._pairing_token_ttl_seconds}s"
                if self._pairing_token_ttl_seconds is not None
                else "unlimited"
            ),
        )
        logger.info("  Discovery: %s", self._discovery_backend)
        token_snapshot = load_shared_token_snapshot(self._hashi_root)
        # Startup is fail-closed. Only a later malformed revision may retain an
        # already loaded in-memory credential in the maintenance loop.
        token_snapshot.require_valid(self._hashi_root)
        shared_token = token_snapshot.token
        self._last_loaded_shared_token = shared_token
        self._last_shared_token_revision = token_snapshot.revision
        logger.info("  Auth     : %s", "shared-token" if shared_token else "discovery-only")
        logger.info("═" * 55)
        if not shared_token:
            logger.warning("Shared token is not configured; protocol trust is disabled and Remote is running in discovery-only mode")
        if token_snapshot.state == "invalid":
            logger.error("Shared-token configuration is invalid: %s", token_snapshot.error)
        if self._lan_mode:
            logger.warning("Legacy LAN mode is enabled; pairing-auth endpoints remain permissive on trusted LANs")

        # Components
        pairing_manager = PairingManager(
            lan_mode=self._lan_mode,
            token_ttl_seconds=self._pairing_token_ttl_seconds,
            auto_approve=self._pairing_auto_approve,
        )
        terminal_executor = TerminalExecutor(
            lan_mode=self._lan_mode,
            max_allowed_level=self._max_terminal_level,
        )
        local_capabilities = _build_local_capabilities(
            rescue_start_enabled=terminal_executor.allows_level(AuthLevel.L3_RESTART)
        )
        instance_info["remote_supervisor"] = {
            "mode": "supervised" if self._supervised else "child",
            "source": "flag_or_env" if self._supervised else "hashi_child_or_manual",
        }

        # Peer registry + discovery
        self._registry = PeerRegistry(self._hashi_root, instance_id)
        self._discoveries = []
        if self._discovery_backend in {"lan", "both"}:
            self._discoveries.append(
                LanDiscovery(
                    self_instance_id=instance_id,
                    on_peers_changed=lambda peers: self._registry.on_peers_changed(
                        peers,
                        backend="lan",
                    ),
                )
            )
        if self._discovery_backend in {"tailscale", "both"}:
            self._discoveries.append(
                TailscaleDiscovery(
                    self_instance_id=instance_id,
                    hashi_root=self._hashi_root,
                    on_peers_changed=lambda peers: self._registry.on_peers_changed(
                        peers,
                        backend="tailscale",
                    ),
                )
            )

        peer_self = self._build_self_peer(
            instance_info=instance_info,
            instance_id=instance_id,
            workbench_port=workbench_port,
            local_capabilities=local_capabilities,
        )
        write_live_endpoint(self._hashi_root, peer_self)

        # Start discovery/advertising
        for discovery in self._discoveries:
            ok = await discovery.advertise(peer_self)
            if ok:
                logger.info("%s: advertising as %s", discovery.backend_name, instance_id)
            else:
                logger.warning("%s: failed to start discovery/advertising", discovery.backend_name)

        instance_info["remote_port"] = self._port
        self._protocol_manager = ProtocolManager(
            hashi_root=self._hashi_root,
            instance_info=instance_info,
            peer_registry=self._registry,
            workbench_port=workbench_port,
            local_capabilities=local_capabilities,
            use_tls=self._use_tls,
            discovery_status_provider=self._discovery_status,
        )
        self._protocol_manager.record_shared_token_config_state(
            token_snapshot.state,
            token_snapshot.error,
        )
        await self._protocol_manager.start()
        self._advertisement_task = asyncio.create_task(
            self._continuous_advertisement_loop(
                instance_info=instance_info,
                instance_id=instance_id,
                workbench_port=workbench_port,
                local_capabilities=local_capabilities,
            )
        )

        # Create FastAPI app
        app = create_app(
            instance_info=instance_info,
            pairing_manager=pairing_manager,
            terminal_executor=terminal_executor,
            peer_registry=self._registry,
            protocol_manager=self._protocol_manager,
            workbench_port=workbench_port,
            hashi_root=str(self._hashi_root),
            control_hashi_root=str(self._control_hashi_root),
        )

        # TLS
        ssl_certfile = ssl_keyfile = None
        if self._use_tls:
            try:
                cert_path, key_path = load_or_generate_cert(socket.gethostname())
                ssl_certfile = str(cert_path)
                ssl_keyfile = str(key_path)
                logger.info("TLS: enabled (cert: %s)", cert_path)
            except Exception as e:
                logger.warning("TLS: cert generation failed (%s), running without TLS", e)

        config = uvicorn.Config(
            app=app,
            host=self._host,
            port=self._port,
            ssl_certfile=ssl_certfile,
            ssl_keyfile=ssl_keyfile,
            log_level="info" if not self._verbose else "debug",
            access_log=self._verbose,
        )
        self._uvicorn_server = uvicorn.Server(config)

        logger.info("Server starting on %s:%d %s",
                    self._host, self._port, "(TLS)" if ssl_certfile else "(plain HTTP)")

        await self._uvicorn_server.serve()

    async def _continuous_advertisement_loop(
        self,
        *,
        instance_info: dict,
        instance_id: str,
        workbench_port: int,
        local_capabilities: list[str],
    ) -> None:
        while not self._shutdown_event.is_set():
            try:
                await self._maintain_discovery_once(
                    instance_info=instance_info,
                    instance_id=instance_id,
                    workbench_port=workbench_port,
                    local_capabilities=local_capabilities,
                )
            except Exception as exc:
                logger.warning("Discovery maintenance failed: %s", exc)
            await asyncio.sleep(1)

    async def _maintain_discovery_once(
        self,
        *,
        instance_info: dict,
        instance_id: str,
        workbench_port: int,
        local_capabilities: list[str],
    ) -> dict:
        if not self._protocol_manager:
            return {"refreshed": False, "failed_backends": [], "reason": "protocol_unavailable"}

        token_snapshot = load_shared_token_snapshot(self._hashi_root)
        if token_snapshot.revision != self._last_shared_token_revision:
            self._last_shared_token_revision = token_snapshot.revision
            self._protocol_manager.record_shared_token_config_state(
                token_snapshot.state,
                token_snapshot.error,
            )
            if token_snapshot.state == "invalid":
                logger.error(
                    "Shared-token configuration reload rejected; retaining the current credential: %s",
                    token_snapshot.error,
                )
            else:
                loaded_token = token_snapshot.token
                set_shared_token(loaded_token)
                changed = self._protocol_manager.reload_shared_token(loaded_token)
                self._last_loaded_shared_token = loaded_token
                if changed:
                    logger.info(
                        "Shared-token configuration changed; authenticated peer handshakes will be re-established"
                    )

        directory = self._protocol_manager.get_local_agent_directory_state()
        version = str(directory.get("version") or "")
        directory_state = str(directory.get("directory_state") or "")
        advertisement_key = f"{version}:{directory_state}"
        should_refresh = advertisement_key != self._last_advertised_agent_snapshot
        peer_self = self._build_self_peer(
            instance_info=instance_info,
            instance_id=instance_id,
            workbench_port=workbench_port,
            local_capabilities=local_capabilities,
            agent_directory=directory,
        )

        results: list[tuple[str, bool, bool]] = []
        for discovery in self._discoveries:
            name = str(getattr(discovery, "backend_name", "unknown"))
            backend_key = id(discovery)
            if self._advertised_snapshot_by_backend.get(backend_key) == advertisement_key:
                continue
            if should_refresh:
                get_status = getattr(discovery, "get_status", None)
                status = dict(get_status() or {}) if callable(get_status) else {}
                retry_due = getattr(discovery, "retry_due", None)
                recovering = str(status.get("readiness") or "") not in {
                    "",
                    "ready",
                    "unknown",
                }
                if recovering and callable(retry_due) and not retry_due():
                    results.append((name, False, False))
                    continue
                update = getattr(discovery, "update_advertisement", None)
                ok = bool(await update(peer_self)) if callable(update) else False
                if ok and callable(get_status):
                    ok = str((get_status() or {}).get("readiness") or "") == "ready"
                if ok:
                    self._advertised_snapshot_by_backend[backend_key] = advertisement_key
                results.append((name, ok, True))
                continue
            retry_due = getattr(discovery, "retry_due", None)
            if callable(retry_due) and retry_due():
                ok = bool(await discovery.advertise(peer_self))
                get_status = getattr(discovery, "get_status", None)
                if ok and callable(get_status):
                    ok = str((get_status() or {}).get("readiness") or "") == "ready"
                results.append((name, ok, True))

        failed_backends = [name for name, ok, _attempted in results if not ok]
        attempted_failures = [
            name for name, ok, attempted in results if attempted and not ok
        ]
        all_backends_current = bool(self._discoveries) and all(
            self._advertised_snapshot_by_backend.get(id(discovery)) == advertisement_key
            for discovery in self._discoveries
        )
        refreshed = bool(should_refresh and all_backends_current)
        if refreshed:
            write_live_endpoint(self._hashi_root, peer_self)
            self._last_advertised_agent_snapshot = advertisement_key
            logger.info("Advertisement refreshed with agent snapshot %s", version or "none")
        elif should_refresh and attempted_failures:
            logger.warning(
                "Advertisement refresh remains pending; failed backends: %s",
                ", ".join(attempted_failures),
            )
        return {
            "refreshed": refreshed,
            "failed_backends": failed_backends,
            "attempted_backends": [
                name for name, _ok, attempted in results if attempted
            ],
            "discovery": self._discovery_status(),
        }

    def run(self) -> int:
        self._setup_logging()
        self._setup_signals()
        try:
            asyncio.run(self._run_async())
            return 0
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt — bye!")
            return 0
        except Exception as e:
            logger.error("Fatal: %s", e, exc_info=True)
            return 1
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self._shutdown_event.is_set():
            return
        self._shutdown_event.set()
        if self._uvicorn_server:
            self._uvicorn_server.should_exit = True
        loop = None
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None

        def _stop(coro) -> None:
            if loop is None:
                asyncio.run(coro)
                return
            if loop.is_running():
                loop.create_task(coro)
            else:
                loop.run_until_complete(coro)

        for discovery in self._discoveries:
            _stop(discovery.stop())
        if self._advertisement_task:
            self._advertisement_task.cancel()
        if self._protocol_manager:
            _stop(self._protocol_manager.stop())
        if self._instance_id:
            remove_live_endpoint(self._hashi_root, self._instance_id)
        remove_runtime_claim(self._hashi_root, pid=os.getpid())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Hashi Remote — LAN peer communication for HASHI instances",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default=None, help="Bind address")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Peer API port. Explicit ports are validated for this run but are not persisted.",
    )
    parser.add_argument("--no-tls", action="store_true", help="Disable TLS (dev/debug only)")
    parser.add_argument("--no-lan-mode", action="store_true",
                        help="Require token auth even on LAN (for internet deployments)")
    parser.add_argument(
        "--pairing-token-ttl-seconds",
        type=int,
        default=None,
        help="Expire bearer pairing tokens after this many seconds; omitted means unlimited",
    )
    parser.add_argument(
        "--pairing-auto-approve",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Approve pairing requests immediately while still requiring the issued token",
    )
    parser.add_argument("--discovery", choices=["lan", "tailscale", "both"], default=None,
                        help="Peer discovery backend")
    parser.add_argument("--max-terminal-level", default=None,
                        choices=["L0_READ_ONLY", "L1_READ_FILES", "L2_WRITE", "L3_RESTART"],
                        help="Maximum allowed terminal auth level")
    parser.add_argument("--hashi-root", type=Path, default=None,
                        help="HASHI root directory (auto-detected if omitted)")
    parser.add_argument("--control-hashi-root", type=Path, default=None,
                        help="HASHI root controlled by rescue endpoints; defaults to --hashi-root")
    parser.add_argument("--instance-id", default=None,
                        help="Advertised Remote instance id, for standalone sidecars such as WATCHTOWER")
    parser.add_argument("--display-name", default=None,
                        help="Advertised Remote display name")
    parser.add_argument("--workbench-port", type=int, default=None,
                        help="Backend API port monitored by rescue status (compatibility option name)")
    parser.add_argument("--supervised", action="store_true",
                        help="Mark this Remote as OS-supervised side-program")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    parser.add_argument(
        "--check-port-assignment",
        action="store_true",
        help="Print persisted Hashi Remote port-assignment status and exit",
    )
    parser.add_argument(
        "--reset-port-assignment",
        action="store_true",
        help="Clear persisted Hashi Remote port assignment and exit",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    hashi_root = args.hashi_root
    if hashi_root is None:
        hashi_root = Path(__file__).resolve().parent.parent
    hashi_root = hashi_root.expanduser().resolve()
    try:
        validate_launch_context(hashi_root=hashi_root)
    except RuntimeError as exc:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
        logger.error("%s", exc)
        return 1
    host_for_tools = args.host or _load_remote_config(hashi_root).get("server", {}).get("host", "0.0.0.0")
    allocator = _build_port_allocator(hashi_root, host=host_for_tools)
    if args.check_port_assignment:
        return _print_port_assignment_status(allocator)
    if args.reset_port_assignment:
        return _reset_port_assignment(allocator)
    disabled_state = read_disabled_state(hashi_root)
    if disabled_state:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
        logger.info(
            "Hashi Remote is explicitly disabled; exiting without start (state=%s reason=%s)",
            disabled_state.get("path"),
            disabled_state.get("reason"),
        )
        return 0
    config = _load_remote_config(hashi_root)
    server_cfg = config.get("server", {})
    security_cfg = config.get("security", {})
    discovery_cfg = config.get("discovery", {})
    configured_port = _resolve_configured_remote_port(hashi_root, config)

    host = host_for_tools
    try:
        if args.port is not None:
            assignment = allocator.validate_explicit_port(args.port)
        else:
            assignment = allocator.reserve_configured_port(configured_port)
    except PortAllocationError as exc:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
        logger.error("%s", exc)
        return 1
    port = assignment.port
    attempted_ports = assignment.attempted_ports
    if assignment.source == "allocated":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
        logger.warning(
            "Configured Remote port %s is unavailable on %s; allocated stable fallback %s (attempted=%s state=%s)",
            configured_port,
            host,
            port,
            attempted_ports,
            assignment.state_path,
        )
    use_tls = not args.no_tls if args.no_tls else server_cfg.get("use_tls", True)
    lan_mode = not args.no_lan_mode if args.no_lan_mode else security_cfg.get("lan_mode", False)
    pairing_auto_approve = (
        args.pairing_auto_approve
        if args.pairing_auto_approve is not None
        else security_cfg.get("pairing_auto_approve")
    )
    pairing_token_ttl_seconds = (
        args.pairing_token_ttl_seconds
        if args.pairing_token_ttl_seconds is not None
        else security_cfg.get("pairing_token_ttl_seconds")
    )
    if pairing_token_ttl_seconds is not None:
        pairing_token_ttl_seconds = int(pairing_token_ttl_seconds)
    discovery_backend = args.discovery or os.getenv("HASHI_REMOTE_DISCOVERY") or discovery_cfg.get("backend", "lan")
    max_terminal_level = args.max_terminal_level or security_cfg.get("max_terminal_level", "L2_WRITE")
    supervised = args.supervised or os.getenv("HASHI_REMOTE_SUPERVISED", "").strip().lower() in {"1", "true", "yes", "on"}

    app = HashiRemoteApplication(
        hashi_root=hashi_root,
        host=host,
        port=port,
        use_tls=use_tls,
        lan_mode=lan_mode,
        pairing_auto_approve=pairing_auto_approve,
        pairing_token_ttl_seconds=pairing_token_ttl_seconds,
        max_terminal_level=max_terminal_level,
        discovery_backend=discovery_backend,
        supervised=supervised,
        instance_id=args.instance_id,
        display_name=args.display_name,
        workbench_port=args.workbench_port,
        control_hashi_root=args.control_hashi_root.expanduser().resolve() if args.control_hashi_root else None,
        verbose=args.verbose,
    )
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
