from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from tools import pricing_sources


NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def _openrouter_response(
    model: str = "vendor/new-model",
    *,
    prompt: str = "0.000002",
    completion: str = "0.000006",
    cache_read: str | None = "0.0000002",
    cache_write: str | None = "0.0000025",
    request: str | None = "0",
    canonical_model: str | None = None,
    overrides: list[dict] | None = None,
    fetched_at: datetime = NOW,
) -> pricing_sources.HttpEvidence:
    pricing = {
        "prompt": prompt,
        "completion": completion,
    }
    if request is not None:
        pricing["request"] = request
    if cache_read is not None:
        pricing["input_cache_read"] = cache_read
    if cache_write is not None:
        pricing["input_cache_write"] = cache_write
    if overrides is not None:
        pricing["overrides"] = overrides
    body = json.dumps(
        {
            "data": {
                "id": model,
                "canonical_slug": canonical_model or model,
                "pricing": pricing,
            }
        }
    ).encode()
    return pricing_sources.HttpEvidence(
        status=200,
        content_type="application/json; charset=utf-8",
        body=body,
        fetched_at=fetched_at,
        url=f"https://openrouter.ai/api/v1/model/{model}",
    )


def test_openrouter_exact_route_fact_has_complete_provenance_and_units(tmp_path):
    cache = tmp_path / "pricing.json"

    fact = pricing_sources.refresh_pricing_fact(
        "openrouter-api",
        "vendor/new-model",
        cache_path=cache,
        fetcher=lambda _url: _openrouter_response(),
        now=NOW,
    )

    assert fact.status == "known"
    assert fact.scope == "openrouter_route"
    assert fact.engine == "openrouter-api"
    assert fact.requested_model_id == "vendor/new-model"
    assert fact.canonical_model_id == "vendor/new-model"
    assert fact.currency == "USD"
    assert fact.unit == "token"
    assert fact.input_per_unit == 0.000002
    assert fact.output_per_unit == 0.000006
    assert fact.cache_read_per_unit == 0.0000002
    assert fact.cache_write_per_unit == 0.0000025
    assert fact.request_per_unit == 0.0
    assert fact.revision_kind == "content_sha256"
    assert fact.source_revision.startswith("openrouter:sha256:")
    assert fact.evidence_sha256 in fact.source_revision
    assert fact.source_url.endswith("/vendor/new-model")
    assert fact.unit_source_url == "https://openrouter.ai/openapi.json"
    assert fact.fetched_at == NOW.isoformat()
    assert fact.expires_at == (NOW + timedelta(hours=24)).isoformat()


def test_openrouter_alias_keeps_exact_route_id_and_records_canonical_slug(tmp_path):
    fact = pricing_sources.refresh_pricing_fact(
        "openrouter-api",
        "anthropic/claude-sonnet-4.6",
        cache_path=tmp_path / "pricing.json",
        fetcher=lambda _url: _openrouter_response(
            "anthropic/claude-sonnet-4.6",
            canonical_model="anthropic/claude-4.6-sonnet-20260217",
            request=None,
        ),
        now=NOW,
    )

    assert fact.status == "known"
    assert fact.requested_model_id == "anthropic/claude-sonnet-4.6"
    assert fact.canonical_model_id == "anthropic/claude-4.6-sonnet-20260217"
    assert fact.request_per_unit is None
    assert pricing_sources.calculate_cost(
        fact, input_tokens=1_000, output_tokens=100
    ) == 0.0026


def test_openrouter_response_must_match_requested_exact_route_id(tmp_path):
    fact = pricing_sources.refresh_pricing_fact(
        "openrouter-api",
        "vendor/requested",
        cache_path=tmp_path / "pricing.json",
        fetcher=lambda _url: _openrouter_response("vendor/different"),
        now=NOW,
    )

    assert fact.status == "unknown"
    assert fact.unknown_reason == "model_id_mismatch"


