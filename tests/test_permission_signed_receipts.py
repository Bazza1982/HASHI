"""Signed decisions are accepted only for a pinned, protected issuer."""
import base64
from dataclasses import asdict, replace
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from orchestrator.permissions.authority import (
    DecisionReceipt,
    InvalidDecision,
    PermissionAuthority,
    RevisionConflict,
)
from orchestrator.permissions.models import ExecutionContext, normalize_scope, scope_digest
from orchestrator.permissions.signed_receipts import SignedReceiptVerifier


def _b64(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _token(key, receipt, *, key_id="trusted", raw=None, domain=b"hashi-permission-decision-v1\0"):
    payload = (raw if raw is not None else json.dumps(
        asdict(receipt), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8"))
    signature = key.sign(domain + key_id.encode("ascii") + b"\0" + payload)
    return ".".join(("hpd1", key_id, _b64(payload), _b64(signature)))


def _public(key):
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )


@pytest.fixture
def signed_environment(tmp_path):
    context = ExecutionContext(
        instance_id="instance", owner_id="owner", agent_id="agent", task_id="task",
        run_id="run", session_id="session", turn_id="turn", worker_generation="worker",
        context_generation="generation", execution_epoch="epoch", backend_id="herv3",
        executor_id="registry", policy_revision=1,
    )
    key = Ed25519PrivateKey.generate()
    verifier = SignedReceiptVerifier({"trusted": _public(key)})
    authority = PermissionAuthority(tmp_path / "permission.sqlite", verifier=verifier,
                                    clock=lambda: 1000.0, monotonic=lambda: 1000.0)
    authority.open_task(context)
    scope = normalize_scope("file", str(tmp_path), ["read", "write"], cwd=tmp_path)
    request = authority.request_access(context, [scope], purpose="produce a report")
    receipt = DecisionReceipt(
        nonce="n1", request_id=request.request_id, request_revision=request.revision,
        requested_digest=request.scope_digest, approved_digest=scope_digest([scope]),
        instance_id=context.instance_id, task_id=context.task_id, run_id=context.run_id,
        execution_epoch=context.execution_epoch, operation_id=None, arguments_digest=None,
        policy_revision=context.policy_revision, owner_id=context.owner_id, agent_id=context.agent_id,
        session_id=context.session_id, context_generation=context.context_generation,
        principal_id=context.owner_id, choice="approve", lifetime="task", duration_seconds=60,
        idempotency_key="decision-1", expires_at=1030.0,
    )
    return authority, key, verifier, context, scope, request, receipt


def test_valid_signature_proves_receipt_and_ledger_applies_exact_decision(signed_environment):
    authority, key, verifier, context, scope, _, receipt = signed_environment
    reference = _token(key, receipt)
    assert verifier.verify(reference) == receipt
    lease = authority.decide(reference, approved_scopes=[scope])
    assert authority.decide(reference, approved_scopes=[scope]) == lease
    assert authority.decision_count(receipt.request_id) == 1
    assert authority.evaluate(context, [scope])


def test_untrusted_or_tampered_references_never_grant(signed_environment):
    authority, key, verifier, context, scope, _, receipt = signed_environment
    original = _token(key, receipt)
    header, name, payload, signature = original.split(".")
    altered = json.dumps(asdict(replace(receipt, duration_seconds=6000)), sort_keys=True,
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    attacker = Ed25519PrivateKey.generate()
    examples = [
        "ordinary-user-answer",
        ".".join((header, name, _b64(altered), signature)),
        _token(attacker, receipt),
        _token(key, receipt, key_id="unknown"),
        _token(key, receipt, domain=b"other-protocol\0"),
        original + "=",
        original[:30],
    ]
    for example in examples:
        assert verifier.verify(example) is None
        with pytest.raises(InvalidDecision):
            authority.decide(example, approved_scopes=[scope])
    assert not authority.evaluate(context, [scope])


def test_signed_old_revision_and_other_scope_are_not_approved(signed_environment, tmp_path):
    authority, key, _, context, scope, request, receipt = signed_environment
    old_reference = _token(key, receipt)
    narrower = normalize_scope("file", str(tmp_path), ["read"], cwd=tmp_path)
    authority.narrow_request(context, request.request_id, request.revision, [narrower])
    with pytest.raises(RevisionConflict):
        authority.decide(old_reference, approved_scopes=[scope])
    assert not authority.evaluate(context, [scope])
    assert not authority.evaluate(context, [narrower])


def test_signed_payload_is_strict_and_key_rotation_is_explicit(signed_environment):
    _, key, verifier, _, _, _, receipt = signed_environment
    new = Ed25519PrivateKey.generate()
    rotated = SignedReceiptVerifier({"new": _public(new)})
    assert rotated.verify(_token(key, receipt)) is None
    assert rotated.verify(_token(new, receipt, key_id="new")) == receipt

    original = json.dumps(asdict(receipt), sort_keys=True, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
    duplicate = b'{"nonce":"different",' + original[1:]
    extra = original[:-1] + b',"not_a_claim":true}'
    for invalid in (duplicate, extra, b"{}", b"[]", b"not-json"):
        assert verifier.verify(_token(key, receipt, raw=invalid)) is None
    assert verifier.verify("x" * 20000) is None


def test_bad_trust_configuration_fails_closed():
    with pytest.raises(ValueError):
        SignedReceiptVerifier({})
    with pytest.raises(ValueError):
        SignedReceiptVerifier({"bad.key": b"x" * 32})
    with pytest.raises(ValueError):
        SignedReceiptVerifier({"trusted": b"x"})


@pytest.mark.parametrize(
    "changed",
    [
        {"principal_id": "different-owner"},
        {"session_id": "different-session"},
        {"instance_id": "different-instance"},
        {"expires_at": 1000.0},
        {"choice": "approve", "duration_seconds": 0},
    ],
)
def test_valid_signature_cannot_bypass_ledger_binding(signed_environment, changed):
    authority, key, _, context, scope, _, receipt = signed_environment
    reference = _token(key, replace(receipt, **changed))
    with pytest.raises(InvalidDecision):
        authority.decide(reference, approved_scopes=[scope])
    assert not authority.evaluate(context, [scope])


def test_valid_signature_does_not_authorize_a_broader_scope(signed_environment, tmp_path):
    authority, key, _, context, _, _, receipt = signed_environment
    elsewhere = normalize_scope("file", str(tmp_path.parent), ["read", "write"], cwd=tmp_path)
    reference = _token(key, replace(receipt, approved_digest=scope_digest([elsewhere])))
    with pytest.raises(InvalidDecision):
        authority.decide(reference, approved_scopes=[elsewhere])
    assert not authority.evaluate(context, [elsewhere])
