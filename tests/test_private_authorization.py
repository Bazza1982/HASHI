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
def test_selected_private_policy_accepts_complete_larger_proof_and_resource_sets(tmp_path):
    from orchestrator.private_authorization import public_private_authorization_capabilities

    resources = [f"document:{index:03}" for index in range(70)]
    credentials = {
        f"team-{index:02}": {"secret": f"synthetic-secret-{index:02}-long-enough",
                             "scopes": [f"read.scope.{index:02}"],
                             "allowed_resources": resources}
        for index in range(20)
    }
    config = {"credentials": credentials, "policy": {
        "max_proofs_per_message": 24, "max_resources_per_message": 80,
        "default_ttl_seconds": 600, "max_ttl_seconds": 1200,
    }}
    binding = {**_binding(), "resources": resources}
    capabilities = public_private_authorization_capabilities(config)
    assert capabilities["max_proofs_per_message"] == 24
    assert capabilities["max_resources_per_message"] == 80
    assert capabilities["default_ttl_seconds"] == 600
    assert capabilities["max_ttl_seconds"] == 1200

    proofs = build_private_authorization_proofs(
        config, credential_ids=list(credentials), binding=binding, now=1_800_000_000
    )
    assert len(proofs) == 20
    assert all(proof["expires_at"] - proof["issued_at"] == 600 for proof in proofs)
    store = PrivateAuthorizationStore(tmp_path / "proofs.sqlite3")
    checked = verify_private_authorization_proofs(
        config, proofs=proofs, binding=binding, nonce_store=store, now=1_800_000_400
    )
    assert len(checked) == 20
    assert all(item["state"] == "success" and item["resources"] == resources for item in checked)
    retried = verify_private_authorization_proofs(
        config, proofs=proofs, binding=binding, nonce_store=store, now=1_800_000_401
    )
    assert all(item["idempotent_revalidation"] for item in retried)
    config["credentials"]["team-19"]["revoked"] = True
    revoked = verify_private_authorization_proofs(
        config, proofs=proofs, binding=binding, nonce_store=store, now=1_800_000_402
    )
    assert revoked[-1]["state"] == "revoked"
    altered = verify_private_authorization_proofs(
        config, proofs=proofs, binding={**binding, "content_sha256": authorization_content_sha256("changed")},
        nonce_store=store, now=1_800_000_403,
    )
    assert all(item["state"] in {"invalid", "revoked"} for item in altered)
    assert all("scopes" not in item for item in altered)


def test_private_verifier_reports_over_budget_without_partial_or_silent_grants(tmp_path):
    from orchestrator.message_context import _normalized_authorization_results

    credentials = {
        f"team-{index:02}": {"secret": f"synthetic-secret-{index:02}-long-enough",
                             "scopes": ["read.fixture"]}
        for index in range(20)
    }
    builder = {"credentials": credentials, "policy": {"max_proofs_per_message": 24}}
    proofs = build_private_authorization_proofs(
        builder, credential_ids=list(credentials), binding=_binding(), now=1_800_000_000
    )
    result = verify_private_authorization_proofs(
        credentials, proofs=proofs, binding=_binding(),
        nonce_store=PrivateAuthorizationStore(tmp_path / "proofs.sqlite3"), now=1_800_000_001,
    )
    assert not any(item["state"] == "success" for item in result)
    assert result[0]["reason"] == "proof_budget_exceeded"
    assert result[0]["provided_count"] == 20 and result[0]["max_proofs"] == 16
    assert _normalized_authorization_results(result)[0]["reason"] == "proof_budget_exceeded"


def test_message_context_projects_all_verified_authorizations(tmp_path):
    from types import SimpleNamespace
    from orchestrator.message_context import build_message_context_snapshot

    authorizations = [{"credential_id": f"team-{index:02}", "state": "success",
                       "scopes": [f"scope-{index:02}"], "resources": []}
                      for index in range(20)]
    runtime = SimpleNamespace(global_config=SimpleNamespace(instance_id="HASHI2", project_root=tmp_path))
    snapshot = build_message_context_snapshot(
        runtime, source="session-api", chat_id=0, prompt="fixture",
        metadata={"_private_authorization_results": authorizations},
    )
    assert len(snapshot["private_authorizations"]) == 20
    assert snapshot["private_authorizations"][-1]["scopes"] == ["scope-19"]
def test_private_policy_misconfiguration_does_not_break_messages_without_proofs(tmp_path):
    from orchestrator.private_authorization import public_private_authorization_capabilities

    config = {"credentials": {}, "policy": {"max_proofs_per_message": -1}}
    assert build_private_authorization_proofs(config, credential_ids=[], binding={}) == []
    store = PrivateAuthorizationStore(tmp_path / "proofs.sqlite3")
    assert verify_private_authorization_proofs(config, proofs=[], binding={}, nonce_store=store) == []
    [rejected] = verify_private_authorization_proofs(config, proofs=[{}], binding={}, nonce_store=store)
    assert rejected["state"] == "invalid" and rejected["reason"] == "policy_invalid"
    capability = public_private_authorization_capabilities(config)
    assert capability["available"] is False
    assert capability["max_proofs_per_message"] is None