def test_all_zero_price_requires_explicit_free_route(tmp_path):
    fact = pricing_sources.refresh_pricing_fact(
        "openrouter-api",
        "vendor/zero-but-not-free",
        cache_path=tmp_path / "pricing.json",
        fetcher=lambda _url: _openrouter_response(
            "vendor/zero-but-not-free",
            prompt="0",
            completion="0",
            cache_read="0",
            cache_write="0",
            request="0",
        ),
        now=NOW,
    )

    assert fact.status == "unknown"
    assert fact.unknown_reason == "unproven_zero_price"


def test_conditional_price_overrides_fail_closed_until_tiers_are_supported(tmp_path):
    fact = pricing_sources.refresh_pricing_fact(
        "openrouter-api",
        "vendor/tiered",
        cache_path=tmp_path / "pricing.json",
        fetcher=lambda _url: _openrouter_response(
            "vendor/tiered",
            overrides=[{"min_prompt_tokens": 200_000, "prompt": "0.000004"}],
        ),
        now=NOW,
    )

    assert fact.status == "unknown"
    assert fact.unknown_reason == "unsupported_pricing_overrides"


def test_cache_only_usage_never_fetches_and_expired_fact_is_diagnostic_only(tmp_path):
    cache = tmp_path / "pricing.json"
    pricing_sources.refresh_pricing_fact(
        "openrouter-api",
        "vendor/new-model",
        cache_path=cache,
        fetcher=lambda _url: _openrouter_response(),
        now=NOW,
    )

    fresh = pricing_sources.get_cached_pricing_fact(
        "openrouter-api", "vendor/new-model", cache_path=cache, now=NOW
    )
    expired = pricing_sources.get_cached_pricing_fact(
        "openrouter-api",
        "vendor/new-model",
        cache_path=cache,
        now=NOW + timedelta(hours=25),
    )

    assert fresh.status == "known"
    assert expired.status == "unknown"
    assert expired.unknown_reason == "stale_cache"
    assert expired.last_known_revision == fresh.source_revision
    assert expired.input_per_unit is None
    assert expired.output_per_unit is None


def test_openrouter_route_never_populates_a_direct_provider_key(tmp_path):
    cache = tmp_path / "pricing.json"
    pricing_sources.refresh_pricing_fact(
        "openrouter-api",
        "anthropic/claude-new",
        cache_path=cache,
        fetcher=lambda _url: _openrouter_response("anthropic/claude-new"),
        now=NOW,
    )

    direct = pricing_sources.get_cached_pricing_fact(
        "anthropic-api", "anthropic/claude-new", cache_path=cache, now=NOW
    )

    assert direct.status == "unknown"
    assert direct.unknown_reason == "cache_miss"


@pytest.mark.parametrize("failure", ["timeout", "network_error", "429", "503"])
def test_transient_failure_is_negative_cached_for_fifteen_minutes(
    tmp_path, failure
):
    cache = tmp_path / "pricing.json"

    def fail(_url):
        if failure == "timeout":
            raise TimeoutError("synthetic timeout")
        if failure == "network_error":
            raise pricing_sources.PricingSourceError("network_error")
        return pricing_sources.HttpEvidence(
            status=int(failure),
            content_type="application/json",
            body=b"{}",
            fetched_at=NOW,
            url="https://openrouter.ai/api/v1/model/vendor/new-model",
        )

    fact = pricing_sources.refresh_pricing_fact(
        "openrouter-api",
        "vendor/new-model",
        cache_path=cache,
        fetcher=fail,
        now=NOW,
    )
    called = False

    def should_not_fetch(_url):
        nonlocal called
        called = True
        return _openrouter_response()

    cached = pricing_sources.refresh_pricing_fact(
        "openrouter-api",
        "vendor/new-model",
        cache_path=cache,
        fetcher=should_not_fetch,
        now=NOW + timedelta(minutes=14),
    )

    assert fact.status == "unknown"
    assert fact.expires_at == (NOW + timedelta(minutes=15)).isoformat()
    assert cached.unknown_reason == fact.unknown_reason
    assert called is False


