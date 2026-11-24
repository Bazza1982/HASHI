"""Focused regressions for HASHI PR #203 final narrow closure.

Two P1 findings:

1. Reconstructed Tool evidence must survive multiple interrupted recoveries.
   The incompatible-provider recovery projects completed Tool results into the
   assembled prompt, which is tracked as replaceable owned context. A later
   recovery removes that prompt and with it the reconstructed evidence. The fix
   persists the projection as a durable checkpoint field, separate from the
   replaceable PCM assembly, and reuses it on subsequent incompatible recoveries.

2. Protected evidence discovery. The model may hold the canonical evidence
   reference but not the omitted item identity. The protected callback must
   support scoped discovery of authorized item identities through the existing
   request_diagnostics operation.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from adapters.her_v2_provider import HashiStageProvider


# ---------------------------------------------------------------------------
# P1-1 — reconstructed evidence survives multiple interrupted recoveries
# ---------------------------------------------------------------------------

def test_reconstructed_evidence_reused_from_durable_checkpoint():
    """A second incompatible recovery reuses the durable reconstruction.

    The recovered checkpoint no longer carries the original ``tool`` role
    messages (they were replaced by the assembled prompt), but it does carry the
    durable ``recovery_reconstruction`` projection from the prior recovery.
    """
    context = {
        "recovery_reconstruction": {
            "total": 1,
            "recent": [{
                "tool_call_id": "read-old",
                "status": "completed",
                "output": "UNIQUE_USEFUL_FACT",
            }],
            "omitted": 0,
            "evidence_ref": "her_provider_context_items:sess:turn",
        },
        # No tool messages: the old code would reconstruct nothing here.
        "messages": [],
    }
    request = SimpleNamespace(context={
        "pcm_input": {"runtime_context": {"active_turn_recovery": {
            "provider_context_checkpoint": {"items_evidence_ref": "her_provider_context_items:sess:turn"},
        }}},
    })
    result = HashiStageProvider._reconstructed_tool_evidence(context, request)
    assert result["total"] == 1
    assert result["recent"][0]["tool_call_id"] == "read-old"
    assert result["recent"][0]["output"] == "UNIQUE_USEFUL_FACT"
    assert result["evidence_ref"] == "her_provider_context_items:sess:turn"


def test_reconstruction_derives_from_tool_messages_when_no_durable_field():
    """The original derivation path still works without a durable field."""
    context = {
        "messages": [
            {"role": "tool", "tool_call_id": "read-new", "content": "FRESH_FACT"},
        ],
    }
    request = SimpleNamespace(context={
        "pcm_input": {"runtime_context": {"active_turn_recovery": {
            "provider_context_checkpoint": {"items_evidence_ref": ""},
        }}},
    })
    result = HashiStageProvider._reconstructed_tool_evidence(context, request)
    assert result["total"] == 1
    assert result["recent"][0]["tool_call_id"] == "read-new"
    assert result["recent"][0]["output"] == "FRESH_FACT"


def test_recovery_reconstruction_survives_checkpoint_roundtrip(tmp_path):
    """The durable reconstruction survives the store write/read round-trip."""
    from tests.test_permission_pao_fence import setup_run
    from orchestrator.her_v2.backend_session import HerBackendSessionCoordinator
    from tests.test_her_v2_control_plane import _run_claim

    store, _session, _accepted, _fence, context = setup_run(tmp_path)
    home = tmp_path / "agent-home"
    home.mkdir()
    session_workspace = home
    coordinator = HerBackendSessionCoordinator(home / "backend_state" / "her_v2")
    shared = dict(
        session_id="private-her-session", sections=[], resources=[], instance_id=context.instance_id,
        agent_id=context.agent_id, owner_id=context.owner_id, hashi_conversation_id=context.session_id,
        context_generation=int(context.context_generation), workzone_identity=str(session_workspace),
    )
    encoded, _ = coordinator.prepare_transport(
        **shared, user_message="Inspect", request_id="req-roundtrip",
        message_id="msg-roundtrip",
    )
    source = coordinator.accept(encoded)
    reconstruction = {
        "total": 1,
        "recent": [{"tool_call_id": "read-old", "status": "completed", "output": "UNIQUE_FACT"}],
        "omitted": 0,
        "evidence_ref": "her_provider_context_items:sess:turn",
    }
    coordinator.record_provider_context_checkpoint(source, {
        "provider": "openrouter-api", "model": "test-model", "stage": "direct",
        "boundary": "completed_provider_response",
        "messages": [
            {"role": "user", "content": "Inspect"},
            {"role": "assistant", "content": "Done"},
        ],
        "reasoning": [],
        "recovery_reconstruction": reconstruction,
        "producer_run_claim": _run_claim("run-roundtrip", "req-roundtrip"),
    })
    read_back = coordinator.store.provider_context_for_turn(source.session_id, source.turn_id)
    assert isinstance(read_back, dict)
    assert read_back.get("recovery_reconstruction") == reconstruction
    assert read_back["recovery_reconstruction"]["recent"][0]["output"] == "UNIQUE_FACT"


# ---------------------------------------------------------------------------
# P1-2 — protected evidence discovery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_protected_discovery_of_omitted_receipt(tmp_path):
    """Discover and retrieve an omitted authorized receipt from the canonical ref."""
    from dataclasses import replace

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

    store, _session, accepted, fence, context = setup_run(tmp_path)
    home = tmp_path / "agent-home"
    home.mkdir()
    coordinator = HerBackendSessionCoordinator(home / "backend_state" / "her_v2")
    shared = dict(
        session_id="private-her-session", sections=[], resources=[], instance_id=context.instance_id,
        agent_id=context.agent_id, owner_id=context.owner_id, hashi_conversation_id=context.session_id,
        context_generation=int(context.context_generation), workzone_identity=str(home),
    )

    def accept(request_id):
        encoded, _ = coordinator.prepare_transport(
            **shared, user_message="Inspect many documents", request_id=request_id,
            message_id="message-" + request_id,
        )
        return coordinator.accept(encoded)

    source = accept("source-request")
    # Enough tool messages that the projection must omit some receipts.
    tool_messages = []
    assistant_calls = []
    for index in range(50):
        call_id = f"read-{index}"
        output = f"useful-fact-{index}-" + ("detail-" * 200)
        coordinator.store.record_runtime_event(
            session_id=source.session_id, turn_id=source.turn_id,
            record=_runtime_event(source.turn_id, f"receipt-{index}", "tool_receipt", receipt={
                "tool_call_id": call_id, "tool_name": "file_read", "completed": True,
                "status": "completed", "output": output,
            }),
        )
        assistant_calls.append({"id": call_id, "type": "function",
                                "function": {"name": "file_read", "arguments": json.dumps({"path": f"f{index}"})}})
        tool_messages.append({"role": "tool", "tool_call_id": call_id, "content": output})
    coordinator.record_provider_context_checkpoint(source, {
        "provider": "openrouter-api", "model": "test-model", "stage": "direct",
        "boundary": "completed_tool_batch",
        "messages": [{"role": "user", "content": "Inspect many documents"},
                     {"role": "assistant", "tool_calls": assistant_calls},
                     *tool_messages],
        "reasoning": [],
        "producer_run_claim": _run_claim("run-source", "source-request"),
    })
    assert coordinator.store.reconcile_interrupted() == 1
    successor = accept(accepted.request_id)
    view = bounded_prompt_projection(coordinator.store.consume_recovery_context(
        session_id=successor.session_id, current_turn_id=successor.turn_id))
    assert view["provider_context_checkpoint"]["items_evidence_ref"] == (
        f"her_provider_context_items:{source.session_id}:{source.turn_id}"
    )
    # The tool_receipts projection must have omitted some identities.
    assert view["tool_receipts"]["omitted"] > 0, view["tool_receipts"]

    context = replace(context, turn_id=successor.turn_id)
    binding = RuntimeExecutionBinding(context, accepted.request_id, fence, "INSTANCE:agent")
    identity = {
        "instance_id": context.instance_id, "agent_id": context.agent_id, "owner_id": context.owner_id,
        "hashi_conversation_id": context.session_id, "context_generation": int(context.context_generation),
        "workzone_identity": str(home),
    }
    adapter = SimpleNamespace(
        _session_coordinator=coordinator, config=SimpleNamespace(workspace_dir=home),
        _runtime_request_meta=lambda request_id: {"session_workspace": str(home)},
        _fixed_session_binding=lambda meta: dict(identity),
    )
    turn = SimpleNamespace(binding=binding, adapter=adapter, accepted=successor,
                           runtime=SimpleNamespace(pcm_input={"runtime_context": {"active_turn_recovery": view}}))
    callbacks = WorkerHerCallbacks(SimpleNamespace(
        service=SimpleNamespace(store=store), _turns={accepted.request_id: turn}))

    # Discovery: the model holds only the canonical reference, no item id.
    probe = await callbacks.recovery_evidence(binding=binding, arguments={
        "request_id": "source-request",
        "recovery_evidence_ref": view["provider_context_checkpoint"]["items_evidence_ref"],
        "recovery_item_id": "", "offset": 0, "expected_digest": "",
    })
    assert probe["page"]["status"] == "available", probe
    discovered = json.loads(probe["page"]["content"])
    assert discovered["count"] == 50
    assert set(discovered["item_ids"]) == {f"read-{i}" for i in range(50)}

    # Retrieve an omitted receipt's useful result by a discovered identity.
    target = "read-49"
    retrieved = await callbacks.recovery_evidence(binding=binding, arguments={
        "request_id": "source-request",
        "recovery_evidence_ref": view["provider_context_checkpoint"]["items_evidence_ref"],
        "recovery_item_id": target, "offset": 0, "expected_digest": "",
    })
    assert retrieved["page"]["status"] == "available", retrieved
    content = json.loads(retrieved["page"]["content"])
    assert content["tool_call_id"] == target
    assert content["content"].startswith("useful-fact-49-")

    # Unrelated turn remains unavailable.
    bad_ref = view["provider_context_checkpoint"]["items_evidence_ref"].replace(
        source.turn_id, "other-turn")
    denied = await callbacks.recovery_evidence(binding=binding, arguments={
        "request_id": "source-request", "recovery_evidence_ref": bad_ref,
        "recovery_item_id": "", "offset": 0, "expected_digest": "",
    })
    assert denied["page"]["status"] == "not available"
# HASHI-Fingerprint: hfp_2ba7a96b00ad4d45b519364f0bb2ca8f
