from __future__ import annotations

import importlib.util
import json
import time
import urllib.request
from pathlib import Path

import pytest

from tools.her_debug.cleanup import CleanupGuard, UnsafeCleanupTarget
from tools.her_debug.evidence import EvidenceCollector
from tools.her_debug.lab import (
    HerDebugLab,
    _json,
    _optional_file_baseline,
    _resolve_candidate_binary,
    _runtime_python,
)
from tools.her_debug.scripted_provider import (
    EXACT_FINAL_FRAGMENTS,
    EXACT_REASONING_FRAGMENTS,
    ScriptedProvider,
)
from tools.her_debug.step_state import SequentialStepState, StepProtocolError

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_python_preserves_external_virtualenv_entrypoint(monkeypatch, tmp_path: Path) -> None:
    import tools.her_debug.lab as lab_module

    checkout = tmp_path / "clean-checkout"
    checkout.mkdir()
    external_python = tmp_path / "shared-venv" / "bin" / "python"
    monkeypatch.setattr(lab_module, "ROOT", checkout)
    monkeypatch.setattr(lab_module.sys, "executable", str(external_python))

    assert _runtime_python() == external_python.absolute()


def _controller_module():
    path = ROOT / "scripts" / "her_debug_superloop.py"
    spec = importlib.util.spec_from_file_location("her_debug_superloop", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_campaign_expands_every_cell_scenario_and_presentation_run() -> None:
    items = _controller_module()._build_work_items()
    core = [item for item in items if item["feature_profile"] == "core_off"]
    habit_wire = [
        item for item in items if item["habit_scenario"] == "habit_wire"
    ]
    habit_deep = [
        item for item in items if item["habit_scenario"] == "habit_deep"
    ]
    habit_fault = [
        item for item in items if item["habit_scenario"] == "habit_fault"
    ]

    assert len(items) == 27
    assert len({item["work_item_id"] for item in items}) == 27
    assert len(core) == 12
    assert all(item["stage"] == "stage_1_flash" for item in core)
    assert sum(len(item["scenario_groups"]) for item in core) == 120
    assert sum(len(item["presentation_runs"]) for item in core) == 96
    assert len(habit_wire) == 12
    assert len(habit_deep) == 2
    assert len(habit_fault) == 1
    assert {item["provider"] for item in items} == {"official_deepseek"}
    assert {item["model"] for item in items} == {"deepseek-v4-flash"}
    assert all(item["status"] == "pending" for item in items)
    assert all(item["habit_scenario"] == "none" for item in core)
    assert all(item["feature_profile"] == "habit_on" for item in items if item not in core)


def test_sequential_step_tool_rejects_skip_and_repeat(tmp_path: Path) -> None:
    state = SequentialStepState.create(tmp_path / "step.json", target_steps=2, seed="fixture")
    first = state.expected_token()
    assert first
    second = state.token_for("fixture", 2)

    with pytest.raises(StepProtocolError, match="out-of-order"):
        state.accept(second)
    assert state.accept(first)["accepted_step"] == 1
    with pytest.raises(StepProtocolError, match="repeated"):
        state.accept(first)
    result = state.accept(second)
    assert result["complete"] is True
    assert state.expected_token() is None


def test_cleanup_guard_only_deletes_declared_child(tmp_path: Path) -> None:
    lab = HerDebugLab(tmp_path / "lab")
    layout = lab.create_run(target_steps=1)
    guard = CleanupGuard(lab.root)

    with pytest.raises(UnsafeCleanupTarget):
        guard.validate_run_root(lab.root)
    with pytest.raises(UnsafeCleanupTarget):
        guard.delete_disposable(layout.root, ["../evidence"])
    with pytest.raises(UnsafeCleanupTarget):
        guard.delete_disposable(layout.root, ["evidence"])

    disposable = layout.workspace / "disposable" / "delete-me.txt"
    disposable.write_text("x", encoding="utf-8")
    assert guard.delete_disposable(layout.root, ["workspace/disposable"]) == ["workspace/disposable"]
    assert not disposable.exists()
    assert layout.evidence.is_dir()


def test_optional_operator_baseline_is_clone_portable_and_content_free(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"

    assert _optional_file_baseline(state_path) == {"present": False, "sha256": None}

    state_path.write_text('{"private": "do-not-copy"}', encoding="utf-8")
    baseline = _optional_file_baseline(state_path)

    assert baseline["present"] is True
    assert len(str(baseline["sha256"])) == 64
    assert "do-not-copy" not in json.dumps(baseline)


def test_debug_lab_reads_windows_utf8_bom_json(tmp_path: Path) -> None:
    path = tmp_path / "agents.json"
    path.write_text('\ufeff{"agents": []}\n', encoding="utf-8")

    assert _json(path) == {"agents": []}


def test_debug_lab_prefers_staged_candidate_and_records_its_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = tmp_path / "hashi-her"
    staged.write_bytes(b"staged candidate")
    monkeypatch.setenv("HASHI_HER_STAGED_BINARY", str(staged))
    monkeypatch.delenv("HASHI_HER_STAGED_SHA256", raising=False)
    manifest = {
        "binaries": {
            "linux-x86_64": {
                "path": "releases/active/linux-x86_64/hashi-her",
                "sha256": "0" * 64,
            }
        }
    }

    candidate = _resolve_candidate_binary(manifest)

    assert candidate.path == staged.resolve()
    assert candidate.selection == "staged_environment"
    assert candidate.sha256 == "8143c85d18fc42e62d559f75df23c59b3439ccbd0be50316bd4523716bd06d47"
    assert candidate.expected_sha256 is None


def test_evidence_collector_redacts_keys_values_and_key_like_strings(tmp_path: Path) -> None:
    collector = EvidenceCollector(tmp_path, forbidden_values=["PRIVATE_CANARY"])
    path = collector.write_json(
        "record.json",
        {
            "authorization": "Bearer PRIVATE_CANARY",
            "diagnostic": "PRIVATE_CANARY sk-abcdefghijklmnopqrstuvwxyz",
            "safe": "retained",
        },
    )
    text = path.read_text(encoding="utf-8")

    assert "PRIVATE_CANARY" not in text
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in text
    assert '"authorization": "<redacted>"' in text
    assert collector.scan()["ok"] is True


def test_scripted_provider_preserves_exact_fragment_boundaries() -> None:
    with ScriptedProvider("exact_stream") as provider:
        request = urllib.request.Request(
            f"{provider.base_url}/chat/completions",
            data=json.dumps({"model": "local-fixture", "messages": [], "stream": True}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            lines = response.read().decode("utf-8").splitlines()

    payloads = [json.loads(line.removeprefix("data: ")) for line in lines if line.startswith("data: {")]
    deltas = [payload["choices"][0]["delta"] for payload in payloads]
    reasoning = tuple(delta["reasoning_content"] for delta in deltas if "reasoning_content" in delta)
    final = tuple(delta["content"] for delta in deltas if "content" in delta)
    assert reasoning == EXACT_REASONING_FRAGMENTS
    assert final == EXACT_FINAL_FRAGMENTS
    assert provider.sanitized_requests()[0]["model"] == "local-fixture"


def test_scripted_provider_tolerates_timed_out_client_disconnect() -> None:
    with ScriptedProvider("delayed_response_once", delay_seconds=0.2) as provider:
        request = urllib.request.Request(
            f"{provider.base_url}/chat/completions",
            data=json.dumps({"model": "local-fixture", "messages": [], "stream": True}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(TimeoutError):
            urllib.request.urlopen(request, timeout=0.05)
        time.sleep(0.25)
        with urllib.request.urlopen(request, timeout=2) as response:
            assert b"SCRIPTED_OK" in response.read()

    assert provider.expected_disconnects == 1


def test_lab_self_test_proves_isolation_and_cleanup_guard(tmp_path: Path) -> None:
    result = HerDebugLab(tmp_path / "lab").self_test()

    assert result["ok"] is True
    assert result["checks"]["repeat_rejected"] is True
    assert result["checks"]["broad_delete_rejected"] is True
    assert result["checks"]["evidence_retained"] is True
    assert result["checks"]["gateway_context_mode"] == "0o600"


def test_packaged_candidate_provider_error_has_one_terminal_event(tmp_path: Path) -> None:
    result = HerDebugLab(tmp_path / "lab").run_scenario("http_400", timeout_seconds=20)

    assert result["ok"] is True
    assert result["checks"]["returncode"] == 1
    assert result["checks"]["run_started_count"] == 1
    assert result["checks"]["run_finished_count"] == 1
    assert result["checks"]["terminal_is_last"] is True
    assert result["checks"]["completion_status"] == "error"
    assert result["checks"]["error_kind"] == "api_http_error"
    assert result["checks"]["private_canary_absent"] is True


@pytest.mark.parametrize("scenario", ["http_401", "http_403"])
def test_packaged_candidate_auth_errors_are_classified(tmp_path: Path, scenario: str) -> None:
    result = HerDebugLab(tmp_path / "lab").run_scenario(scenario, timeout_seconds=20)

    assert result["ok"] is True
    assert result["checks"]["returncode"] == 1
    assert result["checks"]["error_kind"] == "api_auth_error"
    assert result["checks"]["run_finished_count"] == 1
    assert result["checks"]["private_canary_absent"] is True


def test_packaged_candidate_sequential_steps_are_exactly_once(tmp_path: Path) -> None:
    result = HerDebugLab(tmp_path / "lab").run_scenario(
        "sequential_steps",
        target_steps=3,
        timeout_seconds=20,
    )

    assert result["ok"] is True
    assert result["checks"]["sequential_steps"] == {
        "accepted": 3,
        "target": 3,
        "event_count": 3,
        "ordered_steps": [1, 2, 3],
        "unique_token_hashes": 3,
    }
    assert result["checks"]["event_kinds"].count("tool_call") == 3
    assert result["checks"]["event_kinds"].count("tool_start") == 3
    assert result["checks"]["event_kinds"].count("tool_end") == 3


def test_packaged_candidate_hits_native_iteration_ceiling_exactly(tmp_path: Path) -> None:
    result = HerDebugLab(tmp_path / "lab").run_scenario(
        "iteration_ceiling",
        target_steps=12,
        max_iterations=12,
        timeout_seconds=20,
    )

    assert result["ok"] is True
    assert result["checks"]["provider_request_count"] == 12
    assert result["checks"]["iterations"] == 12
    assert result["checks"]["completion_status"] == "incomplete"
    assert result["checks"]["stop_reason"] == "max_iterations"
    assert result["checks"]["sequential_steps"]["accepted"] == 11
    assert result["checks"]["sequential_steps"]["ordered_steps"] == list(range(1, 12))


@pytest.mark.parametrize("scenario", ["malformed_sse", "truncated_sse"])
def test_packaged_candidate_stream_protocol_failure_names_last_safe_event(
    tmp_path: Path,
    scenario: str,
) -> None:
    result = HerDebugLab(tmp_path / "lab").run_scenario(scenario, timeout_seconds=20)

    assert result["ok"] is True
    assert result["checks"]["returncode"] == 1
    assert result["checks"]["error_kind"] == "stream_protocol_error"
    assert result["checks"]["last_safe_event"] == "run_started"
    assert result["checks"]["run_finished_count"] == 1
    assert result["checks"]["private_canary_absent"] is True
