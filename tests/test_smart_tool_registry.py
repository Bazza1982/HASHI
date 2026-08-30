from __future__ import annotations

import json

import pytest

from adapters.her_v2_provider import _EvidenceRecordingToolRegistry
from orchestrator.her_v2.models import Effort, Stage, StageRequest
from tools.builtins import BuiltinExecutionResult
from tools.registry import ToolRegistry
from tools.schemas import ALL_TOOL_NAMES
from tools.smart_tools import (
    SMART_TOOL_EFFECTS,
    SMART_TOOL_PROFILES,
    SMART_TOOL_SPECS,
    SMART_TOOL_STATUSES,
    adapt_legacy_result,
)


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
        audit_context={
            "task_id": "task-123",
            "stage": "execution",
            "model": "test-model",
        },
    )


def _payload(result) -> dict:
    payload = json.loads(result.output)
    assert set(payload) == {"status", "effect", "data", "error", "warning"}
    assert payload["status"] in SMART_TOOL_STATUSES
    assert payload["effect"] in SMART_TOOL_EFFECTS
    return payload


def _ledger(tmp_path) -> list[dict]:
    return [
        json.loads(line)
        for line in (tmp_path / "tool_ledger.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


def test_every_registered_tool_has_one_minimal_profile() -> None:
    assert set(SMART_TOOL_SPECS) == set(ALL_TOOL_NAMES)
    assert SMART_TOOL_PROFILES == {
        "query",
        "poll",
        "verify",
        "idempotent_action",
        "side_effect_action",
        "generic",
    }
    for name, spec in SMART_TOOL_SPECS.items():
        assert spec.name == name
        assert spec.version
        assert spec.profile in SMART_TOOL_PROFILES
        assert spec.description


@pytest.mark.asyncio
async def test_bash_nonzero_exit_is_failed_not_false_success(
    tmp_path, monkeypatch
) -> None:
    registry = _registry(tmp_path, "bash")

    async def fake_dispatch(tool_name, arguments):
        assert tool_name == "bash"
        assert arguments == {"command": "false"}
        return BuiltinExecutionResult(
            "[exit code 1]\nfailed output", {"exit_code": 1}
        )

    monkeypatch.setattr(registry, "_dispatch", fake_dispatch)
    result = await registry.execute("bash", {"command": "false"}, "call-1")

    payload = _payload(result)
    assert result.is_error is True
    assert payload["status"] == "failed"
    assert payload["effect"] == "unknown"
    assert payload["error"]["code"] == "nonzero_exit"
    assert _ledger(tmp_path)[0]["status"] == "failed"


def test_bash_command_not_found_does_not_claim_no_change() -> None:
    _spec, outcome = adapt_legacy_result(
        "bash",
        output="[exit code 127]\nmissing-command: not found",
        raw_is_error=False,
        details={"exit_code": 127},
    )

    assert outcome.status == "failed"
    assert outcome.effect == "unknown"
    assert outcome.error.code == "command_not_found"


@pytest.mark.asyncio
async def test_scheduler_without_gateway_is_unavailable_and_not_retryable(
    tmp_path, monkeypatch
) -> None:
    registry = _registry(tmp_path, "hashi_scheduler_list")

    async def fake_dispatch(_tool_name, _arguments):
        return "Error: HASHI Workbench API is unavailable in this gateway context"

    monkeypatch.setattr(registry, "_dispatch", fake_dispatch)
    result = await registry.execute("hashi_scheduler_list", {}, "call-1")

    payload = _payload(result)
    assert result.is_error is True
    assert payload["status"] == "unavailable"
    assert payload["effect"] == "no_change"
    assert payload["error"] == {
        "code": "gateway_context_missing",
        "message": "Scheduler is unavailable in the current environment.",
        "retryable": False,
    }
    assert payload["warning"]["code"] == "unavailable_environment"


def test_unreachable_scheduler_rerun_keeps_side_effect_unknown() -> None:
    _spec, outcome = adapt_legacy_result(
        "hashi_scheduler_rerun",
        output="Error: HASHI Scheduler API is unavailable: timeout",
        raw_is_error=True,
    )

    assert outcome.status == "unavailable"
    assert outcome.effect == "unknown"
    assert outcome.error.code == "scheduler_unreachable"


def test_legacy_unavailable_detail_maps_to_unavailable() -> None:
    _spec, outcome = adapt_legacy_result(
        "verification_run",
        output="Error: unknown verification recipe 'missing'",
        raw_is_error=True,
        details={"unavailable": True},
    )

    assert outcome.status == "unavailable"
    assert outcome.effect == "no_change"
    assert outcome.error.code == "tool_unavailable"


@pytest.mark.asyncio
async def test_rejected_patch_is_failed_and_known_no_change(
    tmp_path, monkeypatch
) -> None:
    registry = _registry(tmp_path, "apply_patch")

    async def fake_dispatch(_tool_name, _arguments):
        return "Error: patch rejected (dry-run):\nhunk failed"

    monkeypatch.setattr(registry, "_dispatch", fake_dispatch)
    result = await registry.execute(
        "apply_patch", {"path": "a.py", "patch": "bad"}, "call-1"
    )

    payload = _payload(result)
    assert payload["status"] == "failed"
    assert payload["effect"] == "no_change"
    assert payload["error"]["code"] == "patch_rejected"


@pytest.mark.asyncio
async def test_third_identical_query_warns_and_writes_one_row_per_call(
    tmp_path, monkeypatch
) -> None:
    registry = _registry(tmp_path, "file_read")

    async def fake_dispatch(_tool_name, _arguments):
        return "same result"

    monkeypatch.setattr(registry, "_dispatch", fake_dispatch)
    results = [
        await registry.execute(
            "file_read", {"path": "state.txt"}, f"call-{index}"
        )
        for index in range(1, 4)
    ]

    assert _payload(results[0])["warning"] is None
    assert _payload(results[1])["warning"] is None
    assert _payload(results[2])["warning"]["code"] == "same_result_repeated"
    rows = _ledger(tmp_path)
    assert [row["repeat_count"] for row in rows] == [0, 1, 2]
    assert len({row["result_hash"] for row in rows}) == 1
    assert not (tmp_path / "tool_action_audit.jsonl").exists()
    assert set(rows[0]) == {
        "timestamp",
        "task_id",
        "call_id",
        "stage",
        "model",
        "tool",
        "tool_version",
        "args_hash",
        "status",
        "effect",
        "error_code",
        "duration_ms",
        "result_hash",
        "repeat_count",
    }
    assert rows[0]["task_id"] == "task-123"
    assert rows[0]["stage"] == "execution"
    assert rows[0]["model"] == "test-model"


@pytest.mark.asyncio
async def test_poll_repeat_warns_with_backoff_but_never_stops(
    tmp_path, monkeypatch
) -> None:
    registry = _registry(tmp_path, "background_job_status")
    calls = 0

    async def fake_dispatch(_tool_name, _arguments):
        nonlocal calls
        calls += 1
        return '{"status":"running"}'

    monkeypatch.setattr(registry, "_dispatch", fake_dispatch)
    results = [
        await registry.execute(
            "background_job_status", {"job_id": "job-1"}, f"call-{index}"
        )
        for index in range(1, 4)
    ]

    assert calls == 3
    assert _payload(results[-1])["warning"]["code"] == "poll_state_unchanged"


@pytest.mark.asyncio
async def test_repeated_side_effect_warns_after_execution(tmp_path, monkeypatch) -> None:
    registry = _registry(tmp_path, "telegram_send")
    calls = 0

    async def fake_dispatch(_tool_name, _arguments):
        nonlocal calls
        calls += 1
        return "sent"

    monkeypatch.setattr(registry, "_dispatch", fake_dispatch)
    first = await registry.execute(
        "telegram_send", {"message": "hello"}, "call-1"
    )
    second = await registry.execute(
        "telegram_send", {"message": "hello"}, "call-2"
    )

    assert calls == 2
    assert _payload(first)["warning"] is None
    assert _payload(second)["warning"]["code"] == "repeated_side_effect"


@pytest.mark.asyncio
async def test_idempotent_action_is_rechecked_not_short_circuited(
    tmp_path, monkeypatch
) -> None:
    registry = _registry(tmp_path, "file_write")
    calls = 0

    async def fake_dispatch(_tool_name, _arguments):
        nonlocal calls
        calls += 1
        return '{"changed":false}'

    monkeypatch.setattr(registry, "_dispatch", fake_dispatch)
    first = await registry.execute(
        "file_write", {"path": "a.txt", "content": "x"}, "call-1"
    )
    second = await registry.execute(
        "file_write", {"path": "a.txt", "content": "x"}, "call-2"
    )

    assert calls == 2
    assert _payload(first)["effect"] == "no_change"
    assert _payload(second)["effect"] == "no_change"


@pytest.mark.asyncio
async def test_her_receipt_stays_internal_to_five_field_result(
    tmp_path, monkeypatch
) -> None:
    registry = _registry(tmp_path, "file_read")

    async def fake_dispatch(_tool_name, _arguments):
        return "observed"

    monkeypatch.setattr(registry, "_dispatch", fake_dispatch)
    request = StageRequest(
        turn_id="turn-77",
        request_ref="request-1",
        stage=Stage.EXECUTION,
        role="primary",
        attempt=1,
        goal="Inspect.",
        classification=None,
        effort=Effort.HIGH,
        allow_tools=True,
    )
    evidence = _EvidenceRecordingToolRegistry(
        registry, request, model="execution-model"
    )

    result = await evidence.execute("file_read", {"path": "a.txt"}, "call-1")

    _payload(result)
    assert "HASHI_EVIDENCE_RECEIPT" not in result.output
    assert result.details["evidence_ref"]
    row = _ledger(tmp_path)[0]
    assert row["task_id"] == "turn-77"
    assert row["stage"] == "execution"
    assert row["model"] == "execution-model"
