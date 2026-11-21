from __future__ import annotations

import json
from dataclasses import replace

import pytest

from tests.test_pricing_sources import NOW, _openrouter_response
from tools import pricing_sources
from tools.token_tracker import record_usage


@pytest.mark.parametrize(("model", "source_model"), [
    ("deepseek-flash", "~deepseek/deepseek-flash-latest"),
    ("deepseek-v4-pro", "~deepseek/deepseek-pro-latest"),
])
def test_deepseek_uses_the_working_openrouter_alias(model, source_model):
    assert pricing_sources.resolve_source_model_id("deepseek-api", model) == source_model
    assert pricing_sources._openrouter_url(source_model).endswith("/model/" + source_model)


def test_unavailable_moving_alias_is_unknown_not_a_historical_price(tmp_path, monkeypatch):
    monkeypatch.setenv("HASHI_PRICING_CACHE_FILE", str(tmp_path / "pricing.json"))
    monkeypatch.setattr(pricing_sources, "schedule_prewarm", lambda *a, **kw: None)
    receipt = record_usage(
        tmp_path, model="deepseek-v4-pro", backend="deepseek-api", engine="deepseek-api",
        input_tokens=2000, output_tokens=1000, token_source="provider",
    )
    assert receipt.cost_usd is None
    assert receipt.dominant_cost_source() == "unknown"


def test_response_with_different_id_is_not_accepted_as_alias_price():
    source = "~deepseek/deepseek-flash-latest"
    evidence = _openrouter_response(source)
    payload = json.loads(evidence.body)
    payload["data"]["id"] = "other/unrelated-model"
    payload["data"]["canonical_slug"] = source
    evidence = replace(evidence, body=json.dumps(payload).encode())
    with pytest.raises(pricing_sources.PricingSourceError, match="model_id_mismatch"):
        pricing_sources._openrouter_fact(
            "deepseek-api", "deepseek-flash", source, evidence, now=NOW
        )
