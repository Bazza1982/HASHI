"""Tests for process-local runtime fingerprint memoization."""

from __future__ import annotations

from dataclasses import replace
import pathlib

import pytest

from orchestrator import runtime_fingerprint_cache as cache_module
from orchestrator.runtime_fingerprint_cache import (
    cached_current_runtime_fingerprint,
    invalidate_runtime_fingerprint_cache,
    runtime_fingerprint_cache_stats,
)
from orchestrator.runtime_contract import (
    RuntimeContractError,
    compare_runtime_fingerprints,
    current_runtime_fingerprint,
    load_runtime_policy,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset_cache():
    invalidate_runtime_fingerprint_cache()
    yield
    invalidate_runtime_fingerprint_cache()


def test_cached_fingerprint_equals_authoritative_fingerprint():
    policy = load_runtime_policy(ROOT)
    direct = current_runtime_fingerprint(policy, code_root=ROOT)
    cached = cached_current_runtime_fingerprint(policy, code_root=ROOT)
    assert cached == direct


def test_unchanged_signature_reuses_authoritative_computation(monkeypatch):
    policy = load_runtime_policy(ROOT)
    direct = current_runtime_fingerprint(policy, code_root=ROOT)
    calls = 0

    def compute(policy_, *, code_root):
        nonlocal calls
        calls += 1
        return direct

    monkeypatch.setattr(cache_module, "_fingerprint_signature", lambda *_: "stable")
    monkeypatch.setattr(cache_module, "current_runtime_fingerprint", compute)

    assert cached_current_runtime_fingerprint(policy, code_root=ROOT) == direct
    assert cached_current_runtime_fingerprint(policy, code_root=ROOT) == direct
    assert calls == 1
    assert runtime_fingerprint_cache_stats() == {
        "hits": 1,
        "misses": 1,
        "entries": 1,
    }


def test_signature_change_forces_authoritative_recomputation(monkeypatch):
    policy = load_runtime_policy(ROOT)
    direct = current_runtime_fingerprint(policy, code_root=ROOT)
    signatures = iter(("first", "second"))
    calls = 0

    def compute(policy_, *, code_root):
        nonlocal calls
        calls += 1
        return direct

    monkeypatch.setattr(
        cache_module, "_fingerprint_signature", lambda *_: next(signatures)
    )
    monkeypatch.setattr(cache_module, "current_runtime_fingerprint", compute)

    cached_current_runtime_fingerprint(policy, code_root=ROOT)
    cached_current_runtime_fingerprint(policy, code_root=ROOT)
    assert calls == 2
    assert runtime_fingerprint_cache_stats()["misses"] == 2


def test_manual_invalidation_forces_recomputation(monkeypatch):
    policy = load_runtime_policy(ROOT)
    direct = current_runtime_fingerprint(policy, code_root=ROOT)
    calls = 0

    def compute(policy_, *, code_root):
        nonlocal calls
        calls += 1
        return direct

    monkeypatch.setattr(cache_module, "_fingerprint_signature", lambda *_: "stable")
    monkeypatch.setattr(cache_module, "current_runtime_fingerprint", compute)

    cached_current_runtime_fingerprint(policy, code_root=ROOT)
    invalidate_runtime_fingerprint_cache()
    cached_current_runtime_fingerprint(policy, code_root=ROOT)
    assert calls == 2


def test_distribution_signature_detects_added_metadata(tmp_path, monkeypatch):
    before = cache_module._distribution_set_signature()
    dist_info = tmp_path / "demo_pkg-1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: demo-pkg\nVersion: 1.0\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    after = cache_module._distribution_set_signature()
    assert before != after


def test_cached_fingerprint_still_rejects_runtime_mismatch():
    policy = load_runtime_policy(ROOT)
    cached = cached_current_runtime_fingerprint(policy, code_root=ROOT)
    tampered = replace(
        cached,
        runtime_policy_digest="sha256:" + "0" * 64,
    )

    with pytest.raises(RuntimeContractError):
        compare_runtime_fingerprints(cached, tampered)
