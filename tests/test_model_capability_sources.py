from __future__ import annotations

import base64
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from adapters.her_v2_provider import HashiStageProvider
from adapters.openrouter_api import OpenRouterAdapter
from orchestrator.her_v2.config import ProviderProfile
from orchestrator import runtime_model_selection
from orchestrator.api_gateway import _validate_structured_conversation
from orchestrator.multimodal_contract import resolve_input_capability
from tests.mocks.mock_adapters import SimpleGlobalConfig, SimpleTestConfig
from tools import model_capability_sources, pricing_sources


NOW = datetime(2026, 9, 10, 1, 0, tzinfo=timezone.utc)


def _evidence(
    source_model: str = "openai/gpt-6-astra",
    *,
    inputs: list[str] | None = None,
    outputs: list[str] | None = None,
    response_model: str | None = None,
    include_architecture: bool = True,
    pricing: object = None,
    status: int = 200,
    fetched_at: datetime = NOW,
) -> pricing_sources.HttpEvidence:
    data: dict[str, object] = {
        "id": response_model or source_model,
        "canonical_slug": source_model,
    }
    if include_architecture:
        data["architecture"] = {
            "input_modalities": inputs if inputs is not None else ["file", "image", "text"],
            "output_modalities": outputs if outputs is not None else ["text"],
        }
    if pricing is not None:
        data["pricing"] = pricing
    return pricing_sources.HttpEvidence(
        status=status,
        content_type="application/json; charset=utf-8",
        body=json.dumps({"data": data}).encode("utf-8"),
        fetched_at=fetched_at,
        url=f"https://openrouter.ai/api/v1/model/{source_model}",
    )


def test_exact_codex_mapping_discovers_image_without_treating_file_as_document(
    tmp_path,
):
    cache = tmp_path / "capabilities.json"

    fact = model_capability_sources.refresh_capability_fact(
        "codex-cli",
        "gpt-6-astra",
        cache_path=cache,
        fetcher=lambda _url: _evidence(),
        now=NOW,
    )
    capability = resolve_input_capability(
        "codex-cli",
        "gpt-6-astra",
        capability_cache_path=cache,
    )

    assert fact.status == "known"
    assert fact.source_model_id == "openai/gpt-6-astra"
    assert fact.canonical_model_id == "openai/gpt-6-astra"
    assert fact.input_status("image") == "supported"
    assert fact.input_status("file") == "supported"
    assert fact.input_status("document") == "unknown"
    assert fact.output_status("text") == "supported"
    assert fact.source_url.endswith("/openai/gpt-6-astra")
    assert fact.fetched_at == NOW.isoformat()
    assert fact.expires_at == (NOW + timedelta(hours=24)).isoformat()
    assert fact.source_revision and fact.evidence_sha256 in fact.source_revision
    assert capability.supports("image", "local_path") is True
    assert capability.supports("document") is False
    assert capability.output_modalities == frozenset({"text"})
    assert capability.source == "dynamic_capability_cache"


def test_confirmed_text_only_model_is_unsupported_not_unknown(tmp_path):
    cache = tmp_path / "capabilities.json"
    model_capability_sources.refresh_capability_fact(
        "openrouter-api",
        "vendor/text-only",
        cache_path=cache,
        fetcher=lambda _url: _evidence(
            "vendor/text-only",
            inputs=["text"],
        ),
        now=NOW,
    )

    capability = resolve_input_capability(
        "openrouter-api",
        "vendor/text-only",
        capability_cache_path=cache,
    )

    assert capability.status_for("image") == "unsupported"
    assert capability.supports("image") is False


