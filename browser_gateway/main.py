from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path

from browser_gateway.server import BrowserGatewayServer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HASHI OLL Browser Gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8876)
    parser.add_argument("--workbench-url", default="http://127.0.0.1:18800")
    parser.add_argument("--state-db", default="")
    parser.add_argument("--audit-log", default="")
    parser.add_argument("--public-base-url", default="")
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parent.parent
    state_db = Path(args.state_db) if args.state_db else root / "state" / "browser_gateway.sqlite"
    audit_log = Path(args.audit_log) if args.audit_log else root / "logs" / "oll_gateway.audit.jsonl"
    server = BrowserGatewayServer(
        project_root=root,
        host=args.host,
        port=args.port,
        workbench_url=args.workbench_url,
        state_db=state_db,
        audit_log=audit_log,
        public_base_url=args.public_base_url,
    )
    await server.start()
    stop_event = asyncio.Event()

    def _stop(*_args):
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _stop)

    await stop_event.wait()
    await server.stop()
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    args = parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
