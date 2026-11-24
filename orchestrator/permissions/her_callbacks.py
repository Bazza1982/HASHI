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
    METHODS = frozenset({"provider_checkpoint", "audit_record", "recovery_context", "recovery_evidence", "runtime_port"})

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

    async def runtime_port(self, *, binding, operation, arguments):
        from orchestrator.permissions.runtime_ports import invoke

        turn, guard = self._turn(binding)
        result = await invoke(turn, operation, arguments)
        with guard(binding.context):
            pass
        return result

    async def recovery_context(self, *, binding):
        turn, guard = self._turn(binding)
        # Only the owning Worker supplies an adopted permission continuation
        # or a successor context verified by the canonical HER store. The host
        # cannot choose either the snapshot or its continuation semantics.
        result = copy.deepcopy(turn.recovery)
        continuation = getattr(turn, "successor_continuation", None)
        if continuation is not None:
            if not isinstance(result, dict):
                raise PermissionDenied("successor provider context is unavailable")
            result["_herv3_continuation"] = copy.deepcopy(continuation)
        with guard(binding.context):
            pass
        return result

    async def recovery_evidence(self, *, binding, arguments):
        """Return one canonical page and its original Tool call to the PAO service.

        This private callback is reachable only by the authenticated Safety
        service. It never returns the source arguments to the restricted host.
        The service must check current Tool admission, scopes and policy before
        releasing the page to the Run-bound execution channel.
        """
        import json
        from pathlib import Path
        from collections.abc import Mapping
        from orchestrator.request_diagnostics import read_scoped_recovery_evidence

        turn, guard = self._turn(binding)
        unavailable = {"page": {"status": "not available"}}
        allowed = {"request_id", "recovery_evidence_ref", "recovery_item_id", "offset", "expected_digest"}
        if (not isinstance(arguments, dict) or set(arguments) - allowed
                or not all(isinstance(arguments.get(k), str) and arguments[k]
                           for k in ("request_id", "recovery_evidence_ref"))):
            return unavailable

        def read_page():
            adapter, accepted = turn.adapter, turn.accepted
            context = binding.context
            if accepted.turn_id != context.turn_id:
                return unavailable
            meta = adapter._runtime_request_meta(binding.request_id)
            identity = adapter._fixed_session_binding(meta)
            if (identity.get("instance_id", "").casefold() != context.instance_id.casefold()
                    or identity.get("agent_id", "").casefold() != context.agent_id.casefold()
                    or identity.get("owner_id") != context.owner_id
                    or identity.get("hashi_conversation_id") != context.session_id
                    or str(identity.get("context_generation")) != context.context_generation):
                return unavailable
            pcm = getattr(turn.runtime, "pcm_input", None)
            runtime = pcm.get("runtime_context") if isinstance(pcm, Mapping) else None
            if not isinstance(runtime, Mapping):
                return unavailable
            reference = arguments["recovery_evidence_ref"]
            view, source_turn, view_kind = None, "", ""
            for key in ("active_turn_recovery", "settled_checkpoint"):
                candidate = runtime.get(key)
                if not isinstance(candidate, Mapping):
                    continue
                found = set()
                for field in ("tool_receipts", "side_effects", "provider_context_checkpoint"):
                    item = candidate.get(field)
                    if isinstance(item, Mapping):
                        found.update(str(item.get(name) or "") for name in
                                     ("evidence_ref", "canonical_evidence_ref", "items_evidence_ref"))
                if reference in found and candidate.get("source_request_id") == arguments["request_id"]:
                    view = candidate
                    view_kind = key
                    source_turn = str(candidate.get("interrupted_turn_id") or candidate.get("settled_through_turn_id") or "")
                    break
            if not view or not source_turn or view.get("session_id") != accepted.session_id:
                return unavailable
            store = adapter._session_coordinator.store
            if view_kind == "active_turn_recovery":
                current = store.active_turn_recovery(accepted.session_id, accepted.turn_id)
                if not current or current.get("recovery_source_turn_id") != source_turn:
                    return unavailable
            else:
                settled = store.settled_checkpoint(accepted.session_id)
                payload = settled.get("payload") if isinstance(settled, Mapping) else None
                from orchestrator.her_v2.session_store import _digest

                if (not isinstance(payload, Mapping) or payload.get("settled_through_turn_id") != source_turn
                        or payload.get("source_request_id") != arguments["request_id"]
                        or _digest(payload) != settled.get("payload_digest")):
                    return unavailable
            source = store.active_turn_recovery(accepted.session_id, source_turn)
            if not source or not store._verified_provider_context_claim(
                    source, source_request_id=arguments["request_id"]):
                return unavailable
            checkpoint = store.provider_context_for_turn(accepted.session_id, source_turn)
            if not isinstance(checkpoint, Mapping):
                return unavailable
            item_id = arguments["recovery_item_id"]
            if not item_id:
                # Scoped discovery: the caller holds the canonical reference but
                # not an item identity. Re-run the same binding, claim and
                # workzone checks, then return only authorized item identities.
                agent_home = Path(adapter.config.workspace_dir)
                expected = agent_home / "backend_state" / "her_v2" / "fixed_sessions.sqlite3"
                if Path(store.path).resolve() != expected.resolve():
                    return unavailable
                scope = {"agent_name": context.agent_id, "owner_id": context.owner_id,
                         "instance_id": context.instance_id, "hashi_session_id": context.session_id,
                         "context_generation": int(context.context_generation),
                         "session_workspace": identity.get("workzone_identity"),
                         "_herv3_recovery_scopes": [{"session_id": accepted.session_id,
                             "turn_id": source_turn, "request_id": arguments["request_id"]}]}
                page = read_scoped_recovery_evidence(
                    agent_home=agent_home, request_id=arguments["request_id"], evidence_ref=reference,
                    item_id="", offset=arguments.get("offset", 0),
                    expected_digest=arguments.get("expected_digest", ""), scope=scope)
                if page.get("status") != "available":
                    return unavailable
                return {"page": page}
            call_id = item_id
            if not reference.startswith("her_provider_context_items:"):
                field = "tool_receipts" if ":tool_receipts:" in reference else "side_effects"
                matches = [item for item in source.get(field, []) if isinstance(item, Mapping)
                           and any(str(item.get(name) or "") == item_id for name in
                                   ("operation_id", "tool_call_id", "invocation_id", "evidence_ref"))]
                if len(matches) != 1:
                    return unavailable
                call_id = str(matches[0].get("tool_call_id") or item_id)
            calls = []
            for message in checkpoint.get("messages", []):
                if not isinstance(message, Mapping) or message.get("role") != "assistant":
                    continue
                for call in message.get("tool_calls") or []:
                    if isinstance(call, Mapping) and call.get("id") == call_id:
                        calls.append(call)
            if len(calls) != 1 or not isinstance(calls[0].get("function"), Mapping):
                return unavailable
            function = calls[0]["function"]
            tool_name, tool_args = function.get("name"), function.get("arguments")
            if isinstance(tool_args, str):
                try:
                    tool_args = json.loads(tool_args)
                except (ValueError, TypeError):
                    return unavailable
            if not isinstance(tool_name, str) or not isinstance(tool_args, dict):
                return unavailable
            agent_home = Path(adapter.config.workspace_dir)
            expected = agent_home / "backend_state" / "her_v2" / "fixed_sessions.sqlite3"
            if Path(store.path).resolve() != expected.resolve():
                return unavailable
            scope = {"agent_name": context.agent_id, "owner_id": context.owner_id,
                     "instance_id": context.instance_id, "hashi_session_id": context.session_id,
                     "context_generation": int(context.context_generation),
                     "session_workspace": identity.get("workzone_identity"),
                     "_herv3_recovery_scopes": [{"session_id": accepted.session_id,
                         "turn_id": source_turn, "request_id": arguments["request_id"]}]}
            page = read_scoped_recovery_evidence(
                agent_home=agent_home, request_id=arguments["request_id"], evidence_ref=reference,
                item_id=item_id, offset=arguments.get("offset", 0),
                expected_digest=arguments.get("expected_digest", ""), scope=scope)
            if page.get("status") != "available":
                return unavailable
            return {"page": page, "tool_name": tool_name, "tool_arguments": tool_args,
                    "source_workzone": identity.get("workzone_identity")}

        try:
            result = await asyncio.to_thread(read_page)
        except (AttributeError, OSError, TypeError, ValueError, RuntimeError):
            result = unavailable
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
        if record.get("format") != "her-v2-audit-v1":
            raise PermissionDenied("unsupported bound runtime audit format")
        # The original owner supplies its envelope. Its keyword append keeps
        # canonical-before-projection persistence, redaction and deduplication.
        facts = {key: value for key, value in record.items()
                 if key not in {"format", "recorded_at"}}
        result = await asyncio.to_thread(turn.adapter._audit_log.append, **facts)
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

    async def runtime_port(self, binding, operation, arguments):
        return await self.client.call_async("runtime_port", {
            "binding": binding, "operation": operation, "arguments": arguments,
        })

    async def recovery(self, binding):
        return await self.client.call_async("recovery_context", {"binding": binding})

    async def recovery_evidence(self, binding, arguments):
        return await self.client.call_async("recovery_evidence", {"binding": binding, "arguments": arguments})

    async def checkpoint(self, binding, checkpoint):
        return await self.client.call_async("provider_checkpoint", {"binding": binding, "checkpoint": checkpoint})

    async def audit(self, binding, record):
        return await self.client.call_async("audit_record", {"binding": binding, "record": record})