def test_expired_success_is_refreshed_instead_of_used_as_stale_price(tmp_path):
    cache = tmp_path / "pricing.json"
    first = pricing_sources.refresh_pricing_fact(
        "openrouter-api",
        "vendor/new-model",
        cache_path=cache,
        fetcher=lambda _url: _openrouter_response(prompt="0.000002"),
        now=NOW,
    )
    refreshed_at = NOW + timedelta(hours=25)
    calls = 0

    def refresh(_url):
        nonlocal calls
        calls += 1
        return _openrouter_response(prompt="0.000003", fetched_at=refreshed_at)

    second = pricing_sources.refresh_pricing_fact(
        "openrouter-api",
        "vendor/new-model",
        cache_path=cache,
        fetcher=refresh,
        now=refreshed_at,
    )

    assert calls == 1
    assert first.input_per_unit == 0.000002
    assert second.input_per_unit == 0.000003
    assert second.source_revision != first.source_revision


@pytest.mark.parametrize(
    ("evidence", "reason"),
    [
        (
            pricing_sources.HttpEvidence(
                status=200,
                content_type="text/html",
                body=b"<html></html>",
                fetched_at=NOW,
                url="https://openrouter.ai/api/v1/model/vendor/new-model",
            ),
            "unsupported_content_type",
        ),
        (
            pricing_sources.HttpEvidence(
                status=200,
                content_type="application/json",
                body=b"{truncated",
                fetched_at=NOW,
                url="https://openrouter.ai/api/v1/model/vendor/new-model",
            ),
            "malformed_json",
        ),
        (
            pricing_sources.HttpEvidence(
                status=200,
                content_type="application/json",
                body=b"x" * (pricing_sources.MAX_RESPONSE_BYTES + 1),
                fetched_at=NOW,
                url="https://openrouter.ai/api/v1/model/vendor/new-model",
            ),
            "response_too_large",
        ),
    ],
)
def test_mime_malformed_and_oversize_responses_fail_closed(tmp_path, evidence, reason):
    fact = pricing_sources.refresh_pricing_fact(
        "openrouter-api",
        "vendor/new-model",
        cache_path=tmp_path / "pricing.json",
        fetcher=lambda _url: evidence,
        now=NOW,
    )
    assert fact.status == "unknown"
    assert fact.unknown_reason == reason


def test_incomplete_dimensions_only_block_usage_that_needs_them(tmp_path):
    cache = tmp_path / "pricing.json"
    fact = pricing_sources.refresh_pricing_fact(
        "openrouter-api",
        "vendor/no-cache-price",
        cache_path=cache,
        fetcher=lambda _url: _openrouter_response(
            "vendor/no-cache-price", cache_read=None, cache_write=None
        ),
        now=NOW,
    )

    assert fact.status == "known"
    assert pricing_sources.calculate_cost(
        fact, input_tokens=1_000, output_tokens=100, cached_tokens=0
    ) == 0.0026
    assert (
        pricing_sources.calculate_cost(
            fact, input_tokens=1_000, output_tokens=100, cached_tokens=10
        )
        is None
    )


def test_known_zero_is_not_confused_with_unknown(tmp_path):
    fact = pricing_sources.refresh_pricing_fact(
        "openrouter-api",
        "vendor/free-model:free",
        cache_path=tmp_path / "pricing.json",
        fetcher=lambda _url: _openrouter_response(
            "vendor/free-model:free",
            prompt="0",
            completion="0",
            cache_read="0",
            cache_write="0",
            request="0",
        ),
        now=NOW,
    )
    assert fact.status == "known_zero"
    assert pricing_sources.calculate_cost(
        fact, input_tokens=100, output_tokens=20, cached_tokens=10
    ) == 0.0