def test_catalogue_video_and_file_do_not_invent_unimplemented_routes(tmp_path):
    cache = tmp_path / "capabilities.json"
    model_capability_sources.refresh_capability_fact(
        "openrouter-api",
        "vendor/video-catalogue-model",
        cache_path=cache,
        fetcher=lambda _url: _evidence(
            "vendor/video-catalogue-model",
            inputs=["text", "video", "file"],
        ),
        now=NOW,
    )

    capability = resolve_input_capability(
        "openrouter-api",
        "vendor/video-catalogue-model",
        capability_cache_path=cache,
    )

    assert capability.status_for("video") == "supported"
    assert capability.supports("video") is False
    assert capability.status_for("document") == "unknown"
    assert capability.supports("document") is False


def test_output_file_fact_remains_distinct_from_input_document(tmp_path):
    cache = tmp_path / "capabilities.json"
    model_capability_sources.refresh_capability_fact(
        "openrouter-api",
        "vendor/file-output",
        cache_path=cache,
        fetcher=lambda _url: _evidence(
            "vendor/file-output",
            inputs=["text"],
            outputs=["file"],
        ),
        now=NOW,
    )

    capability = resolve_input_capability(
        "openrouter-api",
        "vendor/file-output",
        capability_cache_path=cache,
    )

    assert capability.output_modalities == frozenset({"file"})
    assert capability.output_status_for("file") == "supported"
    assert capability.output_status_for("document") == "supported"
    assert capability.status_for("document") == "unknown"


def test_message_path_is_cache_only_when_capability_is_missing(tmp_path, monkeypatch):
    def forbidden_fetch(_url):
        raise AssertionError("message-time capability resolution attempted network I/O")

    monkeypatch.setattr(
        model_capability_sources,
        "shared_bounded_https_get",
        forbidden_fetch,
    )

    capability = resolve_input_capability(
        "codex-cli",
        "gpt-6-astra",
        capability_cache_path=tmp_path / "missing.json",
    )

    assert capability.source == "dynamic_unknown"
    assert capability.status_for("image") == "unknown"


@pytest.mark.parametrize(
    ("fetch", "reason"),
    [
        (lambda: _evidence(status=404), "http_404"),
        (
            lambda: _evidence(include_architecture=False),
            "missing_capability_field",
        ),
        (
            lambda: _evidence(response_model="openai/different"),
            "model_id_mismatch",
        ),
    ],
)
def test_unproven_catalogue_results_remain_unknown(tmp_path, fetch, reason):
    fact = model_capability_sources.refresh_capability_fact(
        "codex-cli",
        "gpt-6-astra",
        cache_path=tmp_path / "capabilities.json",
        fetcher=lambda _url: fetch(),
        now=NOW,
    )

    assert fact.status == "unknown"
    assert fact.input_status("image") == "unknown"
    assert fact.unknown_reason == reason


def test_timeout_is_unknown_and_negative_cached(tmp_path):
    cache = tmp_path / "capabilities.json"

    def timeout(_url):
        raise TimeoutError("synthetic")

    first = model_capability_sources.refresh_capability_fact(
        "codex-cli",
        "gpt-6-astra",
        cache_path=cache,
        fetcher=timeout,
        now=NOW,
    )
    called = False

    def should_not_fetch(_url):
        nonlocal called
        called = True
        return _evidence()

    second = model_capability_sources.refresh_capability_fact(
        "codex-cli",
        "gpt-6-astra",
        cache_path=cache,
        fetcher=should_not_fetch,
        now=NOW + timedelta(minutes=14),
    )

    assert first.unknown_reason == "timeout"
    assert second.unknown_reason == "timeout"
    assert called is False


def test_network_failure_is_unknown_not_unsupported(tmp_path):
    def fail(_url):
        raise pricing_sources.PricingSourceError("network_error")

    fact = model_capability_sources.refresh_capability_fact(
        "codex-cli",
        "gpt-6-astra",
        cache_path=tmp_path / "capabilities.json",
        fetcher=fail,
        now=NOW,
    )

    assert fact.status == "unknown"
    assert fact.unknown_reason == "network_error"
    assert fact.input_status("image") == "unknown"


