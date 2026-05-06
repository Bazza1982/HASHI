import json
from types import SimpleNamespace

from orchestrator import runtime_wrapper


class _Logger:
    def __init__(self):
        self.messages = []

    def warning(self, message):
        self.messages.append(message)


def test_wrapper_metadata_fields_are_stable():
    runtime = SimpleNamespace(backend_manager=SimpleNamespace(agent_mode="wrapper"))
    result = SimpleNamespace(
        final_text="visible",
        wrapper_used=True,
        wrapper_failed=False,
        latency_ms=12.3456,
        fallback_reason=None,
    )

    assert runtime_wrapper.wrapper_audit_fields(runtime, result) == {
        "wrapper_mode": True,
        "wrapper_used": True,
        "wrapper_failed": False,
        "wrapper_latency_ms": 12.346,
        "wrapper_fallback_reason": None,
        "wrapper_final_chars": 7,
    }
    assert runtime_wrapper.wrapper_listener_fields("core", "visible", result) == {
        "core_raw": "core",
        "visible_text": "visible",
        "wrapper_used": True,
        "wrapper_failed": False,
        "wrapper_fallback_reason": None,
    }


def test_wrapper_verbose_excerpt_keeps_short_text_unchanged():
    assert runtime_wrapper.wrapper_verbose_excerpt("short", limit=20) == "short"


def test_core_memory_assistant_text_uses_core_when_wrapper_touched_output():
    runtime = SimpleNamespace(backend_manager=SimpleNamespace(agent_mode="wrapper"))
    result = SimpleNamespace(wrapper_used=True, wrapper_failed=False)

    assert runtime_wrapper.core_memory_assistant_text(runtime, "core", "visible", result) == "core"


def test_append_core_transcript_writes_expected_jsonl(tmp_path):
    runtime = SimpleNamespace(
        config=SimpleNamespace(active_backend="codex-cli"),
        core_transcript_log_path=tmp_path / "core_transcript.jsonl",
        error_logger=_Logger(),
        workspace_dir=tmp_path,
    )
    item = SimpleNamespace(source="text", request_id="req-1", summary="Summary")
    result = SimpleNamespace(wrapper_used=True, wrapper_failed=False, fallback_reason=None)

    runtime_wrapper.append_core_transcript(
        runtime,
        item,
        core_raw="core",
        visible_text="visible",
        completion_path="foreground",
        wrapper_result=result,
    )

    rows = (tmp_path / "core_transcript.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    entry = json.loads(rows[0])
    assert entry["role"] == "assistant_core"
    assert entry["text"] == "core"
    assert entry["visible_text"] == "visible"
    assert entry["source"] == "text"
    assert entry["completion_path"] == "foreground"
    assert entry["wrapper_used"] is True
