"""Focused regressions for HASHI PR #203 final narrow closure.

Reconstructed Tool evidence must survive multiple interrupted recoveries.
The incompatible-provider recovery projects completed Tool results into the
assembled prompt, which is tracked as replaceable owned context. A later
recovery removes that prompt and with it the reconstructed evidence. The fix
persists the projection as a durable checkpoint field, separate from the
replaceable PCM assembly, and reuses it on subsequent incompatible recoveries.
"""
from __future__ import annotations

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
# HASHI-Fingerprint: hfp_2ba7a96b00ad4d45b519364f0bb2ca8f
