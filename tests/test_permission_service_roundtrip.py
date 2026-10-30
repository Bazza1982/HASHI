"""A bounded real private-IPC round trip; model transport/native launcher are injected.

No HASHI instance, HTTP server, account or business process is started.
"""
import asyncio
import base64
from datetime import datetime, timezone
import sys
import os
from types import SimpleNamespace
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from adapters.her_v2_provider import HashiStageProvider
from adapters.openrouter_api import _APIResult
from orchestrator.enterprise import IdentityService
from orchestrator.her_v2.audit import DurableAuditLog
from orchestrator.her_v2.backend_session import HerBackendSessionCoordinator
from orchestrator.her_v2.ledger import LedgerStore
from orchestrator.her_v2.runtime import HERv2Runtime
from orchestrator.permissions.authority import PermissionAuthority
from orchestrator.permissions.clients import PAO_METHODS, CONTROL_METHODS, PermissionRunClient, PermissionControlClient
from orchestrator.permissions.continuation import PermissionPark
from orchestrator.permissions.execution_channel import PermissionExecutionChannel, EXECUTION_METHODS
from orchestrator.permissions.ipc_rpc import PermissionPipeClient
from orchestrator.permissions.ipc_server import PermissionEndpoint
from orchestrator.permissions.model_broker import SelectedModelBroker
from orchestrator.permissions.model_receipts import ModelReceiptJournal
from orchestrator.permissions.models import PermissionSettings, PermissionLevel, normalize_scope
from orchestrator.permissions.native_factory import InstalledExecution, _NoModelTransport
from orchestrator.permissions.pao_run_fence import PaoRunFence
from orchestrator.permissions.receipt_issuer import SupervisorReceiptIssuer
from orchestrator.permissions.registry import ManagedToolRegistry
from orchestrator.permissions.run_service import ManagedRunPolicy
from orchestrator.permissions.service import PermissionServiceApplication
from orchestrator.permissions.signed_receipts import SignedReceiptVerifier
from orchestrator.permissions.windows_ipc import WindowsPipeListener, PipePeerPolicy, current_process_user_sid
from orchestrator.permissions.wire import PermissionServiceRouter
from orchestrator.runtime_common import QueuedRequest
from tests.test_permission_execution_gate import TestFileSupervisor
from tests.test_permission_pao_fence import setup_run
from tests.test_permission_restricted_her import command
from tests.test_permission_provider_park import tool


