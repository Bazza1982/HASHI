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
import signal
import socket
import sys
import threading
from pathlib import Path
from typing import Optional

import uvicorn

# Add parent to path if running as script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from remote.api.server import create_app
from remote.peer.base import PeerInfo
from remote.peer.lan import LanDiscovery
from remote.peer.registry import PeerRegistry
from remote.security.pairing import PairingManager
from remote.security.tls import load_or_generate_cert
from remote.terminal.executor import TerminalExecutor, AuthLevel

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8766


def _load_agents_config(hashi_root: Path) -> dict:
    """Load agents.json to get instance info."""
    path = hashi_root / "agents.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {}


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
        lan_mode: bool = True,
        max_terminal_level: str = "L2_WRITE",
        verbose: bool = False,
    ):
        self._hashi_root = hashi_root or Path(__file__).resolve().parent.parent
        self._host = host
        self._port = port
        self._use_tls = use_tls
        self._lan_mode = lan_mode
        self._max_terminal_level = AuthLevel[max_terminal_level]
        self._verbose = verbose

        self._shutdown_event = threading.Event()
        self._uvicorn_server: Optional[uvicorn.Server] = None
        self._discovery: Optional[LanDiscovery] = None
        self._registry: Optional[PeerRegistry] = None

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
        workbench_port = instance_info["workbench_port"]

        logger.info("═" * 55)
        logger.info("  Hashi Remote v1.0.0  🌸")
        logger.info("  Instance : %s", instance_id)
        logger.info("  Platform : %s", instance_info["platform"])
        logger.info("  Peer port: %d  |  Workbench: %d", self._port, workbench_port)
        logger.info("  LAN mode : %s", "on" if self._lan_mode else "off")
        logger.info("═" * 55)

        # Components
        pairing_manager = PairingManager(lan_mode=self._lan_mode)
        terminal_executor = TerminalExecutor(
            lan_mode=self._lan_mode,
            max_allowed_level=self._max_terminal_level,
        )

        # Peer registry + discovery
        self._registry = PeerRegistry(self._hashi_root, instance_id)
        self._discovery = LanDiscovery(
            self_instance_id=instance_id,
            on_peers_changed=self._registry.on_peers_changed,
        )

        peer_self = PeerInfo(
            instance_id=instance_id,
            display_name=instance_info["display_name"],
            host=socket.gethostname(),
            port=self._port,
            workbench_port=workbench_port,
            platform=instance_info["platform"],
            hashi_version=instance_info["hashi_version"],
        )

        # Start mDNS advertising
        ok = await self._discovery.advertise(peer_self)
        if ok:
            logger.info("mDNS: advertising as %s", instance_id)
        else:
            logger.warning("mDNS: failed to start advertising — peer discovery unavailable")

        # Create FastAPI app
        app = create_app(
            instance_info=instance_info,
            pairing_manager=pairing_manager,
            terminal_executor=terminal_executor,
            peer_registry=self._registry,
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
        if self._discovery:
            asyncio.get_event_loop().run_until_complete(self._discovery.stop())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Hashi Remote — LAN peer communication for HASHI instances",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Peer API port")
    parser.add_argument("--no-tls", action="store_true", help="Disable TLS (dev/debug only)")
    parser.add_argument("--no-lan-mode", action="store_true",
                        help="Require token auth even on LAN (for internet deployments)")
    parser.add_argument("--max-terminal-level", default="L2_WRITE",
                        choices=["L0_READ_ONLY", "L1_READ_FILES", "L2_WRITE", "L3_RESTART"],
                        help="Maximum allowed terminal auth level")
    parser.add_argument("--hashi-root", type=Path, default=None,
                        help="HASHI root directory (auto-detected if omitted)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    hashi_root = args.hashi_root
    if hashi_root is None:
        hashi_root = Path(__file__).resolve().parent.parent

    app = HashiRemoteApplication(
        hashi_root=hashi_root,
        host=args.host,
        port=args.port,
        use_tls=not args.no_tls,
        lan_mode=not args.no_lan_mode,
        max_terminal_level=args.max_terminal_level,
        verbose=args.verbose,
    )
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
