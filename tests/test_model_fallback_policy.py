"""Focused tests for the authoritative user-consented fallback policy."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from orchestrator.config_json import ConfigConflictError
from orchestrator.flexible_backend_registry import (
    canonical_model_id,
    get_same_model_provider_targets,
    same_model_target,
)
from orchestrator.model_fallback_policy import (
    DISPOSITION_ASK_REQUIRED,
    DISPOSITION_BLOCKED,
    DISPOSITION_DISABLED,
    DISPOSITION_READY,
    DISPOSITION_UNAVAILABLE,
    MODE_ASK,
    MODE_AUTO,
    MODE_OFF,
    REASON_BUDGET_EXHAUSTED,
    REASON_CONFIRMATION_REQUIRED,
    REASON_DISABLED,
    REASON_INCOMPATIBLE_CAPABILITY,
    REASON_MISSING_KEY_OR_RUNTIME,
    REASON_MODALITY_EXCLUDED,
    REASON_NO_ALLOWED_TARGET,
    REASON_PRIVACY_BLOCKED,
    REASON_UNKNOWN_COST,
    REASON_UNSAFE_CHECKPOINT,
    SOURCE_CODEX_CLI,
    SOURCE_HERV3,
    TARGET_KIND_CODEX_CLI,
    TARGET_KIND_HERV3_MODEL,
    FallbackPolicy,
    FallbackRequestFacts,
    FallbackRoute,
    FallbackRouteFacts,
    FallbackSourceRule,
    default_policy,
    make_ask_confirmation,
    policy_path,
    read_policy,
    resolve_fallback_decision,
    write_policy,
)
from orchestrator.privacy_levels import PrivacyLevel

HERV3_ROUTE = FallbackRoute(
    source=SOURCE_HERV3,
    target_kind=TARGET_KIND_HERV3_MODEL,
    provider="openrouter-api",
    model="deepseek/deepseek-v4-flash",
    same_model=True,
)
SECOND_HERV3_ROUTE = FallbackRoute(
    source=SOURCE_HERV3,
    target_kind=TARGET_KIND_HERV3_MODEL,
    provider="hashi-api",
    model="gpt-5.6-sol",
)
CODEX_TARGET_ROUTE = FallbackRoute(
    source=SOURCE_HERV3,
    target_kind=TARGET_KIND_CODEX_CLI,
)
CODEX_SOURCE_ROUTE = FallbackRoute(
    source=SOURCE_CODEX_CLI,
    target_kind=TARGET_KIND_HERV3_MODEL,
    provider="deepseek-api",
    model="deepseek-v4-flash",
    same_model=True,
)


def _runtime(tmp_path):
    return SimpleNamespace(global_config=SimpleNamespace(bridge_home=tmp_path))


def _facts(route: FallbackRoute, **overrides) -> FallbackRequestFacts:
    base = {
        "source": SOURCE_HERV3,
        "source_provider": "deepseek-api",
        "source_model": "deepseek-v4-flash",
        "modality": "text",
        "privacy_level": int(PrivacyLevel.PROVIDER_TRUST),
        "tool_activity": False,
        "checkpoint_safe": False,
        "budget_used": 0,
        "known_extra_cost_usd": 0.01,
        "interactive": True,
        "request_id": "req-fallback-1",
        "route_facts": {
            route.key(): FallbackRouteFacts(
                credential_ready=True, capability_ok=True
            )
        },
    }
    base.update(overrides)
    return FallbackRequestFacts(**base)


def _auto_policy(*routes: FallbackRoute, **overrides) -> FallbackPolicy:
    values = {
        "mode": MODE_AUTO,
        "routes": tuple(routes),
        "max_additional_calls": 1,
    }
    values.update(overrides)
    return FallbackPolicy(**values)


def test_registry_same_model_equivalence_is_exact_and_authoritative():
    assert (
        canonical_model_id("deepseek-api", "deepseek-v4-flash")
        == "deepseek-v4-flash"
    )
    assert same_model_target(
        source_engine="deepseek-api",
        source_model="deepseek-v4-flash",
        target_engine="openrouter-api",
        target_model="deepseek/deepseek-v4-flash",
    )
    # Similar-looking names never prove equivalence.
    assert not same_model_target(
        source_engine="deepseek-api",
        source_model="deepseek-v4-flash",
        target_engine="openrouter-api",
        target_model="deepseek/deepseek-v4-pro",
    )
    assert not same_model_target(
        source_engine="deepseek-api",
        source_model="deepseek-v4-flash",
        target_engine="openrouter-api",
        target_model="deepseek/deepseek-v4-flash-preview",
    )
    assert get_same_model_provider_targets(
        "deepseek-api", "deepseek-v4-pro"
    ) == [("openrouter-api", "deepseek/deepseek-v4-pro")]


def test_default_and_migrated_policy_is_off_and_never_falls_back(tmp_path):
    runtime = _runtime(tmp_path)
    saved, document = read_policy(runtime, "owner-1", for_update=True)
    assert document is None
    assert saved.mode == MODE_OFF
    assert saved.routes == ()
    # Reading a missing configuration never writes a file.
    assert not policy_path(runtime).exists()
    decision = resolve_fallback_decision(
        saved, _facts(HERV3_ROUTE, source="herv3")
    )
    assert decision.allowed is False
    assert decision.blocking_reason == REASON_DISABLED
    assert decision.disposition == DISPOSITION_DISABLED


def test_auto_without_an_allowed_target_never_falls_back():
    policy = _auto_policy()
    decision = resolve_fallback_decision(policy, _facts(HERV3_ROUTE))
    assert decision.allowed is False
    assert decision.blocking_reason == REASON_NO_ALLOWED_TARGET
    assert decision.disposition == DISPOSITION_UNAVAILABLE


def test_ordered_allowlist_selects_the_first_eligible_target():
    policy = _auto_policy(HERV3_ROUTE, SECOND_HERV3_ROUTE)
    first = resolve_fallback_decision(
        policy,
        _facts(
            HERV3_ROUTE,
            route_facts={
                HERV3_ROUTE.key(): FallbackRouteFacts(
                    credential_ready=False, capability_ok=True
                ),
                SECOND_HERV3_ROUTE.key(): FallbackRouteFacts(
                    credential_ready=True, capability_ok=True
                ),
            },
        ),
    )
    assert first.allowed is True
    assert first.route == SECOND_HERV3_ROUTE
    assert first.disposition == DISPOSITION_READY
    # With every key configured, the user's order decides.
    ordered = FallbackPolicy(
        mode=MODE_AUTO,
        routes=(SECOND_HERV3_ROUTE, HERV3_ROUTE),
        max_additional_calls=1,
    )
    decision = resolve_fallback_decision(
        ordered,
        _facts(
            HERV3_ROUTE,
            route_facts={
                HERV3_ROUTE.key(): FallbackRouteFacts(
                    credential_ready=True, capability_ok=True
                ),
                SECOND_HERV3_ROUTE.key(): FallbackRouteFacts(
                    credential_ready=True, capability_ok=True
                ),
            },
        ),
    )
    assert decision.route == SECOND_HERV3_ROUTE


def test_sources_and_targets_remain_independent():
    policy = _auto_policy(
        HERV3_ROUTE,
        sources={
            SOURCE_HERV3: FallbackSourceRule(enabled=False),
            SOURCE_CODEX_CLI: FallbackSourceRule(
                enabled=True, allow_herv3_targets=True
            ),
        },
    )
    assert resolve_fallback_decision(policy, _facts(HERV3_ROUTE)).allowed is False
    # Enabling the Codex CLI source does not enable the HERV3 source.
    codex_facts = _facts(
        CODEX_SOURCE_ROUTE,
        source=SOURCE_CODEX_CLI,
        route_facts={
            CODEX_SOURCE_ROUTE.key(): FallbackRouteFacts(
                credential_ready=True, capability_ok=True
            )
        },
    )
    assert resolve_fallback_decision(policy, codex_facts).allowed is False
    # Independent target-kind permission: HERV3 source denied Codex targets.
    policy2 = _auto_policy(
        CODEX_TARGET_ROUTE,
        sources={
            SOURCE_HERV3: FallbackSourceRule(
                enabled=True, allow_codex_cli_targets=False
            )
        },
    )
    decision = resolve_fallback_decision(policy2, _facts(CODEX_TARGET_ROUTE))
    assert decision.allowed is False
    assert decision.blocking_reason == REASON_NO_ALLOWED_TARGET


def test_same_model_claim_must_hold_in_the_authoritative_registry():
    forged = FallbackRoute(
        source=SOURCE_HERV3,
        target_kind=TARGET_KIND_HERV3_MODEL,
        provider="openrouter-api",
        model="deepseek/deepseek-v4-pro",
        same_model=True,
    )
    policy = _auto_policy(forged)
    decision = resolve_fallback_decision(
        policy, _facts(forged, source_model="deepseek-v4-flash")
    )
    assert decision.allowed is False
    assert decision.blocking_reason == REASON_INCOMPATIBLE_CAPABILITY


@pytest.mark.parametrize(
    "level",
    [PrivacyLevel.OFF, PrivacyLevel.PROVIDER_TRUST, PrivacyLevel.BASIC_REDACTION],
)
def test_every_current_privacy_policy_can_only_narrow(level):
    policy = _auto_policy(HERV3_ROUTE)
    facts = _facts(
        HERV3_ROUTE, privacy_level=int(level), source_model="deepseek-v4-flash"
    )
    assert resolve_fallback_decision(policy, facts).allowed is True

    blocked = FallbackPolicy(
        mode=MODE_AUTO,
        routes=(HERV3_ROUTE,),
        privacy_narrowing={level.name.casefold(): {"blocked": True}},
    )
    decision = resolve_fallback_decision(blocked, facts)
    assert decision.allowed is False
    assert decision.blocking_reason == REASON_PRIVACY_BLOCKED

    narrowed = FallbackPolicy(
        mode=MODE_AUTO,
        routes=(HERV3_ROUTE,),
        privacy_narrowing={level.name.casefold(): {"max_additional_calls": 0}},
    )
    decision = resolve_fallback_decision(narrowed, facts)
    assert decision.allowed is False
    assert decision.blocking_reason == REASON_BUDGET_EXHAUSTED

    subset = FallbackPolicy(
        mode=MODE_AUTO,
        routes=(HERV3_ROUTE,),
        privacy_narrowing={
            level.name.casefold(): {"allow_route_keys": ["none|none|none|none"]}
        },
    )
    decision = resolve_fallback_decision(subset, facts)
    assert decision.allowed is False
    assert decision.blocking_reason == REASON_NO_ALLOWED_TARGET


def test_invalid_privacy_policy_fails_closed():
    decision = resolve_fallback_decision(
        _auto_policy(HERV3_ROUTE),
        _facts(HERV3_ROUTE, privacy_level=99),
    )

    assert decision.allowed is False
    assert decision.blocking_reason == REASON_PRIVACY_BLOCKED
    assert decision.additional_calls_limit == 0


@pytest.mark.parametrize("modality", ["phone", "call", "voice"])
def test_phone_call_and_voice_are_excluded_before_target_selection(modality):
    policy = _auto_policy(HERV3_ROUTE)
    decision = resolve_fallback_decision(
        policy, _facts(HERV3_ROUTE, modality=modality)
    )
    assert decision.allowed is False
    assert decision.blocking_reason == REASON_MODALITY_EXCLUDED
    assert decision.route is None
    assert decision.confirmation is None


def test_missing_key_and_incompatible_capability_block_one_route():
    policy = _auto_policy(HERV3_ROUTE)
    assert (
        resolve_fallback_decision(
            policy,
            _facts(
                HERV3_ROUTE,
                route_facts={
                    HERV3_ROUTE.key(): FallbackRouteFacts(
                        credential_ready=False, capability_ok=True
                    )
                },
            ),
        ).blocking_reason
        == REASON_MISSING_KEY_OR_RUNTIME
    )
    assert (
        resolve_fallback_decision(
            policy,
            _facts(
                HERV3_ROUTE,
                route_facts={
                    HERV3_ROUTE.key(): FallbackRouteFacts(
                        credential_ready=True, capability_ok=False
                    )
                },
            ),
        ).blocking_reason
        == REASON_INCOMPATIBLE_CAPABILITY
    )


def test_checkpoint_safety_rules_for_model_and_cross_engine_targets():
    policy = _auto_policy(HERV3_ROUTE, CODEX_TARGET_ROUTE)
    unsafe = resolve_fallback_decision(
        policy,
        _facts(
            HERV3_ROUTE,
            tool_activity=True,
            checkpoint_safe=False,
            route_facts={
                HERV3_ROUTE.key(): FallbackRouteFacts(
                    credential_ready=True, capability_ok=True
                ),
                CODEX_TARGET_ROUTE.key(): FallbackRouteFacts(
                    credential_ready=True, capability_ok=True
                ),
            },
        ),
    )
    # The post-tool model continuation is blocked without checkpoint proof;
    # the cross-engine route is blocked after any Tool activity as well.
    assert unsafe.allowed is False
    assert unsafe.blocking_reason == REASON_UNSAFE_CHECKPOINT

    safe_model = resolve_fallback_decision(
        policy,
        _facts(
            HERV3_ROUTE,
            tool_activity=True,
            checkpoint_safe=True,
            route_facts={
                HERV3_ROUTE.key(): FallbackRouteFacts(
                    credential_ready=True, capability_ok=True
                )
            },
        ),
    )
    assert safe_model.allowed is True
    assert safe_model.route == HERV3_ROUTE

    codex_only = _auto_policy(CODEX_TARGET_ROUTE)
    cross_engine = resolve_fallback_decision(
        codex_only,
        _facts(
            CODEX_TARGET_ROUTE,
            tool_activity=True,
            checkpoint_safe=True,
            route_facts={
                CODEX_TARGET_ROUTE.key(): FallbackRouteFacts(
                    credential_ready=True, capability_ok=True
                )
            },
        ),
    )
    assert cross_engine.allowed is False
    assert cross_engine.blocking_reason == REASON_UNSAFE_CHECKPOINT

    partial = resolve_fallback_decision(
        codex_only,
        _facts(
            CODEX_TARGET_ROUTE,
            visible_partial_answer=True,
            route_facts={
                CODEX_TARGET_ROUTE.key(): FallbackRouteFacts(
                    credential_ready=True, capability_ok=True
                )
            },
        ),
    )
    assert partial.allowed is False
    assert partial.blocking_reason == REASON_UNSAFE_CHECKPOINT


def test_unknown_cost_is_blocked_by_default_and_budget_stops_the_chain():
    policy = _auto_policy(HERV3_ROUTE)
    unknown = resolve_fallback_decision(
        policy, _facts(HERV3_ROUTE, known_extra_cost_usd=None)
    )
    assert unknown.allowed is False
    assert unknown.blocking_reason == REASON_UNKNOWN_COST

    allowed_unknown = _auto_policy(HERV3_ROUTE, allow_unknown_cost=True)
    assert (
        resolve_fallback_decision(
            allowed_unknown, _facts(HERV3_ROUTE, known_extra_cost_usd=None)
        ).allowed
        is True
    )

    exhausted = resolve_fallback_decision(
        policy, _facts(HERV3_ROUTE, budget_used=1)
    )
    assert exhausted.allowed is False
    assert exhausted.blocking_reason == REASON_BUDGET_EXHAUSTED
    assert exhausted.additional_calls_limit == 1

    ceiling = _auto_policy(HERV3_ROUTE, max_extra_cost_usd=0.05)
    over_ceiling = resolve_fallback_decision(
        ceiling, _facts(HERV3_ROUTE, known_extra_cost_usd=0.2)
    )
    assert over_ceiling.allowed is False
    assert over_ceiling.blocking_reason == REASON_BUDGET_EXHAUSTED

    invalid_route_cost = resolve_fallback_decision(
        policy,
        _facts(
            HERV3_ROUTE,
            route_facts={
                HERV3_ROUTE.key(): FallbackRouteFacts(
                    credential_ready=True,
                    capability_ok=True,
                    known_extra_cost_usd=float("nan"),
                )
            },
        ),
    )
    assert invalid_route_cost.allowed is False
    assert invalid_route_cost.blocking_reason == REASON_UNKNOWN_COST


@pytest.mark.parametrize("value", [False, float("nan"), float("inf"), -0.01])
def test_invalid_cost_ceiling_is_rejected(value):
    with pytest.raises(ValueError, match="max_extra_cost_usd"):
        FallbackPolicy(max_extra_cost_usd=value)


def test_ask_mode_binds_one_time_action_and_fails_closed_for_background():
    policy = _auto_policy(HERV3_ROUTE, mode=MODE_ASK)
    facts = _facts(HERV3_ROUTE)
    decision = resolve_fallback_decision(policy, facts, now=1000.0)
    assert decision.allowed is False
    assert decision.blocking_reason == REASON_CONFIRMATION_REQUIRED
    assert decision.disposition == DISPOSITION_ASK_REQUIRED
    action = decision.confirmation
    assert action is not None
    assert action.request_id == facts.request_id
    assert action.route_key == HERV3_ROUTE.key()
    assert action.budget_used_at_issue == 0

    # The bound action authorises exactly this request and route.
    confirmed = resolve_fallback_decision(
        policy,
        _facts(HERV3_ROUTE, confirmation=action),
        now=1001.0,
    )
    assert confirmed.allowed is True
    assert confirmed.route == HERV3_ROUTE

    # After the paid call advances the budget, the same action is stale.
    stale = resolve_fallback_decision(
        policy,
        _facts(HERV3_ROUTE, budget_used=1, confirmation=action),
        now=1002.0,
    )
    assert stale.allowed is False
    assert stale.blocking_reason == REASON_BUDGET_EXHAUSTED

    # An expired action cannot authorise a paid call.
    expired = resolve_fallback_decision(
        policy,
        _facts(HERV3_ROUTE, confirmation=action),
        now=action.expires_at + 1,
    )
    assert expired.allowed is False
    assert expired.blocking_reason == REASON_CONFIRMATION_REQUIRED

    # A duplicate action bound to another request must not match.
    other_request = resolve_fallback_decision(
        policy,
        _facts(HERV3_ROUTE, request_id="req-other", confirmation=action),
        now=1001.0,
    )
    assert other_request.allowed is False
    assert other_request.blocking_reason == REASON_CONFIRMATION_REQUIRED

    # Ask behaves as blocked for scheduler/background work that cannot answer.
    background = resolve_fallback_decision(
        policy,
        _facts(HERV3_ROUTE, interactive=False),
        now=1000.0,
    )
    assert background.allowed is False
    assert background.blocking_reason == REASON_CONFIRMATION_REQUIRED
    assert background.disposition == DISPOSITION_BLOCKED
    assert background.confirmation is None


def test_make_ask_confirmation_is_stable_for_one_request_shape():
    policy = _auto_policy(HERV3_ROUTE, mode=MODE_ASK)
    facts = _facts(HERV3_ROUTE)
    first = make_ask_confirmation(policy, facts, HERV3_ROUTE, now=1000.0)
    second = make_ask_confirmation(policy, facts, HERV3_ROUTE, now=1000.0)
    assert first.action_id == second.action_id


def test_policy_persists_survives_reload_and_honors_revision_conflicts(tmp_path):
    runtime = _runtime(tmp_path)
    policy = _auto_policy(HERV3_ROUTE, mode=MODE_ASK, max_additional_calls=2)
    _, document = read_policy(runtime, "owner-1", for_update=True)
    write_policy(runtime, "owner-1", policy, document=document)

    reloaded, _ = read_policy(runtime, "owner-1")
    assert reloaded.mode == MODE_ASK
    assert reloaded.max_additional_calls == 2
    assert reloaded.routes == (HERV3_ROUTE,)

    # A stale document cannot overwrite a committed change.
    _, stale_document = read_policy(runtime, "owner-1", for_update=True)
    write_policy(
        runtime,
        "owner-1",
        _auto_policy(SECOND_HERV3_ROUTE),
        document=stale_document,
    )
    with pytest.raises(ConfigConflictError):
        write_policy(
            runtime,
            "owner-1",
            _auto_policy(),
            document=stale_document,
        )

    # Other actors and unknown top-level fields are preserved.
    _, fresh = read_policy(runtime, "owner-2", for_update=True)
    write_policy(
        runtime,
        "owner-2",
        _auto_policy(CODEX_TARGET_ROUTE, mode=MODE_AUTO),
        document=fresh,
    )
    payload = json.loads(policy_path(runtime).read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert set(payload["users"]) == {"owner-1", "owner-2"}
    assert payload["users"]["owner-2"]["mode"] == MODE_AUTO
    assert (
        payload["users"]["owner-2"]["routes"][0]["target_kind"]
        == TARGET_KIND_CODEX_CLI
    )


def test_route_validation_rejects_internal_names_and_unknown_kinds():
    with pytest.raises(ValueError):
        FallbackRoute(
            source=SOURCE_HERV3,
            target_kind=TARGET_KIND_HERV3_MODEL,
            provider="her-v2",
            model="deepseek-v4-flash",
        )
    with pytest.raises(ValueError):
        FallbackRoute(source="wrapper", target_kind=TARGET_KIND_CODEX_CLI)
    with pytest.raises(ValueError):
        FallbackRoute(
            source=SOURCE_HERV3,
            target_kind=TARGET_KIND_CODEX_CLI,
            provider="openrouter-api",
        )


def test_policy_defaults_off_and_never_stores_credentials():
    policy = default_policy()
    payload = policy.to_dict()
    assert payload["mode"] == MODE_OFF
    serialized = json.dumps(payload)
    assert "key" not in serialized and "secret" not in serialized


def test_excluded_route_is_never_reused_and_next_route_wins():
    policy = _auto_policy(
        HERV3_ROUTE,
        SECOND_HERV3_ROUTE,
        max_additional_calls=2,
    )
    decision = resolve_fallback_decision(
        policy,
        _facts(
            HERV3_ROUTE,
            excluded_route_keys=frozenset({HERV3_ROUTE.key()}),
            route_facts={
                HERV3_ROUTE.key(): FallbackRouteFacts(
                    credential_ready=True,
                    capability_ok=True,
                    known_extra_cost_usd=0.01,
                ),
                SECOND_HERV3_ROUTE.key(): FallbackRouteFacts(
                    credential_ready=True,
                    capability_ok=True,
                    known_extra_cost_usd=0.01,
                ),
            },
        ),
    )
    assert decision.allowed is True
    assert decision.route == SECOND_HERV3_ROUTE