@pytest.mark.skipif(sys.platform != "win32", reason="native private pipe")
@pytest.mark.asyncio
async def test_sdk_service_her_human_approval_new_epoch_and_single_effect(tmp_path):
    store, session, accepted, fence, context = setup_run(tmp_path)
    signing, approval, stop_key = (Ed25519PrivateKey.generate() for _ in range(3))
    observer = SupervisorReceiptIssuer(key_id="test-observer", signing_key=stop_key)
    authority = PermissionAuthority(tmp_path / "authority.sqlite", verifier=SignedReceiptVerifier(
        {"approval": approval.public_key().public_bytes_raw()},
        trusted_supervisor_keys={observer.key_id: observer.public_key},
    ))
    ceiling = normalize_scope("file", str(tmp_path), ("create", "write"), cwd=tmp_path)
    settings = PermissionSettings(PermissionLevel.L1)
    policy = ManagedRunPolicy(settings, 1, tmp_path, (ceiling,), ("file_write",), {})
    identity = IdentityService.from_path(tmp_path / "identities.sqlite")
    identity.create_organization(org_id="fixture", name="Fixture")
    identity.create_user(org_id="fixture", email="owner@example.test", display_name="Fixture",
                         password="local-test-only", user_id="human")
    physical_calls, file_calls, meters = [], [], []
    journal = ModelReceiptJournal(tmp_path / "models.sqlite")
    sid = current_process_user_sid()
    public = base64.b64encode(signing.public_key().public_bytes_raw()).decode()
    model_profile = {"engine": "deepseek-api", "model": "deepseek-v4-pro"}

    class Driver:
        def _request_headers(self):
            return {"Authorization": "fixture-secret"}
        async def _stream_api_once(self, payload, headers, callback):
            physical_calls.append(payload["messages"])
            if len(physical_calls) == 1:
                return _APIResult("", [tool("save-once", "report.txt")], "tool_calls",
                                  prompt_tokens=12, completion_tokens=2, cost_usd=.01, provider_response_id="first")
            assert any(item.get("tool_call_id") == "save-once" for item in payload["messages"])
            return _APIResult("Report saved.", None, "stop", prompt_tokens=8, completion_tokens=2,
                              cost_usd=.02, provider_response_id="second")
        _call_api_once = _stream_api_once
        async def shutdown(self):
            return True

    class Models:
        async def create(self, binding, config, guard):
            broker = SelectedModelBroker(authority, binding, drivers={(model_profile["engine"], model_profile["model"]): Driver()},
                                         run_guard=guard, receipt_store=journal)
            return broker, {"profiles": [model_profile], "privacy_level": 1}

    class FileSupervisor(TestFileSupervisor):
        async def execute(self, *args):
            file_calls.append(args[1])
            return await super().execute(*args)
        async def close(self):
            return True

    class Factory:
        def __init__(self):
            self.public_key, self.runner, self.model_factory = public, None, Models()
            self.owned = {}
        def executor_id(self, policy):
            return observer.key_id
        async def create(self, binding, policy):
            guard = PaoRunFence(store, context=binding.context, request_id=binding.request_id,
                                fencing_token=binding.fencing_token, worker_id=binding.worker_id)
            supervisor = FileSupervisor(authority, tmp_path)
            supervisor._confirm_stopped = lambda ledger, ctx, claim: ledger.confirm_resource_stopped(
                ctx, claim, receipt_reference=observer.issue_stop(ctx, claim, {"test_file_closed": True}))
            registry = ManagedToolRegistry(
                allowed_tools=["file_write"], access_root=tmp_path, workspace_dir=tmp_path, secrets={},
                audit_context={"agent_name": context.agent_id, "safety_mode": "read_write"},
                permission_authority=authority, permission_context=binding.context, permission_supervisor=supervisor,
                requestable_scopes=(ceiling,), durable_wait=True,
            )
            channel = PermissionExecutionChannel(binding, authority=authority, run_guard=guard, registry=registry,
                                                 model_broker=_NoModelTransport(), checkpoint_writer=lambda *a: None,
                                                 audit_writer=lambda *a: None)
            address = r"\\.\pipe\LOCAL\HASHI.Permissions.completion." + uuid4().hex
            endpoint = PermissionEndpoint(WindowsPipeListener(address, service_sid=sid, peer_policy=PipePeerPolicy(sid)),
                                          PermissionServiceRouter(channel, methods=EXECUTION_METHODS),
                                          signing_key=signing, service_generation=authority._service_generation)
            result = InstalledExecution(self, binding, policy, channel, endpoint, address)
            self.owned[binding.context.execution_epoch] = result
            await endpoint.start()
            return result
        async def retire(self, binding):
            result = self.owned.get(binding.context.execution_epoch)
            if result is not None:
                await result.close()
                self.owned.pop(binding.context.execution_epoch)
            return not authority.unsettled_resources(binding.context)

    factory = Factory()
    service = PermissionServiceApplication(authority, store, identity, policy_resolver=lambda *args: policy,
                                            executor_factory=factory, key_id="approval", signing_key=approval,
                                            owner_bindings={"human": context.owner_id}, levels=("L1",))
    endpoints = []
    for router in (service.pao_router, service.control_router):
        address = r"\\.\pipe\LOCAL\HASHI.Permissions.completion." + uuid4().hex
        endpoints.append(PermissionEndpoint(WindowsPipeListener(address, service_sid=sid, peer_policy=PipePeerPolicy(sid)),
                                            router, signing_key=signing, service_generation=service.generation))
    try:
        await service.start(tuple(endpoints))
        addresses = [endpoint._listener.name for endpoint in endpoints]
        pao_client = PermissionPipeClient(addresses[0], signing.public_key(), methods=PAO_METHODS, idle_timeout_s=10)
        control = PermissionControlClient(PermissionPipeClient(addresses[1], signing.public_key(),
                                                              methods=CONTROL_METHODS, idle_timeout_s=10))
        client = PermissionRunClient(pao_client, store=store, workspace_dir=tmp_path, settings=settings)
        coordinator = HerBackendSessionCoordinator(tmp_path / "trusted-her", execution_owner={
            "owner_id": "test-her-owner", "pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat(),
        })
        encoded, _ = coordinator.prepare_transport(
            session_id="her", sections=[], resources=[], user_message="Write report",
            request_id=accepted.request_id, message_id=accepted.message_id, instance_id=context.instance_id,
            agent_id=context.agent_id, owner_id=context.owner_id, hashi_conversation_id=context.session_id,
            context_generation=1, workzone_identity="workspace",
        )
        her_turn = coordinator.accept(encoded)
        arguments = dict(request_id=accepted.request_id, run_id=accepted.run_id, owner_id=context.owner_id,
                         agent_id=context.agent_id, session_id=context.session_id, context_generation=1,
                         fencing_token=fence, worker_id="INSTANCE:agent", worker_generation="first",
                         turn_id=her_turn.turn_id, backend_id="her-v2")
        config_holder = SimpleNamespace(get_tool_definitions=lambda: [])
        from orchestrator.permissions.host_protocol import unpack
        config = unpack(command(config_holder)["config"])
        audit = DurableAuditLog(primary_path=tmp_path / "audit.jsonl", fallback_path=tmp_path / "audit-fallback.jsonl")

        async def worker_run(binding, recovery=None):
            executor = client.execution_registry(binding)
            provider = HashiStageProvider(backend_manager=None, tool_registry=executor.registry,
                                           audit_log=audit, workzone_ref=str(tmp_path), usage_observer=meters.append)
            def checkpoint(value):
                run = store.get_run(binding.context.run_id, owner_id=binding.context.owner_id)
                producer = dict(run_id=run["run_id"], request_id=run["request_id"], attempt=run["attempt"],
                                attempt_id=f"{run['run_id']}:attempt:{run['attempt']}", state_revision=run["fencing_token"],
                                input_digest=run["request_digest"], state=run["state"], parent_run_id=run["parent_run_id"])
                return coordinator.record_provider_context_checkpoint(her_turn, {**value, "producer_run_claim": producer})
            provider.bind_provider_context(observer=checkpoint, recovery=recovery)
            executor.bind_provider(provider)
            runtime = HERv2Runtime(config=config, provider=provider, ledger_store=LedgerStore(tmp_path / ("engine-" + binding.context.execution_epoch)),
                                  audit_log=audit, workzone_ref=str(tmp_path))
            try:
                return await executor.run_her(runtime, "Write report", accepted.request_id, binding=binding)
            except PermissionPark as park:
                return park

        binding = await client.open_execution(**arguments)
        first = await asyncio.to_thread(lambda: asyncio.run(worker_run(binding)))
        assert isinstance(first, PermissionPark) and len(physical_calls) == 1 and not file_calls
        login = await asyncio.to_thread(control.login, org_id="fixture", email="owner@example.test", password="local-test-only")
        from orchestrator.permissions.models import PermissionSubject
        subject = PermissionSubject(context.instance_id, context.owner_id, context.agent_id, context.session_id, "1")
        approved = await asyncio.to_thread(control.submit_session_decision, subject,
                                           ["approve", first.result.request_id, "once", "--revision", "1"],
                                           session_token=login["session"]["token"], invocation_id="human", connection_binding="actual-private-fc")
        assert approved.choice == "approve"
        item = QueuedRequest(accepted.request_id, 7, "Write report", "test", "Report",
                             datetime.now(timezone.utc).isoformat(), session_id=context.session_id,
                             run_id=context.run_id, owner_id=context.owner_id, message_id=accepted.message_id, fencing_token=fence)
        reference = coordinator.store.active_turn_recovery(her_turn.session_id, her_turn.turn_id)
        service.runs.parking.park(binding, item, checkpoint_reference={
            "her_session_id": her_turn.session_id, "turn_id": her_turn.turn_id,
            "digest": reference["provider_context_digest"],
            "producer_run_claim": reference["provider_context_producer_claim"],
        }, request_id=first.result.request_id, operation_id=first.result.operation_id, state="waiting")
        coordinator.store.mark_permission_waiting(session_id=her_turn.session_id, turn_id=her_turn.turn_id,
                                                  execution_owner_id=coordinator.execution_owner["owner_id"],
                                                  expected_digest=reference["provider_context_digest"])
        await client.execution_registry(binding).park()
        resumed, _ = service.runs.parking.claim_resume(binding)
        next_fence = store.mark_request_running(resumed.request_id, worker_id="INSTANCE:agent")
        successor = await client.open_execution(**{**arguments, "fencing_token": next_fence, "worker_generation": "second"})
        ticket = service.runs.parking.resume_ticket(successor)
        recovery = coordinator.store.adopt_permission_turn(ticket, execution_owner=coordinator.execution_owner)
        final = await asyncio.to_thread(lambda: asyncio.run(worker_run(successor, recovery)))
        assert not isinstance(final, PermissionPark), (final.result, file_calls, len(physical_calls))
        assert (tmp_path / "report.txt").read_text() == "save-once"
        assert len(file_calls) == 1 and len(physical_calls) == 2
        assert authority.decision_count(first.result.request_id) == 1
        assert len({item.request_id for item in meters}) == 2
        assert all(item.request_id.startswith("permission-provider:") for item in meters)
    finally:
        await service.close()