def test_alias_ambiguity_fails_closed_before_fetch(tmp_path):
    called = False

    def fetch(_url):
        nonlocal called
        called = True
        return _evidence()

    fact = model_capability_sources.refresh_capability_fact(
        "codex-cli",
        "future-model",
        cache_path=tmp_path / "capabilities.json",
        source_mappings={
            ("codex-cli", "future-model"): (
                "openai/future-a",
                "openai/future-b",
            )
        },
        fetcher=fetch,
        now=NOW,
    )

    assert fact.status == "unknown"
    assert fact.unknown_reason == "source_alias_ambiguous"
    assert called is False


def test_exact_manual_override_has_priority_over_dynamic_fact(tmp_path):
    cache = tmp_path / "capabilities.json"
    model_capability_sources.refresh_capability_fact(
        "openrouter-api",
        "vendor/text-only",
        cache_path=cache,
        fetcher=lambda _url: _evidence(
            "vendor/text-only",
            inputs=["text"],
        ),
        now=NOW,
    )

    capability = resolve_input_capability(
        "openrouter-api",
        "vendor/text-only",
        config={
            "input_modalities": ["text", "image"],
            "input_transports": {"image": ["data_url"]},
        },
        capability_cache_path=cache,
    )

    assert capability.source == "explicit_config"
    assert capability.supports("image", "data_url") is True


def test_stale_last_known_fact_is_preserved_but_does_not_grant_media(tmp_path):
    cache = tmp_path / "capabilities.json"
    old_now = datetime.now(timezone.utc) - timedelta(days=2)
    known = model_capability_sources.refresh_capability_fact(
        "codex-cli",
        "gpt-6-astra",
        cache_path=cache,
        fetcher=lambda _url: _evidence(fetched_at=old_now),
        now=old_now,
    )

    stale = model_capability_sources.get_cached_capability_fact(
        "codex-cli",
        "gpt-6-astra",
        cache_path=cache,
        now=old_now + timedelta(hours=25),
    )
    capability = resolve_input_capability(
        "codex-cli",
        "gpt-6-astra",
        capability_cache_path=cache,
    )

    assert stale.status == "unknown"
    assert stale.stale is True
    assert stale.last_known_revision == known.source_revision
    assert stale.last_known_input_modalities["image"] == "supported"
    # The real clock is beyond the synthetic fact's expiry, so routing fails
    # closed even though diagnostic last-known data remains available.
    assert capability.status_for("image") == "unknown"
    assert capability.supports("image") is False


