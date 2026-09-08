import json
from datetime import datetime, timezone

import pytest

from tools import pricing_sources
from tools.token_tracker import PRICING, calc_cost, get_price, record_usage


def test_qwen37_flash_uses_exact_openrouter_slug_and_base_price():
    assert get_price("qwen/qwen3.7-flash", input_tokens=31_999) == {
        "input": 0.03,
        "cached": 0.006,
        "output": 0.13,
    }
    assert calc_cost(1_000, 1_000, "qwen/qwen3.7-flash") == pytest.approx(0.00016)


@pytest.mark.parametrize(
    ("input_tokens", "input_price", "cached_price", "output_price"),
    [
        (32_000, 0.10, 0.02, 0.40),
        (255_999, 0.10, 0.02, 0.40),
        (256_000, 0.20, 0.04, 0.80),
    ],
)
def test_qwen37_flash_applies_openrouter_prompt_length_tiers(
    input_tokens, input_price, cached_price, output_price
):
    assert get_price("qwen/qwen3.7-flash", input_tokens=input_tokens) == {
        "input": input_price,
        "cached": cached_price,
        "output": output_price,
    }


def test_qwen37_flash_tiered_cost_uses_matching_cached_rate():
    assert calc_cost(
        32_000,
        1_000,
        "qwen/qwen3.7-flash",
        cached_tokens=10_000,
    ) == pytest.approx(0.0028)


def test_glm53_uses_openrouter_prices():
    assert get_price("z-ai/glm-5.3") == {
        "input": 1.40,
        "cached": 0.26,
        "output": 4.40,
    }
    assert calc_cost(1_000_000, 1_000_000, "z-ai/glm-5.3") == pytest.approx(5.8)


@pytest.mark.parametrize(
    ("model", "base", "large_prompt"),
    [
        (
            "gpt-5.6-luna",
            {"input": 0.20, "cached": 0.02, "output": 1.20},
            {"input": 0.40, "cached": 0.04, "output": 1.80},
        ),
        (
            "gpt-5.6-sol",
            {"input": 2.00, "cached": 0.20, "output": 10.00},
            {"input": 4.00, "cached": 0.40, "output": 15.00},
        ),
    ],
)
def test_gpt56_openrouter_prices_switch_only_above_272k(
    model, base, large_prompt
):
    assert get_price(model, input_tokens=272_000) == base
    assert get_price(model, input_tokens=272_001) == large_prompt


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-5.6-luna", 0.102),
        ("gpt-5.6-sol", 0.99),
    ],
)
def test_gpt56_large_prompt_cost_uses_matching_cached_rate(model, expected):
    assert calc_cost(
        300_000,
        10_000,
        model,
        cached_tokens=100_000,
    ) == pytest.approx(expected)


def test_provider_reasoning_tokens_are_not_charged_twice():
    assert calc_cost(
        1_000,
        500,
        "gpt-5.4",
        thinking_tokens=300,
        thinking_in_output=True,
    ) == pytest.approx(0.01)


def test_estimated_thinking_tokens_remain_separate_output():
    assert calc_cost(
        1_000,
        500,
        "gpt-5.4",
        thinking_tokens=300,
        thinking_in_output=False,
    ) == pytest.approx(0.0145)


def test_pricing_lookup_does_not_fuzzy_match_partial_model_names():
    assert get_price("gpt-5") == PRICING["default"]
    assert get_price("anthropic/claude-sonnet-4-6") == PRICING[
        "claude-sonnet-4-6"
    ]


def test_deepseek_flash_vision_uses_flash_pricing():
    assert get_price("deepseek-v4-flash-vision-exp") == PRICING[
        "deepseek-v4-flash"
    ]


def _dynamic_evidence(model="vendor/new-model", *, cache_read="0.0000002"):
    body = json.dumps(
        {
            "data": {
                "id": model,
                "canonical_slug": model,
                "pricing": {
                    "prompt": "0.000002",
                    "completion": "0.000006",
                    "request": "0",
                    "input_cache_read": cache_read,
                    "input_cache_write": "0.0000025",
                },
            }
        }
    ).encode()
    return pricing_sources.HttpEvidence(
        status=200,
        content_type="application/json",
        body=body,
        fetched_at=datetime.now(timezone.utc),
        url=f"https://openrouter.ai/api/v1/model/{model}",
    )


