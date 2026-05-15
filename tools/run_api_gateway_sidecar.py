#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

BRIDGE_HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE_HOME))

from orchestrator.api_gateway import APIGatewayServer
from orchestrator.config import ConfigManager


async def main() -> None:
    cfg, _agents, secrets = ConfigManager(
        BRIDGE_HOME / "agents.json",
        BRIDGE_HOME / "secrets.json",
        bridge_home=BRIDGE_HOME,
    ).load()

    gateway = APIGatewayServer(cfg, secrets, BRIDGE_HOME / "workspaces")
    await gateway.start()
    print(f"API Gateway sidecar listening on http://{gateway.bind_host}:{gateway.port}", flush=True)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    try:
        await stop_event.wait()
    finally:
        await gateway.stop()


if __name__ == "__main__":
    asyncio.run(main())
