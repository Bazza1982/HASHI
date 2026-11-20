"""Regression tests for PR-H4 (H06, H07, H11, H34, H35, H36, H37, H38)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.builtins import execute_bash
from tools.registry import ToolRegistry
from tools.schemas import ALL_TOOL_NAMES
from tools.smart_tools import (
    SMART_TOOL_SPECS,
    SmartToolRuntime,
    _shell_text_search_admission,
)
from tools.tool_presentation import OnDemandToolView


def test_smart_tool_runtime_defaults_on_and_opt_out(tmp_path) -> None:
    runtime = SmartToolRuntime(tmp_path, None)
    assert runtime.enabled is True
    assert runtime.file_probe_bytes <= 2 * 1024 * 1024
    disabled = SmartToolRuntime(tmp_path, {"enabled": False})
    assert disabled.enabled is False


def test_every_registered_tool_has_explicit_non_generic_profile() -> None:
    non_generic = {
        name for name, spec in SMART_TOOL_SPECS.items() if spec.profile != "generic"
    }
    assert set(ALL_TOOL_NAMES).issubset(non_generic)


def test_smart_tools_unscoped_uses_request_key_for_repeat_detection(tmp_path) -> None:
    runtime = SmartToolRuntime(tmp_path, {"repeat_threshold": 2})
    for call_id in ("c1", "c2"):
        outcome, _, _ = runtime.complete(
            tool_name="apply_patch",
            arguments={"path": "a.txt", "patch": "hi"},
            output="ok",
            raw_is_error=False,
            details={},
            duration_ms=5,
            call_id=call_id,
            audit_context={"request_id": "req-42"},
        )
    assert outcome.warning is not None
    assert outcome.warning.code == "repeated_side_effect"


def test_shell_text_search_admission_allows_bounded_flags_and_pipes(tmp_path) -> None:
    big = tmp_path / "huge.jsonl"
    big.write_bytes(b"x" * 1_100_000 + b"\n")
    for cmd in (
        f"grep -m 5 pattern {big.name}",
        f"rg --max-count=3 pattern {big.name}",
        f"grep pattern {big.name} | head -n 5",
    ):
        admission = _shell_text_search_admission(
            {"command": cmd},
            workspace_dir=tmp_path,
            access_roots=(tmp_path,),
            record_limit=1_000_000,
            probe_limit=2 * 1024 * 1024,
        )
        assert admission is None, f"expected bounded command allowed: {cmd}"


def test_registry_search_options_preserves_short_engineering_defaults(tmp_path) -> None:
    registry = ToolRegistry(
        allowed_tools=["file_search", "file_list"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
        tool_options={"smart_registry": {"enabled": True, "foreground_timeout_seconds": 600}},
    )
    opts = registry._search_options()
    assert "timeout_seconds" not in opts
    assert opts.get("timeout_max_seconds") == 600.0


def test_on_demand_tool_view_keeps_recent_and_in_use_tools_beyond_48() -> None:
    all_names = ["tool_find", *(f"mod_{i:02d}" for i in range(60)), "z_target_tool"]
    fake_base = SimpleNamespace(
        get_tool_definitions=lambda tiers=None: [
            {"type": "function", "function": {"name": n, "description": n}}
            for n in all_names
        ]
    )
    view = OnDemandToolView(fake_base)
    view._activate_after_success("z_target_tool", {}, SimpleNamespace(is_error=False, output="ok"))
    for i in range(55):
        view._activate_after_success(
            "tool_find",
            {"query": f"mod_{i:02d}", "limit": 1},
            SimpleNamespace(is_error=False, output=json.dumps({"matches": [f"mod_{i:02d}"]})),
        )
    visible = {d["function"]["name"] for d in view.get_tool_definitions()}
    assert "z_target_tool" in visible


def test_on_demand_tool_view_bounds_in_use_tools() -> None:
    names = [f"tool_{i:03d}" for i in range(60)]
    fake_base = SimpleNamespace(
        get_tool_definitions=lambda tiers=None: [
            {"type": "function", "function": {"name": n, "description": n}}
            for n in names
        ]
    )
    view = OnDemandToolView(fake_base)
    for n in names:
        view._activate_after_success(n, {}, SimpleNamespace(is_error=False, output="ok"))
    # A very long tool-enabled turn must not grow the in-use set without limit.
    assert len(view._in_use) <= 48
    visible = {d["function"]["name"] for d in view.get_tool_definitions()}
    assert "tool_059" in visible  # most recently used is retained
    assert "tool_000" not in visible  # least recently used was evicted


@pytest.mark.asyncio
async def test_execute_bash_preserves_valid_json_on_truncation(tmp_path) -> None:
    payload = json.dumps({"k": "a" * 25000})
    (tmp_path / "big.json").write_text(payload, encoding="utf-8")
    res = await execute_bash(
        {"command": "cat big.json"},
        workspace_dir=tmp_path,
    )
    parsed = json.loads(res.output)
    assert parsed["truncated"] is True
    assert parsed["original_chars"] > 20000
