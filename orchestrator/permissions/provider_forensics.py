"""Private provider evidence crosses the existing Run-bound runtime port."""
from __future__ import annotations

import asyncio

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.host_protocol import pack, unpack

_KINDS = {
    "wire": ("hashi-provider-wire-v1", "provider_wire_evidence"),
    "invalid_tool": ("hashi-provider-tool-forensic-v1", "provider_tool_forensic"),
}


class BrokeredProviderForensics:
    def __init__(self, client):
        self.client = client

    def __call__(self, kind, record):
        if kind not in _KINDS:
            raise PermissionError("unknown private provider evidence kind")
        result = unpack(self.client.call("runtime_port", {
            "operation": "provider_forensic",
            "arguments": {"args": [], "kwargs": {"kind": pack(kind), "record": pack(record)}},
        }))
        if not isinstance(result, str) or not result.startswith("canonical-audit:"):
            raise OSError("canonical provider evidence acknowledgement is unavailable")
        return result


def persist_provider_forensic_sync(turn, *, kind, record):
    selected = _KINDS.get(kind) if isinstance(kind, str) else None
    if (selected is None or not isinstance(record, dict)
            or record.get("format") != selected[0]):
        raise PermissionDenied("unknown private provider evidence")
    adapter = getattr(turn, "adapter", None)
    runtime = getattr(getattr(adapter, "config", None), "_hashi_runtime", None)
    canonical = getattr(runtime, "canonical_audit", None)
    writer = getattr(canonical, "record", None)
    if not callable(writer):
        raise PermissionDenied("the original canonical provider evidence owner is unavailable")
    # The enclosing callback checks the live Run before and after this call.
    # Records are evidence from the host, never an authority or a file path.
    binding = turn.binding
    identifier = writer(
        selected[1], record, request_id=binding.request_id,
        provenance={"source": "restricted-her-host", "turn_id": binding.context.turn_id,
                    "execution_epoch": binding.context.execution_epoch},
    )
    flush = getattr(canonical, "flush", None)
    if callable(flush):
        flush()
    if not isinstance(identifier, str) or not identifier or len(identifier) > 512:
        raise OSError("canonical provider evidence persistence is unverified")
    return "canonical-audit:" + identifier


async def persist_provider_forensic(turn, *, kind, record):
    return await asyncio.to_thread(persist_provider_forensic_sync, turn, kind=kind, record=record)
# HASHI-Fingerprint: hfp_b81791ec22204f3fb12f1d5f67ae70ab
