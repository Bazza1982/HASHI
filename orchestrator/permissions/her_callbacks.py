"""Narrow HERV3 persistence callbacks owned by the actual Function Worker.

Only the configured Safety Windows identity can connect. A host cannot choose
a writer, filename, producer claim or execution owner.
"""
from __future__ import annotations

import asyncio
import base64
import copy
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.ipc_rpc import PermissionPipeClient
from orchestrator.permissions.ipc_server import PermissionEndpoint
from orchestrator.permissions.pao_run_fence import PaoRunFence
from orchestrator.permissions.windows_ipc import WindowsPipeListener, PipePeerPolicy, current_process_user_sid
from orchestrator.permissions.wire import PermissionServiceRouter


class WorkerHerCallbacks:
    METHODS = frozenset({"provider_checkpoint", "audit_record", "recovery_context"})

    def __init__(self, controller):
        self.controller = controller

    def _turn(self, binding):
        turn = self.controller._turns.get(binding.request_id)
        if turn is None or turn.binding != binding or turn.adapter is None or turn.accepted is None:
            raise PermissionDenied("the HER callback no longer owns this Turn")
        guard = PaoRunFence(
            self.controller.service.store, context=binding.context, request_id=binding.request_id,
            fencing_token=binding.fencing_token, worker_id=binding.worker_id,
        )
        with guard(binding.context):
            pass
        return turn, guard

    async def recovery_context(self, *, binding):
        turn, guard = self._turn(binding)
        # Only open_turn can populate this field after the PAO resume ticket
        # has adopted the exact private context. The host supplies no snapshot.
        result = copy.deepcopy(turn.recovery)
        with guard(binding.context):
            pass
        return result

    async def provider_checkpoint(self, *, binding, checkpoint):
        turn, guard = self._turn(binding)
        if not isinstance(checkpoint, dict) or "producer_run_claim" in checkpoint:
            raise PermissionDenied("a host cannot choose checkpoint provenance")
        adapter = turn.adapter
        producer = adapter._canonical_pao_run_claim(adapter._runtime_request_meta(binding.request_id))
        if producer is None:
            raise PermissionDenied("canonical checkpoint producer is unavailable")
        result = await asyncio.to_thread(
            adapter._session_coordinator.record_provider_context_checkpoint, turn.accepted,
            {**checkpoint, "producer_run_claim": producer},
        )
        # No RPC/slow storage I/O is performed inside a PAO transaction. The
        # private HER owner also CASes execution ownership. A late checkpoint
        # after cancellation cannot acknowledge authority for another effect.
        with guard(binding.context):
            pass
        return result

    async def audit_record(self, *, binding, record):
        turn, guard = self._turn(binding)
        if (not isinstance(record, dict) or record.get("turn_id") != binding.context.turn_id
                or record.get("request_ref") != "hashi-request:" + binding.request_id):
            raise PermissionDenied("audit entry is outside this Turn")
        result = await asyncio.to_thread(turn.adapter._audit_log.append, record)
        with guard(binding.context):
            pass
        return result


async def start_callbacks(controller, safety_sid):
    import os
    key = Ed25519PrivateKey.generate()
    owner = WorkerHerCallbacks(controller)
    address = "\\\\.\\pipe\\LOCAL\\HASHI.Permissions.worker." + uuid4().hex
    endpoint = PermissionEndpoint(
        WindowsPipeListener(address, service_sid=current_process_user_sid(), peer_policy=PipePeerPolicy(safety_sid)),
        PermissionServiceRouter(owner, methods=owner.METHODS), signing_key=key,
        service_generation=uuid4().hex,
    )
    await endpoint.start()
    public = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )).decode("ascii")
    return endpoint, {"endpoint": address, "public_key": public, "process_id": os.getpid()}


class CallbackClient:
    def __init__(self, descriptor):
        if (not isinstance(descriptor, dict)
                or set(descriptor) != {"endpoint", "public_key", "process_id"}):
            raise PermissionDenied("invalid private HER callback descriptor")
        self.descriptor = descriptor
        self.client = PermissionPipeClient(
            descriptor["endpoint"], Ed25519PublicKey.from_public_bytes(base64.b64decode(
                descriptor["public_key"], validate=True,
            )), methods=WorkerHerCallbacks.METHODS,
        )

    async def recovery(self, binding):
        return await self.client.call_async("recovery_context", {"binding": binding})

    async def checkpoint(self, binding, checkpoint):
        return await self.client.call_async("provider_checkpoint", {"binding": binding, "checkpoint": checkpoint})

    async def audit(self, binding, record):
        return await self.client.call_async("audit_record", {"binding": binding, "record": record})
