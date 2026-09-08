"""Stage one real HASHI Function Worker without taking over live traffic.

This is an operator/CI acceptance probe.  It exercises the exact configured
Agent import and backend construction path, but deliberately leaves the
candidate in READY so Telegram polling and the active route are untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))


def _install_service_endpoints(kernel: object, snapshot_path: Path) -> None:
    """Publish a validated live-service snapshot into an isolated probe."""

    from orchestrator.service_endpoints import endpoint_from_snapshot

    data = json.loads(snapshot_path.read_text(encoding="utf-8-sig"))
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict) or "workbench" not in services:
        raise ValueError("service endpoint snapshot has no workbench endpoint")
    instance_id = str(kernel.global_cfg.instance_id)
    for service in sorted(services):
        endpoint = endpoint_from_snapshot(
            data,
            service,
            expected_instance=instance_id,
        )
        kernel.endpoint_registry.publish(
            service,
            instance_id=endpoint.instance_id,
            scheme=endpoint.scheme,
            host=endpoint.host,
            port=endpoint.port,
            metadata=endpoint.metadata,
        )


async def _run(
    agent_name: str,
    bridge_home: Path,
    service_endpoints: Path | None = None,
) -> int:
    from orchestrator import runtime_app as main
    from orchestrator.pathing import build_bridge_paths

    paths = build_bridge_paths(CODE_ROOT, bridge_home=bridge_home)
    kernel = main.UniversalOrchestrator(paths=paths, selected_agents={agent_name})
    kernel._load_config_bundle()
    if service_endpoints is not None:
        _install_service_endpoints(kernel, service_endpoints)
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
    parser.add_argument(
        "--service-endpoints",
        type=Path,
        help=(
            "Validated live service-endpoint snapshot to publish inside the "
            "isolated probe (required by configured CLI tool gateways)"
        ),
    )
    args = parser.parse_args()
    return asyncio.run(
        _run(
            args.agent,
            args.bridge_home.resolve(),
            args.service_endpoints.resolve() if args.service_endpoints else None,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main_cli())