def test_shorter_maximum_private_ttl_also_bounds_unspecified_default():
    from orchestrator.private_authorization import public_private_authorization_capabilities

    capability = public_private_authorization_capabilities(
        {"credentials": {}, "policy": {"max_ttl_seconds": 60}}
    )
    assert capability["default_ttl_seconds"] == 60
    assert capability["max_ttl_seconds"] == 60


def test_resource_budget_rejection_is_explicit_and_grants_no_scope(tmp_path):
    credentials = {"team": {"secret": "synthetic-secret-long-enough", "scopes": ["read.fixture"]}}
    resources = [f"document:{index:03}" for index in range(70)]
    binding = {**_binding(), "resources": resources}
    proof = build_private_authorization_proofs(
        {"credentials": credentials, "policy": {"max_resources_per_message": 80}},
        credential_ids=["team"], binding=binding,
    )
    [result] = verify_private_authorization_proofs(
        credentials, proofs=proof, binding=binding,
        nonce_store=PrivateAuthorizationStore(tmp_path / "proofs.sqlite3"),
    )
    assert result["state"] == "invalid"
    assert result["reason"] == "resource_budget_exceeded"
    assert result["max_resources"] == 64
    assert "scopes" not in result


def test_new_proof_for_same_binding_can_continue_after_original_expiry(tmp_path):
    config = {"credentials": {"team": {"secret": "synthetic-secret-long-enough",
                                      "scopes": ["read.fixture"]}},
              "policy": {"default_ttl_seconds": 600, "max_ttl_seconds": 1200}}
    binding = _binding()
    first = build_private_authorization_proofs(config, credential_ids=["team"], binding=binding, now=10000)
    store = PrivateAuthorizationStore(tmp_path / "proofs.sqlite3")
    assert verify_private_authorization_proofs(config, proofs=first, binding=binding,
        nonce_store=store, now=10500)[0]["state"] == "success"
    renewal = build_private_authorization_proofs(config, credential_ids=["team"], binding=binding, now=10590)
    assert verify_private_authorization_proofs(config, proofs=first, binding=binding,
        nonce_store=store, now=10601)[0]["state"] == "expired"
    assert verify_private_authorization_proofs(config, proofs=renewal, binding=binding,
        nonce_store=store, now=10601)[0]["state"] == "success"
    assert verify_private_authorization_proofs(config, proofs=renewal, binding=binding,
        nonce_store=store, now=10602)[0]["idempotent_revalidation"] is True
def test_new_proof_cannot_change_original_recipient_message_binding_after_restart(tmp_path):
    config = {"credentials": {"team": {"secret": "synthetic-secret-long-enough",
                                      "scopes": ["read.fixture"]}},
              "policy": {"default_ttl_seconds": 600, "max_ttl_seconds": 1200}}
    path = tmp_path / "proofs.sqlite3"
    first_binding = _binding()
    first = build_private_authorization_proofs(
        config, credential_ids=["team"], binding=first_binding, now=10000
    )
    assert verify_private_authorization_proofs(config, proofs=first, binding=first_binding,
        nonce_store=PrivateAuthorizationStore(path), now=10001)[0]["state"] == "success"

    changed = {**first_binding, "resources": ["user:other"]}
    changed_proof = build_private_authorization_proofs(
        config, credential_ids=["team"], binding=changed, now=11000
    )
    assert verify_private_authorization_proofs(config, proofs=changed_proof, binding=changed,
        nonce_store=PrivateAuthorizationStore(path), now=11001)[0]["state"] == "invalid"

    # A fanout recipient is an independent binding, not a changed first one.
    other = {**first_binding, "to_agent": "other"}
    other_proof = build_private_authorization_proofs(
        config, credential_ids=["team"], binding=other, now=11000
    )
    assert verify_private_authorization_proofs(config, proofs=other_proof, binding=other,
        nonce_store=PrivateAuthorizationStore(path), now=11001)[0]["state"] == "success"
def test_concurrent_renewals_cannot_bind_one_recipient_message_to_different_content(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    config = {"team": {"secret": "synthetic-secret-long-enough", "scopes": ["read.fixture"]}}
    first_binding = _binding()
    changed_binding = {**first_binding, "resources": ["user:other"]}
    first = build_private_authorization_proofs(config, credential_ids=["team"], binding=first_binding)
    changed = build_private_authorization_proofs(config, credential_ids=["team"], binding=changed_binding)
    path = tmp_path / "proofs.sqlite3"
    stores = [PrivateAuthorizationStore(path), PrivateAuthorizationStore(path)]
    barrier = threading.Barrier(2)

    def check(index, proof, binding):
        barrier.wait(timeout=3)
        return verify_private_authorization_proofs(config, proofs=proof, binding=binding,
                                                   nonce_store=stores[index])[0]["state"]

    with ThreadPoolExecutor(max_workers=2) as workers:
        a = workers.submit(check, 0, first, first_binding)
        b = workers.submit(check, 1, changed, changed_binding)
        assert sorted([a.result(timeout=10), b.result(timeout=10)]) == ["invalid", "success"]
