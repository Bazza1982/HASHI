"""Tests for the process-local runtime fingerprint memoization.

These verify that the cache only removes redundant work: the cached fingerprint
is identical to the direct computation, cache hits are observable, any
invalidation (manual or via an input signature change) forces recomputation,
and the fail-closed runtime-contract comparison is unchanged.
"""

from __future__ import annotations

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
    RuntimeFingerprint,
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


def test_cached_fingerprint_equals_direct_fingerprint():
    policy = load_runtime_policy(ROOT)
    direct = current_runtime_fingerprint(policy, code_root=ROOT)
    cached = cached_current_runtime_fingerprint(policy, code_root=ROOT)
    assert cached == direct


def test_second_call_is_a_cache_hit():
    policy = load_runtime_policy(ROOT)
    first = cached_current_runtime_fingerprint(policy, code_root=ROOT)
    second = cached_current_runtime_fingerprint(policy, code_root=ROOT)
    assert second == first
    stats = runtime_fingerprint_cache_stats()
    assert stats["misses"] == 1
    assert stats["hits"] == 1


def test_manual_invalidation_forces_recompute():
    policy = load_runtime_policy(ROOT)
    cached_current_runtime_fingerprint(policy, code_root=ROOT)
    assert runtime_fingerprint_cache_stats()["entries"] == 1
    invalidate_runtime_fingerprint_cache()
    assert runtime_fingerprint_cache_stats()["entries"] == 0
    assert runtime_fingerprint_cache_stats()["misses"] == 0
    cached_current_runtime_fingerprint(policy, code_root=ROOT)
    assert runtime_fingerprint_cache_stats()["entries"] == 1
    assert runtime_fingerprint_cache_stats()["misses"] == 1


def test_signature_change_forces_fingerprint_recompute(monkeypatch):
    policy = load_runtime_policy(ROOT)
    cached_current_runtime_fingerprint(policy, code_root=ROOT)
    assert runtime_fingerprint_cache_stats()["misses"] == 1

    monkeypatch.setattr(
        cache_module,
        "_fingerprint_signature",
        lambda policy_, code_root_: "changed",
    )
    cached_current_runtime_fingerprint(policy, code_root=ROOT)
    assert runtime_fingerprint_cache_stats()["misses"] == 2


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
    """Fail-closed comparison is unchanged through the cached path."""
    policy = load_runtime_policy(ROOT)
    cached = cached_current_runtime_fingerprint(policy, code_root=ROOT)

    tampered = dict(cached.to_dict())
    tampered["dependency_digest"] = "sha256:" + "0" * 64
    tampered_fingerprint = RuntimeFingerprint.from_mapping(tampered)

    with pytest.raises(RuntimeContractError):
        compare_runtime_fingerprints(cached, tampered_fingerprint)