def test_record_usage_reads_dynamic_price_cache_and_links_exact_revision(
    tmp_path, monkeypatch
):
    cache = tmp_path / "pricing-cache.json"
    monkeypatch.setenv("HASHI_PRICING_CACHE_FILE", str(cache))
    fact = pricing_sources.refresh_pricing_fact(
        "openrouter-api",
        "vendor/new-model",
        cache_path=cache,
        fetcher=lambda _url: _dynamic_evidence(),
    )

    receipt = record_usage(
        tmp_path,
        model="vendor/new-model",
        backend="openrouter-api",
        engine="openrouter-api",
        input_tokens=1_000,
        output_tokens=100,
        token_source="provider",
    )

    assert receipt.cost_usd == 0.0026
    assert receipt.dominant_cost_source() == "pricing_table"
    assert receipt.pricing_revisions == (fact.source_revision,)
    [record] = [json.loads(line) for line in (tmp_path / "token_usage.jsonl").read_text().splitlines()]
    assert record["pricing_revisions"] == [fact.source_revision]


def test_provider_reported_zero_beats_dynamic_estimate(tmp_path, monkeypatch):
    cache = tmp_path / "pricing-cache.json"
    monkeypatch.setenv("HASHI_PRICING_CACHE_FILE", str(cache))
    pricing_sources.refresh_pricing_fact(
        "openrouter-api",
        "vendor/new-model",
        cache_path=cache,
        fetcher=lambda _url: _dynamic_evidence(),
    )

    receipt = record_usage(
        tmp_path,
        model="vendor/new-model",
        backend="openrouter-api",
        engine="openrouter-api",
        input_tokens=1_000,
        output_tokens=100,
        cost_usd=0.0,
        token_source="provider",
    )

    assert receipt.cost_usd == 0.0
    assert receipt.dominant_cost_source() == "provider"
    assert receipt.pricing_revisions == ()


def test_dynamic_route_price_is_not_used_for_direct_provider(tmp_path, monkeypatch):
    cache = tmp_path / "pricing-cache.json"
    monkeypatch.setenv("HASHI_PRICING_CACHE_FILE", str(cache))
    pricing_sources.refresh_pricing_fact(
        "openrouter-api",
        "vendor/new-model",
        cache_path=cache,
        fetcher=lambda _url: _dynamic_evidence(),
    )

    receipt = record_usage(
        tmp_path,
        model="vendor/new-model",
        backend="vendor-api",
        engine="vendor-api",
        input_tokens=1_000,
        output_tokens=100,
        cost_usd=None,
        token_source="provider",
    )

    assert receipt.cost_usd is None
    assert receipt.dominant_cost_source() == "unknown"


def test_usage_finalization_is_cache_only(tmp_path, monkeypatch):
    cache = tmp_path / "pricing-cache.json"
    monkeypatch.setenv("HASHI_PRICING_CACHE_FILE", str(cache))
    pricing_sources.refresh_pricing_fact(
        "openrouter-api",
        "vendor/new-model",
        cache_path=cache,
        fetcher=lambda _url: _dynamic_evidence(),
    )
    monkeypatch.setattr(
        pricing_sources,
        "bounded_https_get",
        lambda _url: pytest.fail("usage finalization must not access the network"),
    )

    receipt = record_usage(
        tmp_path,
        model="vendor/new-model",
        backend="openrouter-api",
        engine="openrouter-api",
        input_tokens=10,
        output_tokens=1,
        token_source="provider",
    )
    assert receipt.cost_usd is not None


def test_first_unknown_openrouter_usage_schedules_nonblocking_price_supplement(
    tmp_path, monkeypatch
):
    cache = tmp_path / "pricing-cache.json"
    monkeypatch.setenv("HASHI_PRICING_CACHE_FILE", str(cache))
    scheduled = []
    monkeypatch.setattr(
        pricing_sources,
        "schedule_prewarm",
        lambda engine, model, *, cache_path=None: scheduled.append(
            (engine, model, cache_path)
        ),
    )

    receipt = record_usage(
        tmp_path,
        model="vendor/brand-new-model",
        backend="openrouter-api",
        engine="openrouter-api",
        input_tokens=10,
        output_tokens=1,
        token_source="provider",
    )

    assert receipt.cost_usd is None
    assert receipt.dominant_cost_source() == "unknown"
    assert scheduled == [("openrouter-api", "vendor/brand-new-model", None)]
