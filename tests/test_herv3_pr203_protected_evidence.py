"""Protected HERV3 recovery evidence through the actual Run-bound tool channel."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from orchestrator.her_v2.backend_session import HerBackendSessionCoordinator
from orchestrator.her_v2.recovery_projection import bounded_prompt_projection
from orchestrator.permissions.authority import PermissionAuthority
from orchestrator.permissions.execution_channel import PermissionExecutionChannel
from orchestrator.permissions.her_callbacks import WorkerHerCallbacks
from orchestrator.permissions.models import RuntimeExecutionBinding, normalize_scope
from orchestrator.permissions.pao_run_fence import PaoRunFence
from orchestrator.permissions.registry import ManagedToolRegistry
from orchestrator.permissions.remote_registry import BrokeredToolRegistry
from tests.test_her_v2_control_plane import _run_claim
from tests.test_herv3_p0_checkpoint_recovery_bounds import _runtime_event
from tests.test_permission_execution_gate import Issuer, RecoverableFileSupervisor
from tests.test_permission_pao_fence import setup_run
from tools.registry import ToolRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("managed_workspace", [False, True])
async def test_protected_worker_pages_only_authorized_canonical_recovery_evidence(tmp_path, managed_workspace):
    store, session, accepted, fence, context = setup_run(tmp_path)
    home = tmp_path / "agent-home"
    home.mkdir()
    session_workspace = (tmp_path / "state" / "session_workspaces" / context.session_id / "generation_1"
                         if managed_workspace else home)
    coordinator = HerBackendSessionCoordinator(home / "backend_state" / "her_v2")
    shared = dict(
        session_id="private-her-session", sections=[], resources=[], instance_id=context.instance_id,
        agent_id=context.agent_id, owner_id=context.owner_id, hashi_conversation_id=context.session_id,
        context_generation=int(context.context_generation), workzone_identity=str(session_workspace),
    )

    def accept(request_id):
        encoded, _ = coordinator.prepare_transport(
            **shared, user_message="Inspect the important document", request_id=request_id,
            message_id="message-" + request_id,
        )
        return coordinator.accept(encoded)

    source = accept("source-request")
    document = home / "document.txt"
    document.write_text("start " + ("useful details " * 600) + "END-OF-EVIDENCE", encoding="utf-8")
    source_tools = ToolRegistry(allowed_tools=["file_read"], access_root=home, workspace_dir=home, secrets={})
    args = {"path": str(document)}
    completed = await source_tools.execute("file_read", args, tool_call_id="read-old")
    assert not completed.is_error and "END-OF-EVIDENCE" in completed.output
    coordinator.store.record_runtime_event(
        session_id=source.session_id, turn_id=source.turn_id,
        record=_runtime_event(source.turn_id, "receipt", "tool_receipt", receipt={
            "tool_call_id": "read-old", "tool_name": "file_read", "completed": True,
            "status": "completed", "output": completed.output,
        }),
    )
    uncertain_args = {"path": str(home / "uncertain.txt"), "content": "must not replay"}
    coordinator.store.record_runtime_event(
        session_id=source.session_id, turn_id=source.turn_id,
        record=_runtime_event(source.turn_id, "uncertain-intent", "tool_intent",
                              operation_id="uncertain-operation", invocation_id="uncertain-invocation",
                              tool_call_id="write-pending", tool_name="file_write", read_only=False),
    )
    coordinator.store.record_runtime_event(
        session_id=source.session_id, turn_id=source.turn_id,
        record=_runtime_event(source.turn_id, "uncertain-receipt", "tool_receipt",
            operation_id="uncertain-operation", tool_call_id="write-pending",
            receipt={"tool_call_id": "write-pending", "status": "unknown", "completed": False}),
    )
    coordinator.record_provider_context_checkpoint(source, {
        "provider": "openrouter-api", "model": "test-model", "stage": "direct",
        "boundary": "pending_tool_batch",
        "messages": [
            {"role": "user", "content": "Inspect the important document"},
            {"role": "assistant", "tool_calls": [
                {"id": "read-old", "type": "function",
                 "function": {"name": "file_read", "arguments": json.dumps(args)}},
                {"id": "write-pending", "type": "function",
                 "function": {"name": "file_write", "arguments": json.dumps(uncertain_args)}},
            ]},
            {"role": "tool", "tool_call_id": "read-old", "content": completed.output},
        ],
        "reasoning": [{"private": "MUST-NOT-LEAK"}],
        "producer_run_claim": _run_claim("run-source", "source-request"),
    })
    assert coordinator.store.reconcile_interrupted() == 1
    successor = accept(accepted.request_id)
    view = bounded_prompt_projection(coordinator.store.consume_recovery_context(
        session_id=successor.session_id, current_turn_id=successor.turn_id))
    context = replace(context, turn_id=successor.turn_id)
    binding = RuntimeExecutionBinding(context, accepted.request_id, fence, "INSTANCE:agent")

    identity = {
        "instance_id": context.instance_id, "agent_id": context.agent_id, "owner_id": context.owner_id,
        "hashi_conversation_id": context.session_id, "context_generation": int(context.context_generation),
        "workzone_identity": str(session_workspace),
    }
    adapter = SimpleNamespace(
        _session_coordinator=coordinator, config=SimpleNamespace(workspace_dir=home),
        _runtime_request_meta=lambda request_id: {"session_workspace": str(session_workspace)},
        _fixed_session_binding=lambda meta: dict(identity),
    )
    turn = SimpleNamespace(binding=binding, adapter=adapter, accepted=successor,
                           runtime=SimpleNamespace(pcm_input={"runtime_context": {"active_turn_recovery": view}}))
    callbacks = WorkerHerCallbacks(SimpleNamespace(
        service=SimpleNamespace(store=store), _turns={accepted.request_id: turn}))
    scope = normalize_scope("file", str(home), ("read", "create", "write"), cwd=home)
    current_authorization = {"allowed": True}
    authority = PermissionAuthority(tmp_path / "authority.sqlite", verifier=Issuer(),
        clock=lambda: 1000.0, monotonic=lambda: 1000.0,
        baseline_resolver=lambda _: (scope,) if current_authorization["allowed"] else ())
    authority.open_task(context)
    supervisor = RecoverableFileSupervisor(authority, home)
    registry = ManagedToolRegistry(
        allowed_tools=["request_diagnostics", "file_read", "file_write"], access_root=home, workspace_dir=home, secrets={},
        audit_context={"agent_name": context.agent_id, "safety_mode": "read_write"},
        permission_authority=authority, permission_context=context, permission_supervisor=supervisor,
        requestable_scopes=(scope,), baseline_scopes=(scope,), durable_wait=True,
    )
    channel = PermissionExecutionChannel(
        binding, authority=authority, run_guard=PaoRunFence(
            store, context=context, request_id=accepted.request_id, fencing_token=fence,
            worker_id=binding.worker_id), registry=registry, model_broker=SimpleNamespace(),
        checkpoint_writer=lambda *_: {"committed": True}, audit_writer=lambda *_: None,
        require_checkpoints=True,
    )
    channel._evidence_callback = getattr(callbacks, "recovery_evidence", None)
    permitted = {"value": True}
    channel._evidence_policy_validator = lambda _context, _scope: permitted["value"]

    class Client:
        async def call_async(self, method, arguments, **kwargs):
            assert method == "tool_execute"
            return await channel.tool_execute(**arguments)

    broker = BrokeredToolRegistry(Client(), definitions=registry.get_tool_definitions(), scratch_root=home)
    probe = await callbacks.recovery_evidence(binding=binding, arguments={
        "request_id": "source-request", "recovery_evidence_ref": view["tool_receipts"]["evidence_ref"],
        "recovery_item_id": "read-old", "offset": 0, "expected_digest": "",
    })
    assert probe["page"]["status"] == "available", probe
    assert registry.evaluate_admission("file_read", args, "probe") is None
    assert authority.evaluate(context, (scope,)) is True

    async def read_page(reference, call_id, **extra):
        arguments = {"request_id": "source-request", "recovery_evidence_ref": reference,
                     "recovery_item_id": "read-old", **extra}
        await channel.provider_checkpoint(checkpoint={"boundary": "pending_tool_batch", "messages": [
            {"role": "assistant", "tool_calls": [{"id": call_id, "type": "function", "function": {
                "name": "request_diagnostics", "arguments": json.dumps(arguments)}}]},
        ]})
        result = await broker.execute("request_diagnostics", arguments, tool_call_id=call_id)
        assert not result.is_error, result.output
        return json.loads(result.output)["recovery_evidence"]

    async def collect(reference, prefix):
        pages, offset, digest = [], 0, ""
        while True:
            page = await read_page(reference, prefix + str(offset), offset=offset, expected_digest=digest)
            assert page["status"] == "available", page
            pages.append(page["content"])
            digest = page["digest"]
            if page["next_offset"] is None:
                return json.loads("".join(pages))
            offset = page["next_offset"]

    receipt = await collect(view["tool_receipts"]["evidence_ref"], "receipt-")
    result = await collect(view["provider_context_checkpoint"]["items_evidence_ref"], "result-")
    assert receipt["output"] == completed.output
    assert result["content"] == completed.output
    assert "MUST-NOT-LEAK" not in json.dumps([receipt, result])
    bad = await read_page(view["tool_receipts"]["evidence_ref"].replace(source.turn_id, "other"),
                          "wrong-turn")
    assert bad["status"] == "not available"
    for field, wrong in (("owner_id", "another-owner"), ("agent_id", "another-agent"),
                         ("hashi_conversation_id", "another-session"), ("context_generation", 2),
                         ("workzone_identity", str(tmp_path / "outside-workzone"))):
        old = identity[field]
        identity[field] = wrong
        denied = await read_page(reference=view["provider_context_checkpoint"]["items_evidence_ref"],
                                 call_id="identity-" + field)
        assert denied["status"] == "not available"
        identity[field] = old
    current_authorization["allowed"] = False
    denied = await read_page(view["provider_context_checkpoint"]["items_evidence_ref"], "revoked-scope")
    assert denied["status"] == "not available"
    current_authorization["allowed"] = True
    permitted["value"] = False
    revoked = await read_page(view["provider_context_checkpoint"]["items_evidence_ref"], "revoked")
    assert revoked["status"] == "not available"

    # The new execution can investigate the source's uncertain effect without
    # executing it or changing its canonical unknown status.
    permitted["value"] = True
    unknown_page = await read_page(view["side_effects"]["evidence_ref"], "unknown-read",
                                   recovery_item_id="write-pending")
    assert unknown_page["status"] == "available"
    assert json.loads(unknown_page["content"])["state"] == "unknown"
    assert not (home / "uncertain.txt").exists()

    # Exercise the real restricted HERV3 host and its brokered Tool registry,
    # not just a direct database query. The model transport is an injected
    # deterministic response; the PAO Run, scopes and HER SQLite are real.
    import asyncio
    from adapters.openrouter_api import _APIResult
    from orchestrator.permissions.host_protocol import pack, unpack
    from orchestrator.permissions.model_broker import api_result_to_wire
    from orchestrator.permissions.restricted_her_host import run_command
    from tests.test_permission_restricted_her import command

    permitted["value"] = True
    reference = view["provider_context_checkpoint"]["items_evidence_ref"]
    tool_arguments = {"request_id": "source-request", "recovery_evidence_ref": reference,
                      "recovery_item_id": "read-old"}
    model_wire = []

    class Models:
        async def provider_call(self, **call):
            messages = call["payload"]["messages"]
            model_wire.append(messages)
            if len(model_wire) == 1:
                tool_call = {"id": "host-read", "type": "function", "function": {
                    "name": "request_diagnostics", "arguments": json.dumps(tool_arguments)}}
                return api_result_to_wire(_APIResult("", [tool_call], "tool_calls"))
            assert any(row.get("tool_call_id") == "host-read"
                       and "useful details" in row.get("content", "") for row in messages)
            return api_result_to_wire(_APIResult("Recovered the evidence.", None, "stop"))

    channel._models = Models()
    channel._checkpoint = lambda bound, checkpoint: coordinator.record_provider_context_checkpoint(
        successor, {**checkpoint, "producer_run_claim": _run_claim(bound.context.run_id, bound.request_id)})
    loop = asyncio.get_running_loop()

    class HostConnection:
        def call(self, method, arguments):
            if method == "runtime_port" and arguments["operation"] == "manifest":
                return pack({"operations": [], "allow_immediate_response": False, "workzone_ref": str(home)})
            future = asyncio.run_coroutine_threadsafe(getattr(channel, method)(**arguments), loop)
            return future.result(timeout=20)

        async def call_async(self, method, arguments, **kwargs):
            future = asyncio.run_coroutine_threadsafe(getattr(channel, method)(**arguments), loop)
            return await asyncio.wrap_future(future)

    host_root = tmp_path / "host-scratch"
    host_root.mkdir()
    payload = command(registry)
    payload.update(request_id=binding.request_id, turn_id=binding.context.turn_id,
                   prompt="Use the authorized recovery evidence to finish.",
                   pcm_input={"current_request": "Use the authorized recovery evidence to finish.",
                              "sections": [], "history": [],
                              "runtime_context": {"active_turn_recovery": view}})
    async def emit(_event):
        pass
    outcome = await asyncio.to_thread(lambda: asyncio.run(run_command(
        payload, client=HostConnection(), scratch_root=host_root, emit=emit,
        execution_epoch=context.execution_epoch)))
    assert outcome["state"] == "completed", outcome
    assert unpack(outcome["result"]).text == "Recovered the evidence."
    assert len(model_wire) == 2
# HASHI-Fingerprint: hfp_22f9cdc2ac9a495db9401f7eaea60dff
