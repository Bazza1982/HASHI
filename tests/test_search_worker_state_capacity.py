"""Scanner checkpoints must honor selected capacity without an extra fixed pipe cap."""
from pathlib import Path
import json
from types import SimpleNamespace

import pytest

from tools import file_search
from tools.file_search import normalize_query, run_worker
from tools.registry import ToolRegistry
from tools.search_worker import Scanner


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("size", "capacity", "preserved"),
    [(2_600_000, 6_000_000, True), (4_500_000, 6_000_000, True),
     (4_500_000, 0, True), (3_200_000, 3 * 1024 * 1024, False)],
)
async def test_worker_transports_selected_large_continuation_state(
    tmp_path: Path, size: int, capacity: int, preserved: bool,
):
    (tmp_path / "one.txt").write_text("one", encoding="utf-8")
    (tmp_path / "two.txt").write_text("two", encoding="utf-8")
    config = normalize_query(
        {"query": "*", "mode": "path", "match": "glob", "roots": [str(tmp_path)], "max_results": 1},
        defaults=[], read_roots=[tmp_path], cwd=tmp_path,
    )
    config.update(
        authorized_roots=[str(tmp_path)], protected_read_paths=[],
        match_budget=8000, state_capacity_bytes=capacity,
    )
    state = Scanner(config).state
    state["fixture_padding"] = "x" * size
    config["state"] = state

    result = await run_worker(config)

    assert result["matches"]
    assert (result["state"] is not None) is preserved
    if preserved:
        assert result["state"]["fixture_padding"] == "x" * size
    else:
        assert not result["coverage_complete"]
        assert any("capacity exceeded" in warning for warning in result["warnings"])
    assert result["cleanup"]["process_reaped"]


@pytest.mark.asyncio
async def test_registry_continues_large_state_after_replacement(tmp_path, monkeypatch):
    root, home, instance = tmp_path / "files", tmp_path / "agent", tmp_path / "instance"
    for directory in (root, home, instance):
        directory.mkdir()
    (root / "one.txt").write_text("one", encoding="utf-8")
    (root / "two.txt").write_text("two", encoding="utf-8")
    real_worker = file_search.run_worker

    async def inject_large_state(config):
        if not config.get("state"):
            state = Scanner(config).state
            state["fixture_padding"] = "x" * 4_500_000
            config["state"] = state
        return await real_worker(config)

    monkeypatch.setattr(file_search, "run_worker", inject_large_state)
    context = {
        "instance_id": "HASHI5", "agent_name": "fixture", "owner_id": "owner",
        "hashi_session_id": "session", "hashi_run_id": "run", "request_id": "request",
    }

    def registry():
        return ToolRegistry(
            ["file_search"], root, home, {},
            audit_context={"agent_name": "fixture", "global_config": SimpleNamespace(bridge_home=instance)},
            search_scope={"agent_id": "fixture", "agent_home": str(home), "owner_id": "owner"},
            tool_options={"file_search": {"continuations": {"max_state_bytes": 6_000_000}}},
        )

    args = {"query": "*", "mode": "path", "match": "glob", "roots": [str(root)], "max_results": 1}
    first = await registry().execute_with_audit_context("file_search", args, audit_context=context)
    assert not first.is_error, first.output
    first_data = json.loads(first.output)["data"]
    assert first_data["next_cursor"]
    second = await registry().execute_with_audit_context(
        "file_search", {**args, "cursor": first_data["next_cursor"]}, audit_context=context,
    )
    assert not second.is_error, second.output
    second_data = json.loads(second.output)["data"]
    assert second_data["matches"]
    assert first_data["matches"][0]["path"] != second_data["matches"][0]["path"]
# HASHI-Fingerprint: hfp_35d7e3f249b64422822a25c5fe52fb05
