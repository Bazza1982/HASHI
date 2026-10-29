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
    client.idle_timeout_s = float(config.user_idle_timeout_s)
    for name in ("prompt", "request_id", "turn_id"):
        if not isinstance(command[name], str) or not command[name]:
            raise ValueError("a fixed HER request binding is required")
    factory = BrokeredProviderFactory(
        client, profile_config=command["profiles"], scratch_root=root,
        privacy_level=command["privacy_level"],
    )
    registry = BrokeredToolRegistry(client, definitions=command["tool_definitions"], scratch_root=root)
    from orchestrator.permissions.runtime_ports import HostRuntimePorts

    ports = HostRuntimePorts(client)
    factory._artifact_ports = ports if ports.manifest["media_ports"] else None
    services = ports.services()
    audit = DurableAuditLog(primary_writer=_BrokerAuditWriter(client),
                            fallback_path=root / "audit-fallback.jsonl")
    usage = []

    async def stream(event):
        if not isinstance(event, StreamEvent):
            raise ValueError("invalid host stream event")
        if event.kind == "tool_activity" and services is not None:
            services.observe_tool_activity(event.metadata or {})
        await emit(event)

    registry._event_sink = stream
    provider = HashiStageProvider(backend_manager=factory, tool_registry=registry,
                                 on_stream_event=stream, audit_log=audit,
                                 workzone_ref=str(root),
                                 usage_observer=lambda item: usage.append(asdict(item)))
    from orchestrator.permissions.media_bridge import HostMedia, bind_host_media

    media = HostMedia(ports, root) if ports.manifest["media_ports"] else None
    if media is not None:
        command = dict(command, request_content=await media.accepted())
        await bind_host_media(provider, media)
    else:
        from orchestrator.multimodal_contract import attachment_manifest
        if attachment_manifest(command["request_content"]):
            raise ValueError("the original accepted-media port is unavailable")
    provider.bind_provider_context(
        observer=lambda checkpoint: client.call("provider_checkpoint", {"checkpoint": dict(checkpoint)}),
        recovery=command["recovery"],
    )
    provider.bind_commentary_port(services or ports.port("commentary"))
    provider.bind_turn_services(services)
    delivery = _AdapterDelivery(stream, allow_immediate_response=ports.manifest["allow_immediate_response"])
    from adapters.her_v2 import _ExecutionStageCompactionProvider, _HChatDraftToolProvider

    execution_provider = provider
    if "execution_compaction" in ports.manifest["provider_features"]:
        execution_provider = _ExecutionStageCompactionProvider(provider, lambda: ports._sync("execution_start"))
    if "hchat_draft" in ports.manifest["provider_features"]:
        execution_provider = _HChatDraftToolProvider(execution_provider)
    runtime = HERv2Runtime(
        config=config, provider=execution_provider, ledger_store=LedgerStore(root / "ledgers"),
        audit_log=audit, delivery=delivery, workzone_ref=ports.manifest["workzone_ref"],
        commentary=services or ports.port("commentary"), required_persona=ports.port("required_persona"),
        final_style=ports.port("final_style"), habits=ports.port("habits"), meditation=ports.port("meditation"),
        turn_services=services,
        skills_catalogue=command["skills_catalogue"], pcm_input=command["pcm_input"],
    )
    try:
        result = await runtime.run_turn(
            command["prompt"], command["request_id"], turn_id=command["turn_id"],
            effort=command["effort"], request_content=command["request_content"],
            request_mode=command["request_mode"],
        )
        # The existing runtime schedules only a durable Meditation enqueue.
        # Let that enqueue reach its original owner before the disposable host
        # exits; its background model work remains separately owned.
        if runtime._background_tasks:
            await asyncio.gather(*tuple(runtime._background_tasks))
        return {"state": "completed", "result": pack(result), "usage": usage,
                "token_usage": asdict(provider.usage), "cost_usd": provider.cost_usd,
                "tool_call_count": provider.tool_call_count, "tool_loop_count": provider.tool_loop_count}
    except PermissionPark as parked:
        from orchestrator.permissions.wire import encode

        return {"state": "parked", "control": encode(parked.result), "usage": usage,
                "token_usage": asdict(provider.usage), "cost_usd": provider.cost_usd,
                "tool_call_count": provider.tool_call_count, "tool_loop_count": provider.tool_loop_count}
    finally:
        if media is not None:
            try:
                await media.close()
            finally:
                await ports._async("media_cleanup")


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
            methods=("provider_call", "tool_execute", "provider_checkpoint", "audit_record", "runtime_port"),
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
