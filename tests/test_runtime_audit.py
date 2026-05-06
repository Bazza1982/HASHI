import json
import logging
from types import SimpleNamespace

from orchestrator import runtime_audit
from orchestrator.audit_mode import AuditResult, AuditTelemetryCollector


def _runtime(tmp_path, *, mode: str = "audit"):
    return SimpleNamespace(
        backend_manager=SimpleNamespace(
            agent_mode=mode,
            get_state_snapshot=lambda: {},
        ),
        config=SimpleNamespace(active_backend="codex-cli"),
        workspace_dir=tmp_path,
        error_logger=logging.getLogger("test.runtime-audit"),
        get_current_model=lambda: "gpt-test",
        _wrapper_visible_context=lambda context_window: [{"role": "user", "text": "recent", "source": "text"}],
    )


def _item(**overrides):
    values = {
        "request_id": "req/audit 1",
        "source": "text",
        "summary": "Audit summary",
        "silent": False,
        "prompt": "Original user request.",
        "chat_id": 123,
        "deliver_to_telegram": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_audit_enabled_tracks_agent_mode(tmp_path):
    assert runtime_audit.audit_enabled(_runtime(tmp_path, mode="audit")) is True
    assert runtime_audit.audit_enabled(_runtime(tmp_path, mode="flex")) is False


def test_build_audit_telemetry_falls_back_to_stream_action_count(tmp_path):
    collector = AuditTelemetryCollector()
    collector.events = [
        {
            "kind": "shell_exec",
            "summary": "ran command",
            "detail": "",
            "tool_name": "shell",
            "file_path": "",
            "timestamp": 1.0,
        }
    ]
    runtime = _runtime(tmp_path)
    response = SimpleNamespace(tool_call_count=None, tool_loop_count=2, usage=None)

    telemetry = runtime_audit.build_audit_telemetry(runtime, _item(), response, collector)

    assert telemetry["tool_call_count"] == 1
    assert telemetry["tool_loop_count"] == 2
    assert telemetry["risk_flags"]["codex_cli_backend"] is True


def test_write_audit_evidence_excludes_prompt_injection_body(tmp_path):
    runtime = _runtime(tmp_path)
    item = _item(prompt="User prompt only.")
    injection_body = "Private Anatta guidance must not appear in evidence."

    path = runtime_audit.write_audit_evidence(
        runtime,
        item,
        core_raw="Core answer influenced by Anatta.",
        visible_text="Visible answer.",
        telemetry={"prompt_sections": [{"title": "ANATTA", "chars": len(injection_body)}]},
        completion_path="foreground",
        audit_criteria={"1": "Flag risky claims."},
        visible_context=[{"role": "user", "text": "recent visible only", "source": "text"}],
    )

    text = path and open(path, encoding="utf-8").read()
    assert path
    assert "User prompt only." in text
    assert injection_body not in text
    entry = json.loads(text)
    assert entry["role"] == "audit_evidence"
    assert entry["user_request"] == "User prompt only."
    assert "final_prompt" not in entry


def test_append_audit_transcript_writes_expected_jsonl(tmp_path):
    runtime = _runtime(tmp_path)
    result = AuditResult(status="warn", max_severity="medium", summary="Needs review", audit_used=True)

    runtime_audit.append_audit_transcript(
        runtime,
        _item(),
        core_raw="core",
        visible_text="visible",
        telemetry={"tool_call_count": 1},
        audit_result=result,
        completion_path="foreground",
    )

    rows = (tmp_path / "audit_transcript.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    entry = json.loads(rows[0])
    assert entry["role"] == "audit"
    assert entry["core_raw"] == "core"
    assert entry["visible_text"] == "visible"
    assert entry["audit_result"]["status"] == "warn"