def test_persisted_fact_survives_module_style_restart(tmp_path):
    cache = tmp_path / "pricing.json"
    first = pricing_sources.refresh_pricing_fact(
        "openrouter-api",
        "vendor/new-model",
        cache_path=cache,
        fetcher=lambda _url: _openrouter_response(),
        now=NOW,
    )
    second = pricing_sources.get_cached_pricing_fact(
        "openrouter-api", "vendor/new-model", cache_path=cache, now=NOW
    )
    assert second == first


def test_tampered_or_non_finite_cached_fact_is_never_used(tmp_path):
    cache = tmp_path / "pricing.json"
    pricing_sources.refresh_pricing_fact(
        "openrouter-api",
        "vendor/new-model",
        cache_path=cache,
        fetcher=lambda _url: _openrouter_response(),
        now=NOW,
    )
    payload = json.loads(cache.read_text(encoding="utf-8"))
    [stored] = payload["facts"].values()
    stored["input_per_unit"] = float("nan")
    cache.write_text(json.dumps(payload), encoding="utf-8")

    fact = pricing_sources.get_cached_pricing_fact(
        "openrouter-api", "vendor/new-model", cache_path=cache, now=NOW
    )

    assert fact.status == "unknown"
    assert fact.unknown_reason == "cache_miss"


def test_concurrent_refreshes_coalesce_to_one_fetch(tmp_path):
    cache = tmp_path / "pricing.json"
    calls = 0
    calls_lock = threading.Lock()

    def fetch(_url):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return _openrouter_response()

    results = []

    def run():
        results.append(
            pricing_sources.refresh_pricing_fact(
                "openrouter-api",
                "vendor/new-model",
                cache_path=cache,
                fetcher=fetch,
                now=NOW,
            )
        )

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == 1
    assert len(results) == 4
    assert {item.source_revision for item in results} == {
        results[0].source_revision
    }


def test_successful_model_configuration_schedules_prewarm_without_owning_price(
    tmp_path, monkeypatch
):
    from orchestrator import runtime_model_selection

    calls = []
    monkeypatch.setattr(
        pricing_sources,
        "schedule_prewarm",
        lambda engine, model, *, cache_path=None: calls.append(
            (engine, model, cache_path)
        ),
    )
    selected = SimpleNamespace(
        fast_provider="openrouter-api",
        fast_model="vendor/fast",
        pro_provider="openrouter-api",
        pro_model="vendor/pro",
        route_targets={},
    )
    manager = SimpleNamespace(
        apply_her_v2_configuration=lambda value: None,
    )
    runtime = SimpleNamespace(
        config=SimpleNamespace(active_backend="her-v2"),
        backend_manager=manager,
        global_config=SimpleNamespace(bridge_home=tmp_path),
    )

    assert runtime_model_selection.apply_her_v2_configuration(runtime, selected) is None
    assert calls == [
        ("openrouter-api", "vendor/fast", tmp_path / "tmp" / "pricing-facts-v1.json"),
        ("openrouter-api", "vendor/pro", tmp_path / "tmp" / "pricing-facts-v1.json"),
    ]


def test_pricing_prewarm_failure_never_rolls_back_model_configuration(
    tmp_path, monkeypatch
):
    from orchestrator import runtime_model_selection

    monkeypatch.setattr(
        pricing_sources,
        "schedule_prewarm",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    selected = SimpleNamespace(
        fast_provider="openrouter-api",
        fast_model="vendor/fast",
        pro_provider="openrouter-api",
        pro_model="vendor/pro",
        route_targets={},
    )
    applied = []
    runtime = SimpleNamespace(
        config=SimpleNamespace(active_backend="her-v2"),
        backend_manager=SimpleNamespace(
            apply_her_v2_configuration=lambda value: applied.append(value)
        ),
        global_config=SimpleNamespace(bridge_home=tmp_path),
    )

    assert runtime_model_selection.apply_her_v2_configuration(runtime, selected) is None
    assert applied == [selected]
