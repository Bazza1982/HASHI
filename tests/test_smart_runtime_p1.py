"""P1 remediation tests: authoritative error status and repeat-detection scope."""
from __future__ import annotations

import pytest

from tools.registry import ToolRegistry, ToolResult
from tools.smart_tools import SmartToolOutcome, SmartToolRuntime, SmartToolSpec


def _registry(tmp_path, *tool_names: str) -> ToolRegistry:
    return ToolRegistry(
        allowed_tools=list(tool_names),
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
        tool_options={
            "smart_registry": {
                "enabled": True,
                "ledger_path": "tool_ledger.jsonl",
                "repeat_threshold": 3,
            }
        },
        audit_context={"task_id": "task-123", "stage": "execution"},
    )


def _force_outcome(registry, status="success", effect="observed", error=None, warning=None):
    def fake_complete(**kwargs):
        spec = SmartToolSpec(
            name=kwargs.get("tool_name") or "unknown",
            version="1",
            profile="generic",
            description="",
        )
        outcome = SmartToolOutcome(
            status=status, effect=effect, data=None, error=error, warning=warning
        )
        return outcome, spec, {}

    registry.smart_tools.complete = fake_complete


def test_native_failure_never_reclassified_as_success(tmp_path):
    registry = _registry(tmp_path, "file_read")
    # A classifier that (incorrectly) reports success on an inconsistent envelope.
    _force_outcome(registry, status="success", effect="observed")
    native = ToolResult(tool_call_id="call-1", output="whatever", is_error=True, details={})
    result = registry._finalize_tool_result("file_read", {"path": "x"}, native, started=0.0)
    assert result.is_error is True
    assert result.details["smart_status"] == "success"


def test_native_success_preserved(tmp_path):
    registry = _registry(tmp_path, "file_read")
    _force_outcome(registry, status="success", effect="observed")
    native = ToolResult(tool_call_id="call-1", output="content", is_error=False, details={})
    result = registry._finalize_tool_result("file_read", {"path": "x"}, native, started=0.0)
    assert result.is_error is False


def test_native_success_with_partial_classification_is_not_an_error(tmp_path):
    registry = _registry(tmp_path, "file_read")
    _force_outcome(registry, status="partial", effect="observed")
    native = ToolResult(tool_call_id="call-1", output="partial data", is_error=False, details={})
    result = registry._finalize_tool_result("file_read", {"path": "x"}, native, started=0.0)
    assert result.is_error is False
    assert result.details["smart_status"] == "partial"


def test_native_success_with_needs_replan_classification_is_an_error(tmp_path):
    registry = _registry(tmp_path, "file_read")
    _force_outcome(registry, status="needs_replan", effect="observed")
    native = ToolResult(tool_call_id="call-1", output="x", is_error=False, details={})
    result = registry._finalize_tool_result("file_read", {"path": "x"}, native, started=0.0)
    assert result.is_error is True


def test_native_failure_with_unavailable_classification_is_an_error(tmp_path):
    registry = _registry(tmp_path, "file_read")
    _force_outcome(registry, status="unavailable", effect="observed")
    native = ToolResult(tool_call_id="call-1", output="x", is_error=False, details={})
    result = registry._finalize_tool_result("file_read", {"path": "x"}, native, started=0.0)
    assert result.is_error is True


def _runtime(tmp_path):
    return SmartToolRuntime(
        tmp_path, {"enabled": True, "repeat_threshold": 3}
    )


def _call(rt, call_id, audit_context, output="same output"):
    return rt.complete(
        tool_name="file_search",
        arguments={"query": "q"},
        output=output,
        raw_is_error=False,
        details=None,
        duration_ms=1,
        call_id=call_id,
        audit_context=audit_context,
    )


def test_repeat_unscoped_requests_do_not_inherit_history(tmp_path):
    rt = _runtime(tmp_path)
    for i in range(4):
        outcome, _spec, _rec = _call(rt, f"call-{i}", {})
        assert outcome.warning is None, "unscoped calls must not invent a repeat warning"


def test_repeat_different_requests_same_action_do_not_warn(tmp_path):
    rt = _runtime(tmp_path)
    for req in ("req-1", "req-2", "req-3"):
        outcome, _spec, _rec = _call(rt, f"call-{req}", {"request_id": req})
        assert outcome.warning is None, "independent requests must not inherit repeat history"


def test_repeat_within_single_request_warns(tmp_path):
    rt = _runtime(tmp_path)
    codes = []
    for i in range(3):
        outcome, _spec, _rec = _call(rt, f"call-{i}", {"request_id": "req-1"})
        codes.append(outcome.warning.code if outcome.warning else None)
    assert codes == [None, None, "same_result_repeated"]


def test_repeat_within_single_task_warns(tmp_path):
    rt = _runtime(tmp_path)
    codes = []
    for i in range(3):
        outcome, _spec, _rec = _call(rt, f"call-{i}", {"task_id": "task-9"})
        codes.append(outcome.warning.code if outcome.warning else None)
    assert codes == [None, None, "same_result_repeated"]
