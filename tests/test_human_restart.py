from __future__ import annotations

from orchestrator.human_restart import build_human_restart_proof, verify_human_restart_proof


def test_human_restart_proof_verifies():
    proof = build_human_restart_proof(
        "secret",
        requester="HASHI9",
        reason="telegram /restart hard restart",
        human_source="telegram",
        notify_agent="hashiko",
        timestamp=1000,
        nonce="abc",
    )

    ok, message = verify_human_restart_proof(
        "secret",
        requester="HASHI9",
        reason="telegram /restart hard restart",
        human_source="telegram",
        notify_agent="hashiko",
        proof=proof,
        now=1000,
    )

    assert ok is True
    assert message == "ok"


def test_human_restart_proof_rejects_wrong_reason():
    proof = build_human_restart_proof(
        "secret",
        requester="HASHI9",
        reason="telegram /restart hard restart",
        human_source="telegram",
        notify_agent="hashiko",
        timestamp=1000,
        nonce="abc",
    )

    ok, message = verify_human_restart_proof(
        "secret",
        requester="HASHI9",
        reason="different",
        human_source="telegram",
        notify_agent="hashiko",
        proof=proof,
        now=1000,
    )

    assert ok is False
    assert "failed verification" in message
