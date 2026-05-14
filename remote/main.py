"""
Hashi Remote — Main entry point.

Usage:
    # From the HASHI root directory:
    python -m remote                     # uses config.yaml auto-detected
    python -m remote --no-tls           # disable TLS (dev only)
    python -m remote --port 8766        # custom port
    python -m remote --verbose          # debug logging

Hashi Remote runs alongside the HASHI Workbench and enables:
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

from orchestrator.remote_lifecycle import read_disabled_state
from remote.api.server import create_app
from remote.live_endpoints import remove_live_endpoint
from remote.peer.base import PeerInfo
from remote.peer.lan import LanDiscovery
from remote.peer.registry import PeerRegistry
from remote.port_selection import DEFAULT_PORT, select_available_port
from remote.peer.tailscale import TailscaleDiscovery
from remote.protocol_manager import ProtocolManager, PROTOCOL_VERSION, build_default_capabilities
from remote.runtime_identity import remove_runtime_claim, validate_launch_context, write_runtime_claim
from remote.security.pairing import PairingManager
from remote.security.shared_token import load_shared_token
from remote.security.tls import load_or_generate_cert
from remote.terminal.executor import TerminalExecutor, AuthLevel

logger = logging.getLogger(__name__)

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
        key, value = [part.strip() for part in line.split(":", 1)]
        value = value.strip("\"'")
        target = {"server": server, "security": security, "discovery": discovery}.get(current)
        if target is None:
            continue
        if value.lower() in {"true", "false"}:
            target[key] = value.lower() == "true"
        else:
            try:
                target[key] = int(value)
            except ValueError:
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


def _load_instance_info(hashi_root: Path) -> dict:
    """Extract this instance's info from agents.json global section."""
    cfg = _load_agents_config(hashi_root)
    global_cfg = cfg.get("global", {})
    return {
        "instance_id": global_cfg.get("instance_id", "HASHI"),
        "display_name": global_cfg.get("display_name", "HASHI Instance"),
        "workbench_port": global_cfg.get("workbench_port", 18800),
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
        max_terminal_level: str = "L2_WRITE",
        discovery_backend: str = "lan",
        supervised: bool = False,
        verbose: bool = False,
    ):
        self._hashi_root = hashi_root or Path(__file__).resolve().parent.parent
        self._host = host
        self._port = port
        self._use_tls = use_tls
        self._lan_mode = lan_mode
        self._max_terminal_level = AuthLevel[max_terminal_level]
        self._discovery_backend = discovery_backend
        self._supervised = supervised
        self._verbose = verbose

        self._shutdown_event = threading.Event()
        self._uvicorn_server: Optional[uvicorn.Server] = None
        self._discoveries: list = []
        self._registry: Optional[PeerRegistry] = None
        self._protocol_manager: Optional[ProtocolManager] = None
        self._advertisement_task: Optional[asyncio.Task] = None
        self._last_advertised_agent_snapshot = ""
        self._instance_id = ""

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
        return PeerInfo(
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
        instance_info = _load_instance_info(self._hashi_root)
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
        logger.info("  Peer port: %d  |  Workbench: %d", self._port, workbench_port)
        logger.info("  LAN mode : %s", "on" if self._lan_mode else "off")
        logger.info("  Discovery: %s", self._discovery_backend)
        shared_token = load_shared_token(self._hashi_root)
        logger.info("  Auth     : %s", "shared-token" if shared_token else "discovery-only")
        logger.info("═" * 55)
        if not shared_token:
            logger.warning("Shared token is not configured; protocol trust is disabled and Remote is running in discovery-only mode")
        if self._lan_mode:
            logger.warning("Legacy LAN mode is enabled; pairing-auth endpoints remain permissive on trusted LANs")

        # Components
        pairing_manager = PairingManager(lan_mode=self._lan_mode)
        terminal_executor = TerminalExecutor(
            lan_mode=self._lan_mode,
            max_allowed_level=self._max_terminal_level,
        )
        local_capabilities = build_default_capabilities(
            rescue_start_enabled=terminal_executor.allows_level(AuthLevel.L3_RESTART)
        )
        for capability in ("file_transfer_hmac_v1", "message_attachments_v1"):
            if capability not in local_capabilities:
                local_capabilities.append(capability)
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
                    on_peers_changed=self._registry.on_peers_changed,
                )
            )
        if self._discovery_backend in {"tailscale", "both"}:
            self._discoveries.append(
                TailscaleDiscovery(
                    self_instance_id=instance_id,
                    hashi_root=self._hashi_root,
                    on_peers_changed=self._registry.on_peers_changed,
                )
            )

        peer_self = self._build_self_peer(
            instance_info=instance_info,
            instance_id=instance_id,
            workbench_port=workbench_port,
            local_capabilities=local_capabilities,
        )

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
                if not self._protocol_manager:
                    await asyncio.sleep(30)
                    continue
                directory = self._protocol_manager.get_local_agent_directory_state()
                version = str(directory.get("version") or "")
                directory_state = str(directory.get("directory_state") or "")
                advertisement_key = f"{version}:{directory_state}"
                should_refresh = advertisement_key != self._last_advertised_agent_snapshot
                if should_refresh:
                    peer_self = self._build_self_peer(
                        instance_info=instance_info,
                        instance_id=instance_id,
                        workbench_port=workbench_port,
                        local_capabilities=local_capabilities,
                        agent_directory=directory,
                    )
                    for discovery in self._discoveries:
                        update = getattr(discovery, "update_advertisement", None)
                        if update is not None:
                            await update(peer_self)
                    self._last_advertised_agent_snapshot = advertisement_key
                    logger.info("Advertisement refreshed with agent snapshot %s", version or "none")
            except Exception as exc:
                logger.warning("Advertisement refresh failed: %s", exc)
            await asyncio.sleep(30)

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
    parser.add_argument("--port", type=int, default=None, help="Peer API port")
    parser.add_argument("--no-tls", action="store_true", help="Disable TLS (dev/debug only)")
    parser.add_argument("--no-lan-mode", action="store_true",
                        help="Require token auth even on LAN (for internet deployments)")
    parser.add_argument("--discovery", choices=["lan", "tailscale", "both"], default=None,
                        help="Peer discovery backend")
    parser.add_argument("--max-terminal-level", default=None,
                        choices=["L0_READ_ONLY", "L1_READ_FILES", "L2_WRITE", "L3_RESTART"],
                        help="Maximum allowed terminal auth level")
    parser.add_argument("--hashi-root", type=Path, default=None,
                        help="HASHI root directory (auto-detected if omitted)")
    parser.add_argument("--supervised", action="store_true",
                        help="Mark this Remote as OS-supervised side-program")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
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

    host = args.host or server_cfg.get("host", "0.0.0.0")
    requested_port = args.port or configured_port
    port, attempted_ports = select_available_port(
        host,
        requested_port,
        configured_port,
    )
    if port != requested_port:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
        logger.warning(
            "Configured Remote port %s is unavailable on %s; using %s instead (attempted=%s)",
            requested_port,
            host,
            port,
            attempted_ports,
        )
    use_tls = not args.no_tls if args.no_tls else server_cfg.get("use_tls", True)
    lan_mode = not args.no_lan_mode if args.no_lan_mode else security_cfg.get("lan_mode", False)
    discovery_backend = args.discovery or os.getenv("HASHI_REMOTE_DISCOVERY") or discovery_cfg.get("backend", "lan")
    max_terminal_level = args.max_terminal_level or security_cfg.get("max_terminal_level", "L2_WRITE")
    supervised = args.supervised or os.getenv("HASHI_REMOTE_SUPERVISED", "").strip().lower() in {"1", "true", "yes", "on"}

    app = HashiRemoteApplication(
        hashi_root=hashi_root,
        host=host,
        port=port,
        use_tls=use_tls,
        lan_mode=lan_mode,
        max_terminal_level=max_terminal_level,
        discovery_backend=discovery_backend,
        supervised=supervised,
        verbose=args.verbose,
    )
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