def test_concurrent_capability_refreshes_coalesce_to_one_fetch(tmp_path):
    cache = tmp_path / "capabilities.json"
    calls = 0
    calls_lock = threading.Lock()

    def fetch(_url):
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return _evidence()

    results = []

    def run():
        results.append(
            model_capability_sources.refresh_capability_fact(
                "codex-cli",
                "gpt-6-astra",
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
    assert {fact.source_revision for fact in results} == {known.source_revision for known in results}


def test_different_models_fetch_concurrently_without_holding_cache_lock(tmp_path):
    cache = tmp_path / "capabilities.json"
    both_fetching = threading.Event()
    release = threading.Event()
    started: list[str] = []
    guard = threading.Lock()
    results = []

    def fetch(url):
        model = url.removeprefix("https://openrouter.ai/api/v1/model/")
        with guard:
            started.append(model)
            if len(started) == 2:
                both_fetching.set()
        release.wait(timeout=3)
        return _evidence(model)

    def run(model):
        results.append(
            model_capability_sources.refresh_capability_fact(
                "openrouter-api",
                model,
                cache_path=cache,
                fetcher=fetch,
                now=NOW,
            )
        )

    threads = [
        threading.Thread(target=run, args=("vendor/model-a",)),
        threading.Thread(target=run, args=("vendor/model-b",)),
    ]
    for thread in threads:
        thread.start()
    try:
        assert both_fetching.wait(timeout=1)
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=3)

    assert sorted(started) == ["vendor/model-a", "vendor/model-b"]
    assert len(results) == 2
    assert {fact.requested_model_id for fact in results} == {
        "vendor/model-a",
        "vendor/model-b",
    }


def test_duplicate_async_prewarm_keeps_every_completion_callback(
    tmp_path,
    monkeypatch,
):
    started = threading.Event()
    release = threading.Event()
    completed: list[str] = []
    fact = SimpleNamespace(engine="codex-cli", requested_model_id="gpt-6-astra")

    def refresh(*_args, **_kwargs):
        started.set()
        release.wait(timeout=3)
        return fact

    monkeypatch.setattr(model_capability_sources, "refresh_capability_fact", refresh)
    first = model_capability_sources.schedule_prewarm(
        "codex-cli",
        "gpt-6-astra",
        cache_path=tmp_path / "capabilities.json",
        on_complete=lambda _fact: completed.append("first"),
    )
    assert started.wait(timeout=1)
    second = model_capability_sources.schedule_prewarm(
        "codex-cli",
        "gpt-6-astra",
        cache_path=tmp_path / "capabilities.json",
        on_complete=lambda _fact: completed.append("second"),
    )
    release.set()
    deadline = time.monotonic() + 3
    while len(completed) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert first is True
    assert second is False
    assert completed == ["first", "second"]


def test_pricing_failure_does_not_discard_capability_from_shared_evidence(
    tmp_path,
    monkeypatch,
):
    model = "vendor/capability-with-bad-price"
    evidence = _evidence(
        model,
        pricing={"prompt": "not-a-rate", "completion": "0.000001"},
    )
    calls = 0
    barrier = threading.Barrier(2)

    def fetch(_url):
        nonlocal calls
        calls += 1
        time.sleep(0.05)
        return evidence

    monkeypatch.setattr(pricing_sources, "bounded_https_get", fetch)
    pricing_result = []
    capability_result = []

    def pricing_run():
        barrier.wait()
        pricing_result.append(
            pricing_sources.refresh_pricing_fact(
                "openrouter-api",
                model,
                cache_path=tmp_path / "pricing.json",
                now=NOW,
                force=True,
            )
        )

    def capability_run():
        barrier.wait()
        capability_result.append(
            model_capability_sources.refresh_capability_fact(
                "openrouter-api",
                model,
                cache_path=tmp_path / "capabilities.json",
                now=NOW,
                force=True,
            )
        )

    threads = [threading.Thread(target=pricing_run), threading.Thread(target=capability_run)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == 1
    assert pricing_result[0].status == "unknown"
    assert pricing_result[0].unknown_reason == "invalid_pricing_schema"
    assert capability_result[0].status == "known"
    assert capability_result[0].input_status("image") == "supported"


@pytest.mark.asyncio
async def test_her_stage_reads_the_same_dynamic_revision_as_direct_adapter(tmp_path):
    model = "vendor/new-her-vision"
    cache = tmp_path / "tmp" / "model-capability-facts-v1.json"
    fact = model_capability_sources.refresh_capability_fact(
        "openrouter-api",
        model,
        cache_path=cache,
        fetcher=lambda _url: _evidence(model),
    )
    cfg = SimpleTestConfig(name="her-stage", workspace_dir=str(tmp_path / "agent"))
    cfg.engine = "openrouter-api"
    cfg.model = model
    global_cfg = SimpleGlobalConfig()
    global_cfg.project_root = tmp_path
    backend = OpenRouterAdapter(cfg, global_cfg)

    class Manager:
        privacy_level = 1

        def create_ephemeral_backend(self, engine, target_model=None):
            assert (engine, target_model) == ("openrouter-api", model)
            return backend

    provider = HashiStageProvider(backend_manager=Manager())
    resolved = await provider.resolve_stage_modalities(
        ProviderProfile("dynamic", "openrouter-api", model)
    )

    assert resolved["input_modalities"] == ("image", "text")
    assert resolved["output_modalities"] == ("text",)
    assert resolved["source"] == "dynamic_capability_cache"
    assert backend.input_capability.source_revision == fact.source_revision


def test_gateway_uses_dynamic_fact_for_new_codex_image_model(tmp_path):
    cache = tmp_path / "capabilities.json"
    model_capability_sources.refresh_capability_fact(
        "codex-cli",
        "gpt-6-astra",
        cache_path=cache,
        fetcher=lambda _url: _evidence(),
    )
    png = b"\x89PNG\r\n\x1a\nimage"
    data_url = "data:image/png;base64," + base64.b64encode(png).decode("ascii")

    result = _validate_structured_conversation(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Inspect it."},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        engine="codex-cli",
        model="gpt-6-astra",
        capability_cache_path=cache,
    )

    assert result is None


def test_real_model_selection_clears_old_snapshot_before_async_refresh(
    tmp_path,
    monkeypatch,
):
    refreshed_models = []
    pricing_targets = []
    capability_targets = []
    backend_cfg = {"model": "old-image-model", "effort": "high"}
    backend = SimpleNamespace(
        config=SimpleNamespace(engine="codex-cli", model="old-image-model"),
        effort="high",
    )
    backend.refresh_input_capability = lambda: refreshed_models.append(
        backend.config.model
    )
    manager = SimpleNamespace(
        current_backend=backend,
        persist_state=lambda **values: values,
    )
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            active_backend="codex-cli",
            allowed_backends=[
                {
                    "engine": "codex-cli",
                    "model": "new-text-model",
                    "models": ["new-text-model"],
                }
            ],
        ),
        backend_manager=manager,
        global_config=SimpleNamespace(bridge_home=tmp_path),
        _get_available_models_for=lambda _engine: ["new-text-model"],
        _get_backend_cfg=lambda _engine: backend_cfg,
    )
    monkeypatch.setattr(
        runtime_model_selection,
        "_schedule_pricing_prewarm",
        lambda _runtime, *targets: pricing_targets.extend(targets),
    )
    monkeypatch.setattr(
        runtime_model_selection,
        "_schedule_capability_prewarm",
        lambda _runtime, *targets: capability_targets.extend(targets),
    )

    runtime_model_selection.set_backend_model(
        runtime,
        "codex-cli",
        "new-text-model",
    )

    assert backend.config.model == "new-text-model"
    assert refreshed_models == ["new-text-model"]
    assert pricing_targets == [("codex-cli", "new-text-model")]
    assert capability_targets == [("codex-cli", "new-text-model")]


def test_async_refresh_updates_only_the_still_selected_exact_model(
    tmp_path,
    monkeypatch,
):
    callbacks = []
    refreshes = []
    backend = SimpleNamespace(
        config=SimpleNamespace(engine="codex-cli", model="gpt-6-astra"),
        refresh_input_capability=lambda: refreshes.append("refreshed"),
    )
    runtime = SimpleNamespace(
        backend_manager=SimpleNamespace(current_backend=backend),
        global_config=SimpleNamespace(bridge_home=tmp_path),
    )
    monkeypatch.setattr(
        model_capability_sources,
        "schedule_prewarm",
        lambda engine, model, *, cache_path=None, on_complete=None: callbacks.append(
            (engine, model, cache_path, on_complete)
        ),
    )

    runtime_model_selection._schedule_capability_prewarm(
        runtime,
        ("codex-cli", "gpt-6-astra"),
    )
    callback = callbacks[0][3]
    callback(
        SimpleNamespace(
            engine="codex-cli",
            requested_model_id="gpt-6-astra",
        )
    )
    backend.config.model = "different-model"
    callback(
        SimpleNamespace(
            engine="codex-cli",
            requested_model_id="gpt-6-astra",
        )
    )

    assert refreshes == ["refreshed"]
