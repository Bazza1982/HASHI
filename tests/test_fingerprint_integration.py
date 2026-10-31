"""Management, gateway JSON and real Engine/permission boundary integrations."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest

from adapters.codex_cli import CodexCLIAdapter
from orchestrator.config_admin import ConfigAdmin
from orchestrator.config_json import write_config_json
from orchestrator.fingerprint.coverage import observe_engine_event
from orchestrator.fingerprint.models import FingerprintPolicy
from tests.test_fingerprint_admission import runtime_at
from tests.test_fingerprint_execution import registry_at, events
from tests.test_permission_registry import environment
from tests.test_permission_execution_gate import find_pending


@pytest.mark.asyncio
async def test_management_cas_applies_to_future_admission_and_preserves_earlier_run(tmp_path):
    runtime = runtime_at(tmp_path, False)
    config_path = tmp_path / "agents.json"
    write_config_json(config_path, {"global": {"instance_id": "TEST"}, "agents": []})
    runtime.global_config.config_path = config_path
    first_id = await runtime.enqueue_request(7, "old", "api", "old", idempotency_key="old")
    first = runtime.queue.get_nowait()
    _, audit, _ = registry_at(tmp_path)
    admin = ConfigAdmin(SimpleNamespace(config_path=config_path))
    snapshot = admin.read_fingerprint_policy()
    admin.write_fingerprint_policy(FingerprintPolicy(enabled=True),
        expected_revision=snapshot.revision, actor_id="owner", audit=audit)
    await runtime.enqueue_request(7, "new", "api", "new", idempotency_key="new")
    second = runtime.queue.get_nowait()
    assert second.request_metadata["fingerprint_policy"]["enabled"] is True
    assert runtime.session_store.get_run(first.run_id)["fingerprint_policy"]["enabled"] is False


@pytest.mark.asyncio
async def test_gateway_serialization_preserves_trusted_frozen_policy_and_ids(tmp_path):
    from tools.gateway.context import GatewayContext
    registry, store, work = registry_at(tmp_path)
    context = GatewayContext.from_registry(registry, backend="codex")
    copied = GatewayContext(**json.loads(json.dumps(asdict(context))))
    gateway = copied.build_registry()
    result = await gateway.execute("file_write", {"path": "gateway.json", "content": "{}"}, tool_call_id="gateway")
    assert not result.is_error
    assert result.details["fingerprint"]["identity"]["run_id"] == "run"
    assert result.details["fingerprint"]["identity"]["instance_id"] == "TEST"
    assert events(store, "fingerprint_receipt")[-1]["payload"]["tool_call_id"] == "gateway"


@pytest.mark.asyncio
async def test_actual_codex_file_change_converter_retains_all_reported_paths(tmp_path):
    registry, store, work = registry_at(tmp_path)
    adapter = CodexCLIAdapter.__new__(CodexCLIAdapter)
    received = []
    adapter._emit_stream_event = lambda event, callback: received.append(event)
    adapter._parse_codex_event(json.dumps({"type": "item.completed", "item": {
        "type": "file_change", "changes": [{"path": "one.py"}, {"path": "two.py"}]}}), None)
    assert len(received) == 1 and isinstance(received[0].metadata["file_paths"], tuple)
    runtime = SimpleNamespace(name="agent", global_config=SimpleNamespace(instance_id="TEST"), canonical_audit=store)
    item = SimpleNamespace(session_id="session", run_id="run", request_id="request",
        request_metadata={"fingerprint_policy": FingerprintPolicy(enabled=True).snapshot()})
    await observe_engine_event(runtime, item, received[0])
    report = events(store, "fingerprint_engine_report")[-1]["payload"]
    assert report["reported_paths"] == ["one.py", "two.py"]
    assert report["coverage"]["complete"] is False


@pytest.mark.parametrize("snapshot", [FingerprintPolicy(enabled=True).snapshot(), {"schema": 999}])
@pytest.mark.asyncio
async def test_supervised_write_uses_permission_result_without_reopening_target(environment, tmp_path, monkeypatch, snapshot):
    authority, issuer, context, supervisor, registry = environment
    registry.audit_context["fingerprint_policy"] = snapshot
    import tools.effect_receipts as effects
    reads = []
    monkeypatch.setattr(effects, "observe_tool_effect", lambda **kwargs: reads.append(kwargs))
    task = asyncio.create_task(registry.execute("file_write", {"path": "result.txt", "content": "approved"}, "call"))
    request = await find_pending(authority, context)
    operation = authority.operation_status_for_task(context, request.operation_id)
    issuer.approve(authority, context, request, operation.arguments_digest)
    result = await asyncio.wait_for(task, 2)
    assert not result.is_error and (tmp_path / "result.txt").read_text() == "approved"
    assert reads == []
    assert result.details["fingerprint"]["evidence_status"] == "permission_result_only"
    assert result.details["fingerprint"]["marker_status"] == "not_qualified"
    assert not (tmp_path / ".hashi-fingerprint").exists()


@pytest.mark.asyncio
async def test_queued_exchange_recovery_uses_original_persisted_policy_after_restart(tmp_path):
    from tests.test_exchange_ingress import _configure, _delivery, _evidence
    from orchestrator.message_context import CONNECTOR_EVIDENCE_METADATA_KEY
    _configure(tmp_path)
    delivery = _delivery()
    prompt, proof = _evidence(tmp_path, delivery)
    from orchestrator.message_context import seal_connector_evidence
    # Valid signed downstream Connector facts test PAO recovery, without
    # claiming qualification of the external Exchange service/relay formatter.
    claims = {"_message_source_reserved": "hchat", "_hchat_context": {
        "from_agent": "planner", "from_instance": "ALICE", "to_agent": "reviewer",
        "to_instance": "HASHI2", "sender_assurance": "exchange_verified",
        "network_authentication": "exchange_wss", "relay_chain": [],
        "remote_principal": {"actor_id": "synthetic-local-actor"},
        "exchange_message": {"message_id": "synthetic-message"},
    }}
    metadata = {CONNECTOR_EVIDENCE_METADATA_KEY: seal_connector_evidence(tmp_path, claims=claims, prompt=prompt)}
    runtime = runtime_at(tmp_path, True, instance_id="HASHI2", agent="reviewer")
    request_id = await runtime.enqueue_request(7, prompt, "hchat-exchange", "Exchange",
        request_metadata=metadata, idempotency_key="original-exchange")
    original = runtime.queue.get_nowait()
    restarted = runtime_at(tmp_path, False, instance_id="HASHI2", agent="reviewer")
    recovered_id = await restarted.enqueue_request(7, prompt, "hchat-exchange", "Exchange",
        request_metadata=metadata, idempotency_key="original-exchange")
    recovered = restarted.queue.get_nowait()
    assert recovered_id == request_id and recovered.run_id == original.run_id
    assert recovered.request_metadata["fingerprint_policy"]["enabled"] is True
    assert len(restarted.session_store.messages(recovered.session_id)) == 1


def test_standard_public_frontend_admission_keeps_trusted_fingerprint_policy(tmp_path):
    from tests.test_frontend_standard_interface import _setup_runtime
    from orchestrator.frontend_contracts import build_frontend_ingress_envelope
    from orchestrator.frontend_ingress import admit_frontend_ingress
    runtime, store, owner, session = _setup_runtime(tmp_path)
    runtime.global_config.fingerprint = FingerprintPolicy(enabled=True)
    envelope = build_frontend_ingress_envelope(
        source_id="backend_api", ingress_transport="session-api", surface="workbench",
        channel_key="default", instance_id="HASHI1",
        principal={"kind": "human_or_client", "assurance": "runtime_observed"},
        network_authentication="not_applicable", relay_chain=[],
        request_id="fp-public", idempotency_key="fp-key",
        session_id=session["session_id"], agent_id="test-agent")
    accepted = admit_frontend_ingress(runtime, envelope, text="Hello")
    assert accepted["status"] == "accepted"
    assert store.get_run(accepted["run_id"])["fingerprint_policy"]["enabled"] is True
