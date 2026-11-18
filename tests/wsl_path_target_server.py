"""Disposable Backend API target for the Windows-to-WSL path integration test."""
from __future__ import annotations

import asyncio
import json
import signal
import tempfile
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web

from orchestrator.workbench_api import WorkbenchApiServer


async def main():
    with tempfile.TemporaryDirectory(prefix="hashi-wsl-target-api-") as directory:
        root = Path(directory)
        config = root / "agents.json"
        config.write_text(json.dumps({"global": {}, "agents": []}), encoding="utf-8")
        settings = SimpleNamespace(
            bridge_home=root, project_root=root, instance_id="PATH-TEST",
            deployment_profile="personal", authorized_id=1,
        )
        server = WorkbenchApiServer(
            config_path=config, global_config=settings,
            secrets={"workbench_admin_token": "synthetic-path-target-token"},
            reconcile_session_runs=False,
        )
        runner = web.AppRunner(server.app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        sockets = site._server.sockets
        print("HASHI_TEST_PORT=" + str(sockets[0].getsockname()[1]), flush=True)
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, stopped.set)
        loop.add_signal_handler(signal.SIGINT, stopped.set)
        try:
            await stopped.wait()
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
# HASHI-Fingerprint: hfp_b3f5a3f85f2b4088b74479be1d0f91fe
