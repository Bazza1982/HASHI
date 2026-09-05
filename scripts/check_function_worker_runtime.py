"""Stage one real HASHI Function Worker without taking over live traffic.

This is an operator/CI acceptance probe.  It exercises the exact configured
Agent import and backend construction path, but deliberately leaves the
candidate in READY so Telegram polling and the active route are untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))


async def _run(agent_name: str, bridge_home: Path) -> int:
    import main
    from orchestrator.pathing import build_bridge_paths

    paths = build_bridge_paths(CODE_ROOT, bridge_home=bridge_home)
    kernel = main.UniversalOrchestrator(paths=paths, selected_agents={agent_name})
    kernel._load_config_bundle()
    client = await kernel.function_workers.prepare_worker(agent_name)
    try:
        ping = await client.call("worker.ping", timeout=30.0)
        print(
            "Function Worker READY: "
            f"agent={agent_name} pid={client.pid} "
            f"generation={client.generation_id} phase={ping['phase']}",
            flush=True,
        )
    finally:
        await client.shutdown(force=True)
    return 0


def main_cli() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", required=True)
    parser.add_argument("--bridge-home", type=Path, default=Path.cwd())
    args = parser.parse_args()
    return asyncio.run(_run(args.agent, args.bridge_home.resolve()))


if __name__ == "__main__":
    raise SystemExit(main_cli())
