"""Recoverable tool-level execution budgets for local search.

Covers budget resolution precedence, the cooperative deadline (partial result
with a working continuation cursor), the emergency cleanup path (no invented
cursor), per-call budget requests, and honest complete zero-match reporting.
All fixtures are synthetic; nothing here touches a production environment.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from tools.file_search import (
    DEFAULT_CLEANUP_GRACE_SECONDS,
    DEFAULT_SEARCH_BUDGET_SECONDS,
    resolve_search_budget,
)
from tests.test_scoped_search import registry_for, payload


def test_budget_precedence_per_call_then_config_then_default(monkeypatch):
    defaults = {
        "file_search": 45.0, "file_list": 15.0, "file_list_recursive": 45.0,
        "log_query": 45.0, "repo_discover": 30.0,
    }
    monkeypatch.setattr("tools.file_search.DEFAULT_SEARCH_BUDGET_SECONDS", dict(defaults))

    # Engineering default.
    budget, policy = resolve_search_budget({}, options={}, operation="file_search")
    assert budget == 45.0 and policy["source"] == "engineering_default"
    budget, policy = resolve_search_budget({"_recursive": False}, options={}, operation="file_list")
    assert budget == 15.0
    budget, policy = resolve_search_budget({"_recursive": True}, options={}, operation="file_list")
    assert budget == 45.0

    # Configured timeout keeps its existing precedence over the default.
    budget, policy = resolve_search_budget({}, options={"timeout_seconds": 9.5}, operation="file_search")
    assert budget == 9.5 and policy["source"] == "configured_timeout"

    # Per-call request wins over configuration; a cap limits it.
    budget, policy = resolve_search_budget(
        {"timeout_seconds": 120}, options={"timeout_seconds": 9.5,
                                          "timeout_max_seconds": 60},
        operation="file_search")
    assert budget == 60 and policy["source"] == "per_call_request" and policy["capped"]

    # Per-call request without a cap extends beyond the configured value.
    budget, policy = resolve_search_budget(
        {"timeout_seconds": 120}, options={"timeout_seconds": 9.5},
        operation="file_search")
    assert budget == 120 and not policy["capped"]

    with pytest.raises(ValueError):
        resolve_search_budget({"timeout_seconds": -1}, options={}, operation="file_search")
    with pytest.raises(ValueError):
        resolve_search_budget({"timeout_seconds": "soon"}, options={}, operation="file_search")


@pytest.mark.asyncio
async def test_cooperative_deadline_returns_partial_result_with_working_cursor(tmp_path):
    """A deadline stop returns a partial result whose cursor resumes the scan
    without duplicating or losing work, and the resumed scan completes
    coverage honestly (a zero-match completion is then legitimate)."""
    registry, repo, _ = registry_for(tmp_path, options={
        "file_search": {"timeout_seconds": 0.05}})
    for i in range(1200):
        (repo / f"file-{i:04d}.txt").write_text("nothing relevant", encoding="utf-8")
    args = {"query": "absent", "roots": [str(repo)], "max_results": 200}
    stop_reasons: set[str] = set()
    rounds = 0
    cursor = None
    data = None
    while True:
        rounds += 1
        if cursor is not None:
            args["cursor"] = cursor
        result = await registry.execute_with_audit_context(
            "file_search", args, audit_context={"request_id": "budget-run"})
        envelope = json.loads(result.output)
        data = payload(result)
        stop_reasons.add(data["stop_reason"])
        assert not data["matches"]  # no false positives along the way
        if data["can_continue"]:
            assert envelope["status"] == "partial"
            assert data["next_cursor"] and data["next_cursor"] != "x" * 43
            if data["stop_reason"] == "deadline":
                assert any("continue this scan with next_cursor" in warning.lower()
                           for warning in data["warnings"])
            cursor = data["next_cursor"]
        else:
            break
    # The deadline must have fired at least once, and only the fully resumed
    # scan may report complete coverage.
    assert "deadline" in stop_reasons
    assert data["coverage_complete"] and data["scope_exhausted"]
    assert data["stop_reason"] == "completed"
    assert rounds >= 2


@pytest.mark.asyncio
async def test_pagination_and_deadline_continue_without_duplicate_or_lost_matches(tmp_path):
    registry, repo, _ = registry_for(tmp_path, options={
        "file_search": {"timeout_seconds": 0.15}})
    expected = set()
    for i in range(200):
        sub = repo / f"dir-{i:03d}"
        sub.mkdir()
        for j in range(8):
            file = sub / f"hit-{j}.txt"
            file.write_text("needle", encoding="utf-8")
            expected.add(str(file))
    args = {"query": "hit", "roots": [str(repo)], "max_results": 200}
    found: set[str] = set()
    cursor = None
    while True:
        if cursor is not None:
            args["cursor"] = cursor
        result = await registry.execute_with_audit_context(
            "file_search", args, audit_context={"request_id": "budget-run"})
        envelope = json.loads(result.output)
        data = payload(result)
        found.update(item["path"] for item in data["matches"])
        if data["can_continue"]:
            assert envelope["status"] == "partial"
            assert data["next_cursor"] and data["next_cursor"] != "x" * 43
            if data["stop_reason"] == "deadline":
                assert any("continue this scan with next_cursor" in warning.lower()
                           for warning in data["warnings"])
            cursor = data["next_cursor"]
        else:
            break
    assert data["coverage_complete"] and data["scope_exhausted"]
    assert found == expected and len(found) == len(expected)


@pytest.mark.asyncio
async def test_emergency_cleanup_reports_lost_continuation_and_reaps_worker(tmp_path, monkeypatch):
    # The worker hangs before producing any result; the parent must terminate
    # the owned tree and must not invent a continuation cursor.
    monkeypatch.setenv("HASHI_SEARCH_WORKER_TEST_HANG_SECONDS", "10")
    registry, repo, _ = registry_for(tmp_path, options={
        "file_search": {"timeout_seconds": 0.1, "cleanup_grace_seconds": 0.2}})
    (repo / "slow.txt").write_text("needle", encoding="utf-8")
    started = asyncio.get_running_loop().time()
    result = await registry.execute(
        "file_search", {"query": "needle", "mode": "content", "roots": [str(repo)]})
    elapsed = asyncio.get_running_loop().time() - started
    envelope = json.loads(result.output)
    assert envelope["status"] == "partial"
    data = envelope["data"]
    assert data["stop_reason"] == "deadline"
    assert data["coverage_complete"] is False and data["scope_exhausted"] is False
    assert data["can_continue"] is False and data["next_cursor"] is None
    assert data.get("continuation_lost") is True
    assert any("no continuation cursor" in warning.lower() for warning in data["warnings"])
    assert not any("continue this scan with next_cursor" in warning.lower()
                   for warning in data["warnings"])
    cleanup = result.details["foreground_cleanup"]
    assert cleanup["process_reaped"] and cleanup["status"] != "cleanup_failed"
    assert elapsed < 5


@pytest.mark.asyncio
async def test_per_call_timeout_is_capped_and_reported(tmp_path):
    registry, repo, _ = registry_for(tmp_path, options={
        "file_search": {"timeout_max_seconds": 0.2}})
    for i in range(60):
        sub = repo / f"dir-{i:02d}"
        sub.mkdir()
        (sub / "a.txt").write_text("needle", encoding="utf-8")
    result = await registry.execute(
        "file_search", {"query": "needle", "roots": [str(repo)],
                        "max_results": 200, "timeout_seconds": 120})
    data = payload(result)
    assert data["execution_budget"]["source"] == "per_call_request"
    assert data["execution_budget"]["capped"] is True
    assert data["execution_budget"]["effective_seconds"] == 0.2


@pytest.mark.asyncio
async def test_invalid_per_call_timeout_is_rejected_not_ignored(tmp_path):
    registry, repo, _ = registry_for(tmp_path)
    result = await registry.execute(
        "file_search", {"query": "x", "roots": [str(repo)], "timeout_seconds": -3})
    assert result.is_error
    envelope = json.loads(result.output)
    assert envelope["error"]["code"] == "invalid_timeout"


@pytest.mark.asyncio
async def test_completed_zero_match_search_reports_complete_coverage(tmp_path):
    registry, repo, _ = registry_for(tmp_path)
    (repo / "a.txt").write_text("present", encoding="utf-8")
    envelope = json.loads((await registry.execute(
        "file_search", {"query": "absent", "roots": [str(repo)]})).output)
    assert envelope["status"] == "success"
    assert not envelope["data"]["matches"]
    assert envelope["data"]["coverage_complete"] and envelope["data"]["scope_exhausted"]
    assert envelope["data"]["stop_reason"] == "completed"
    assert envelope["data"]["execution_budget"]["source"] == "engineering_default"
    assert envelope["data"]["execution_budget"]["effective_seconds"] == DEFAULT_SEARCH_BUDGET_SECONDS["file_search"]


@pytest.mark.asyncio
async def test_emergency_cleanup_does_not_corrupt_next_search(tmp_path, monkeypatch):
    monkeypatch.setenv("HASHI_SEARCH_WORKER_TEST_HANG_SECONDS", "10")
    registry, repo, _ = registry_for(tmp_path, options={
        "file_search": {"timeout_seconds": 0.05, "cleanup_grace_seconds": 0.1}})
    (repo / "a.txt").write_text("needle", encoding="utf-8")
    first = await registry.execute(
        "file_search", {"query": "needle", "mode": "content", "roots": [str(repo)]})
    assert json.loads(first.output)["status"] == "partial"
    assert json.loads(first.output)["data"]["continuation_lost"] is True
    # A fresh search must use a responsive worker again (hang "0" disables
    # the test-only stall; monkeypatch restores the original value later).
    monkeypatch.setenv("HASHI_SEARCH_WORKER_TEST_HANG_SECONDS", "0")
    # The same registry must serve a fresh, unrelated search afterwards.
    second = payload(await registry.execute(
        "file_search", {"query": "nothing-here", "roots": [str(repo)]}))
    assert second["coverage_complete"] and second["stop_reason"] == "completed"


@pytest.mark.asyncio
async def test_deadline_result_warnings_offer_actionable_next_steps(tmp_path):
    registry, repo, _ = registry_for(tmp_path, options={
        "file_search": {"timeout_seconds": 0.02}})
    for i in range(400):
        sub = repo / f"dir-{i:03d}"
        sub.mkdir()
        (sub / "a.txt").write_text("needle", encoding="utf-8")
    data = payload(await registry.execute(
        "file_search", {"query": "needle", "roots": [str(repo)], "max_results": 200}))
    assert data["stop_reason"] == "deadline"
    if data["can_continue"]:
        assert any("continue this scan with next_cursor" in warning.lower()
                   for warning in data["warnings"])
    else:
        assert not any("continue this scan with next_cursor" in warning.lower()
                       for warning in data["warnings"])
        assert any("fresh scan" in warning.lower() for warning in data["warnings"])


@pytest.mark.asyncio
async def test_lost_scan_with_buffered_matches_offers_only_page_continuation(tmp_path, monkeypatch):
    """A page of retained matches is not a cursor for lost scan state."""
    from tools.file_search import Continuations, execute_file_search

    matches = [{"path": str(tmp_path / f"item-{index}.txt"), "excerpt": "x" * 2000}
               for index in range(12)]

    async def stopped_worker(_config):
        return {
            "matches": matches,
            "counters": {"matches": len(matches)},
            "coverage": {"roots": [{"path": str(tmp_path), "failed": False}], "errors": []},
            "scope_exhausted": False,
            "coverage_complete": False,
            "stop_reason": "deadline",
            "state": None,
            "continuation_lost": True,
            "warnings": [],
            "cleanup": {"status": "terminated", "process_reaped": True, "errors": []},
        }

    monkeypatch.setattr("tools.file_search.run_worker", stopped_worker)
    result = await execute_file_search(
        {"query": "needle", "roots": [str(tmp_path)]},
        read_roots=[tmp_path], cwd=tmp_path,
        search_scope={"owner_id": "fixture"},
        audit_context={"agent_name": "fixture", "request_id": "buffered"},
        continuations=Continuations(),
    )
    data = json.loads(result.output)["data"]
    assert data["can_continue"] and data["next_cursor"]
    assert data["continuation_lost"] and not data["coverage_complete"]
    assert any("buffered matches only" in warning.lower() for warning in data["warnings"])
    assert not any("continue this scan with next_cursor" in warning.lower()
                   for warning in data["warnings"])
