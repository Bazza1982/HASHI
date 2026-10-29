"""One owned HERV3 execution host. Launch only through the verified runner.

This program receives its command over an explicitly inherited private handle.
Model calls, tools, durable checkpoints and audit writes use only its assigned
execution channel. It has no Functions/Core RPC client or approval endpoint.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
import sys

# The qualified isolated interpreter does not search the working directory.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from adapters.her_v2_provider import HashiStageProvider, _AdapterDelivery
from adapters.stream_events import StreamEvent
from orchestrator.her_v2.audit import DurableAuditLog
from orchestrator.her_v2.config import HERv2Config
from orchestrator.her_v2.ledger import LedgerStore
from orchestrator.her_v2.runtime import HERv2Runtime
from orchestrator.permissions.continuation import PermissionPark
from orchestrator.permissions.host_protocol import pack, unpack
from orchestrator.permissions.host_provider import BrokeredProviderFactory
from orchestrator.permissions.remote_registry import BrokeredToolRegistry

_FIELDS = frozenset({
    "config", "profiles", "tool_definitions", "privacy_level", "prompt", "request_id",
    "turn_id", "effort", "request_content", "request_mode", "pcm_input", "recovery",
    "skills_catalogue",
})


class _BrokerAuditWriter:
    def __init__(self, client):
        self._client = client

    def append(self, record):
        return self._client.call("audit_record", {"record": dict(record)})


async def run_command(command, *, client, scratch_root, emit):
    """Run the real engine and adapters; neither RPC errors nor parks retry tools."""
    if not isinstance(command, dict) or set(command) != _FIELDS:
        raise ValueError("invalid qualified HER command")
    root = Path(scratch_root)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError("an owned host scratch directory is required")
    config = unpack(command["config"])
    if not isinstance(config, HERv2Config):
        raise ValueError("a qualified HER configuration is required")
    for name in ("prompt", "request_id", "turn_id"):
        if not isinstance(command[name], str) or not command[name]:
            raise ValueError("a fixed HER request binding is required")
    factory = BrokeredProviderFactory(
        client, profile_config=command["profiles"], scratch_root=root,
        privacy_level=command["privacy_level"],
    )
    registry = BrokeredToolRegistry(client, definitions=command["tool_definitions"], scratch_root=root)
    audit = DurableAuditLog(primary_writer=_BrokerAuditWriter(client),
                            fallback_path=root / "audit-fallback.jsonl")
    usage = []

    async def stream(event):
        if not isinstance(event, StreamEvent):
            raise ValueError("invalid host stream event")
        await emit(event)

    provider = HashiStageProvider(backend_manager=factory, tool_registry=registry,
                                 on_stream_event=stream, audit_log=audit,
                                 workzone_ref=str(root),
                                 usage_observer=lambda item: usage.append(asdict(item)))
    provider.bind_provider_context(
        observer=lambda checkpoint: client.call("provider_checkpoint", {"checkpoint": dict(checkpoint)}),
        recovery=command["recovery"],
    )
    delivery = _AdapterDelivery(stream, allow_immediate_response=False)
    runtime = HERv2Runtime(
        config=config, provider=provider, ledger_store=LedgerStore(root / "ledgers"),
        audit_log=audit, delivery=delivery, workzone_ref=str(root),
        skills_catalogue=command["skills_catalogue"], pcm_input=command["pcm_input"],
    )
    try:
        result = await runtime.run_turn(
            command["prompt"], command["request_id"], turn_id=command["turn_id"],
            effort=command["effort"], request_content=command["request_content"],
            request_mode=command["request_mode"],
        )
        return {"state": "completed", "result": pack(result), "usage": usage,
                "token_usage": asdict(provider.usage), "cost_usd": provider.cost_usd,
                "tool_call_count": provider.tool_call_count, "tool_loop_count": provider.tool_loop_count}
    except PermissionPark as parked:
        from orchestrator.permissions.wire import encode

        return {"state": "parked", "control": encode(parked.result), "usage": usage,
                "token_usage": asdict(provider.usage), "cost_usd": provider.cost_usd,
                "tool_call_count": provider.tool_call_count, "tool_loop_count": provider.tool_loop_count}


def main():
    import base64
    import sys
    from multiprocessing.connection import PipeConnection
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from orchestrator.permissions.ipc_rpc import PermissionPipeClient
    from orchestrator.permissions.windows_ipc import JsonPipe
    from orchestrator.permissions.wire import encode

    if len(sys.argv) != 3 or not sys.argv[1].isdigit():
        raise SystemExit("fixed inherited handle and owned scratch are required")
    pipe = JsonPipe(PipeConnection(int(sys.argv[1])))
    try:
        initial = pipe.receive(timeout_s=10)
        if not isinstance(initial, dict) or set(initial) != {"endpoint", "public_key", "command"}:
            raise ValueError("invalid HER host bootstrap")
        client = PermissionPipeClient(
            initial["endpoint"],
            Ed25519PublicKey.from_public_bytes(base64.b64decode(initial["public_key"], validate=True)),
            methods=("provider_call", "tool_execute", "provider_checkpoint", "audit_record"),
        )

        async def emit(event):
            await asyncio.to_thread(pipe.send, {"type": "event", "event": encode(event)})

        response = asyncio.run(run_command(initial["command"], client=client,
                                            scratch_root=Path(sys.argv[2]), emit=emit))
        pipe.send({"type": "result", "result": response})
    except BaseException as error:
        # Trusted supervisor retains diagnostics; the agent cannot obtain
        # exception arguments, credentials or privileged environment data.
        pipe.send({"type": "host_error", "code": type(error).__name__})
        raise SystemExit(1) from None
    finally:
        pipe.close()


if __name__ == "__main__":
    main()
