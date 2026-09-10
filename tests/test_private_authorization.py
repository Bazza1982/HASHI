from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orchestrator.private_authorization import (
    PrivateAuthorizationStore,
    authorization_content_sha256,
    build_private_authorization_proofs,
    verify_private_authorization_proofs,
)


SECRET = "synthetic-finance-secret-123456789"


def _config(*, revoked: bool = False, expires_at: str | None = None):
    return {
        "finance_team": {
            "secret": SECRET,
            "group": "Finance",
            "scopes": ["finance.report", "finance.summary"],
            "allowed_source_agents": ["sender"],
            "allowed_source_instances": ["HASHI1"],
            "allowed_target_agents": ["sunny"],
            "allowed_target_instances": ["HASHI2"],
            "allowed_resources": ["user:synthetic-dad"],
            "revoked": revoked,
            "expires_at": expires_at,
        },
        "marketing_team": {
            "secret": "synthetic-marketing-secret-123456789",
            "group": "Marketing",
            "scopes": ["marketing.report"],
        },
    }


def _binding(message_id: str = "wire-msg-1"):
    return {
        "message_id": message_id,
        "from_instance": "HASHI1",
        "from_agent": "sender",
        "to_instance": "HASHI2",
        "to_agent": "sunny",
        "content_sha256": authorization_content_sha256("synthetic request"),
        "resources": ["user:synthetic-dad"],
    }


def test_multiple_selected_credentials_are_independent_and_receiver_mapped(tmp_path):
    binding = _binding()
    proofs = build_private_authorization_proofs(
        _config(),
        credential_ids=["finance_team", "marketing_team"],
        binding=binding,
        now=1_800_000_000,
    )
    results = verify_private_authorization_proofs(
        _config(),
        proofs=proofs,
        binding=binding,
        nonce_store=PrivateAuthorizationStore(tmp_path / "proofs.sqlite3"),
        now=1_800_000_001,
    )
    assert [item["state"] for item in results] == ["success", "success"]
    assert results[0]["group"] == "Finance"
    assert results[0]["scopes"] == ["finance.report", "finance.summary"]
    assert results[1]["scopes"] == ["marketing.report"]
    assert all(SECRET not in str(item) for item in results)


def test_no_proof_is_explicit_none_and_bad_proofs_do_not_block_message(tmp_path):
    store = PrivateAuthorizationStore(tmp_path / "proofs.sqlite3")
    assert verify_private_authorization_proofs(
        _config(), proofs=[], binding=_binding(), nonce_store=store
    ) == []

    proof = build_private_authorization_proofs(
        _config(), credential_ids=["finance_team"], binding=_binding()
    )[0]
    proof["digest"] = "0" * 64
    [result] = verify_private_authorization_proofs(
        _config(), proofs=[proof], binding=_binding(), nonce_store=store
    )
    assert result["state"] == "invalid"
    assert "scopes" not in result


def test_target_resource_and_cross_message_tampering_fail(tmp_path):
    store = PrivateAuthorizationStore(tmp_path / "proofs.sqlite3")
    proof = build_private_authorization_proofs(
        _config(), credential_ids=["finance_team"], binding=_binding()
    )[0]
    for binding in (
        {**_binding(), "to_agent": "other"},
        {**_binding(), "from_agent": "other"},
        {**_binding(), "resources": ["user:someone-else"]},
        {**_binding(), "content_sha256": authorization_content_sha256("tampered")},
        _binding("wire-msg-2"),
    ):
        [result] = verify_private_authorization_proofs(
            _config(), proofs=[proof], binding=binding, nonce_store=store
        )
        assert result["state"] == "invalid"


def test_malformed_receiver_policy_fails_closed_without_exposing_scope(tmp_path):
    config = _config()
    config["finance_team"]["allowed_target_agents"] = "sunny"
    proof = build_private_authorization_proofs(
        _config(), credential_ids=["finance_team"], binding=_binding()
    )[0]

    [result] = verify_private_authorization_proofs(
        config,
        proofs=[proof],
        binding=_binding(),
        nonce_store=PrivateAuthorizationStore(tmp_path / "proofs.sqlite3"),
    )

    assert result["state"] == "invalid"
    assert "scopes" not in result


def test_expired_and_revoked_are_distinct_and_grant_nothing(tmp_path):
    now = datetime.now(timezone.utc)
    expired_config = _config(expires_at=(now - timedelta(seconds=1)).isoformat())
    expired_proof = build_private_authorization_proofs(
        _config(), credential_ids=["finance_team"], binding=_binding(), now=now.timestamp() - 10
    )[0]
    [expired] = verify_private_authorization_proofs(
        expired_config,
        proofs=[expired_proof],
        binding=_binding(),
        nonce_store=PrivateAuthorizationStore(tmp_path / "expired.sqlite3"),
        now=now.timestamp(),
    )
    [revoked] = verify_private_authorization_proofs(
        _config(revoked=True),
        proofs=[expired_proof],
        binding=_binding(),
        nonce_store=PrivateAuthorizationStore(tmp_path / "revoked.sqlite3"),
        now=now.timestamp(),
    )
    assert expired["state"] == "expired"
    assert revoked["state"] == "revoked"
    assert "scopes" not in expired and "scopes" not in revoked


def test_same_message_retry_revalidates_but_cross_message_replay_fails(tmp_path):
    store = PrivateAuthorizationStore(tmp_path / "proofs.sqlite3")
    proof = build_private_authorization_proofs(
        _config(), credential_ids=["finance_team"], binding=_binding()
    )[0]
    [first] = verify_private_authorization_proofs(
        _config(), proofs=[proof], binding=_binding(), nonce_store=store
    )
    [retry] = verify_private_authorization_proofs(
        _config(), proofs=[proof], binding=_binding(), nonce_store=store
    )
    [replay] = verify_private_authorization_proofs(
        _config(), proofs=[proof], binding=_binding("wire-msg-2"), nonce_store=store
    )
    assert first["state"] == "success"
    assert retry["state"] == "success"
    assert retry["idempotent_revalidation"] is True
    assert replay["state"] == "invalid"


def test_retry_after_revocation_loses_authorization(tmp_path):
    store = PrivateAuthorizationStore(tmp_path / "proofs.sqlite3")
    proof = build_private_authorization_proofs(
        _config(), credential_ids=["finance_team"], binding=_binding()
    )[0]
    assert verify_private_authorization_proofs(
        _config(), proofs=[proof], binding=_binding(), nonce_store=store
    )[0]["state"] == "success"
    assert verify_private_authorization_proofs(
        _config(revoked=True), proofs=[proof], binding=_binding(), nonce_store=store
    )[0]["state"] == "revoked"
