from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.grok_cli import GrokCLIAdapter
from adapters.stream_events import KIND_FILE_EDIT, KIND_FILE_READ, KIND_SHELL_EXEC, KIND_TEXT_DELTA, KIND_THINKING


def _agent_config(tmp_path: Path):
    return SimpleNamespace(
        name="test",
        model="grok-composer-2.5-fast",
        workspace_dir=tmp_path,
        system_md=None,
        extra={},
        resolve_access_root=lambda: tmp_path,
    )


def _write_fake_grok(tmp_path: Path, *, fail_version: bool = False) -> Path:
    script = tmp_path / "grok"
    version_block = (
        "if '--version' in sys.argv:\n"
        "    print('grok 0.1.0')\n"
        "    raise SystemExit(0)\n"
        if not fail_version
        else "if '--version' in sys.argv:\n"
        "    print('not logged in', file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
    )
    script.write_text(
        f"""#!{sys.executable}
import json
import sys

{version_block}

prompt = sys.argv[-1] if sys.argv else ''
if 'fail' in prompt:
    print('grok failed', file=sys.stderr)
    raise SystemExit(7)
if 'empty-retry-ok' in prompt:
    if 'previous Grok CLI response ended without any answer text' in prompt:
        print(json.dumps({{"type": "session", "session_id": "sess-retry", "summary": "started"}}), flush=True)
        print(json.dumps({{"type": "text", "data": "Retry"}}), flush=True)
        print(json.dumps({{"type": "end", "sessionId": "sess-retry", "stopReason": "EndTurn"}}), flush=True)
        raise SystemExit(0)
    print(json.dumps({{"type": "session", "session_id": "sess-empty", "summary": "started"}}), flush=True)
    print(json.dumps({{"type": "thought", "data": "thinking only"}}), flush=True)
    print(json.dumps({{"type": "end", "sessionId": "sess-empty", "stopReason": "EndTurn"}}), flush=True)
    raise SystemExit(0)
if 'empty' in prompt:
    if 'side-effect' in prompt:
        print(json.dumps({{"type": "session", "session_id": "sess-side-effect", "summary": "started"}}), flush=True)
        print(json.dumps({{"type": "thought", "data": "thinking only"}}), flush=True)
        print(json.dumps({{"type": "tool_start", "name": "shell"}}), flush=True)
        print(json.dumps({{"type": "end", "sessionId": "sess-side-effect", "stopReason": "EndTurn"}}), flush=True)
        raise SystemExit(0)
    print(json.dumps({{"type": "session", "session_id": "sess-empty", "summary": "started"}}), flush=True)
    print(json.dumps({{"type": "thought", "data": "thinking only"}}), flush=True)
    print(json.dumps({{"type": "end", "sessionId": "sess-empty", "stopReason": "EndTurn"}}), flush=True)
    raise SystemExit(0)
if 'tool-events' in prompt:
    print(json.dumps({{"type": "session", "session_id": "sess-tool", "summary": "started"}}), flush=True)
    print(json.dumps({{"type": "tool_call", "name": "Read", "path": "README.md"}}), flush=True)
    print(json.dumps({{"type": "tool_call", "name": "Shell", "command": "mkdir -p demo"}}), flush=True)
    print(json.dumps({{"type": "tool_call", "name": "Write", "path": "demo/file.txt"}}), flush=True)
    print(json.dumps({{"type": "text", "data": "done"}}), flush=True)
    print(json.dumps({{"type": "end", "sessionId": "sess-tool", "stopReason": "EndTurn"}}), flush=True)
    raise SystemExit(0)

print(json.dumps({{"type": "session", "session_id": "sess-123", "summary": "started"}}), flush=True)
print(json.dumps({{"type": "thought", "data": "thinking"}}), flush=True)
print(json.dumps({{"type": "text", "data": "Hel"}}), flush=True)
print(json.dumps({{"sessionUpdate": "agent_message_chunk", "content": {{"text": "lo"}}}}), flush=True)
print(json.dumps({{"type": "end", "sessionId": "sess-123", "stopReason": "EndTurn"}}), flush=True)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


@pytest.mark.asyncio
async def test_grok_initialize_checks_cli_version(tmp_path):
    fake_grok = _write_fake_grok(tmp_path)
    adapter = GrokCLIAdapter(_agent_config(tmp_path), SimpleNamespace(grok_cmd=str(fake_grok)))

    assert await adapter.initialize() is True


@pytest.mark.asyncio
async def test_grok_initialize_fails_when_cli_version_fails(tmp_path):
    fake_grok = _write_fake_grok(tmp_path, fail_version=True)
    adapter = GrokCLIAdapter(_agent_config(tmp_path), SimpleNamespace(grok_cmd=str(fake_grok)))

    assert await adapter.initialize() is False


def test_grok_build_cmd_defaults_to_execution_ready_flags(tmp_path):
    adapter = GrokCLIAdapter(_agent_config(tmp_path), SimpleNamespace(grok_cmd="grok"))

    cmd = adapter._build_cmd("do work")

    assert "--permission-mode" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--always-approve" in cmd
    assert "--check" in cmd


def test_grok_build_cmd_allows_agent_overrides(tmp_path):
    cfg = _agent_config(tmp_path)
    cfg.extra = {
        "grok_permission_mode": "default",
        "grok_always_approve": False,
        "grok_check": False,
        "grok_tools": "Read,Write",
        "grok_deny": "Shell(*)",
    }
    adapter = GrokCLIAdapter(cfg, SimpleNamespace(grok_cmd="grok"))

    cmd = adapter._build_cmd("do work")

    assert cmd[cmd.index("--permission-mode") + 1] == "default"
    assert "--always-approve" not in cmd
    assert "--check" not in cmd
    assert cmd[cmd.index("--tools") + 1] == "Read,Write"
    assert cmd[cmd.index("--deny") + 1] == "Shell(*)"


@pytest.mark.asyncio
async def test_grok_streaming_json_reconstructs_final_answer_and_emits_deltas(tmp_path):
    fake_grok = _write_fake_grok(tmp_path)
    adapter = GrokCLIAdapter(_agent_config(tmp_path), SimpleNamespace(grok_cmd=str(fake_grok)))
    events = []

    async def collect(event):
        events.append(event)

    response = await adapter.generate_response(
        "say hello",
        "req-grok",
        is_retry=False,
        silent=False,
        on_stream_event=collect,
    )

    assert response.is_success is True
    assert response.text == "Hello"
    assert response.stream_metadata == {"grok_text_delta_count": 2, "grok_action_event_count": 0}
    assert response.tool_call_count == 0
    assert adapter._session_id == "sess-123"
    text_events = [event for event in events if event.kind == KIND_TEXT_DELTA]
    assert [event.summary for event in text_events] == ["Hel", "lo"]
    thinking_events = [event for event in events if event.kind == KIND_THINKING]
    assert [event.summary for event in thinking_events] == ["thinking"]


@pytest.mark.asyncio
async def test_grok_tool_events_map_to_hashi_stream_events_and_counts(tmp_path):
    fake_grok = _write_fake_grok(tmp_path)
    adapter = GrokCLIAdapter(_agent_config(tmp_path), SimpleNamespace(grok_cmd=str(fake_grok)))
    events = []

    async def collect(event):
        events.append(event)

    response = await adapter.generate_response(
        "please emit tool-events",
        "req-grok-tools",
        is_retry=False,
        silent=False,
        on_stream_event=collect,
    )

    assert response.is_success is True
    assert response.text == "done"
    assert response.tool_call_count == 3
    assert response.stream_metadata["grok_action_event_count"] == 3
    by_kind = [event.kind for event in events]
    assert KIND_FILE_READ in by_kind
    assert KIND_SHELL_EXEC in by_kind
    assert KIND_FILE_EDIT in by_kind


@pytest.mark.asyncio
async def test_grok_nonzero_exit_preserves_partial_text_and_error(tmp_path):
    fake_grok = _write_fake_grok(tmp_path)
    adapter = GrokCLIAdapter(_agent_config(tmp_path), SimpleNamespace(grok_cmd=str(fake_grok)))

    response = await adapter.generate_response(
        "please fail",
        "req-grok-fail",
        is_retry=False,
        silent=False,
        on_stream_event=None,
    )

    assert response.is_success is False
    assert "grok failed" in response.error


@pytest.mark.asyncio
async def test_grok_zero_exit_empty_answer_is_failure_with_diagnostic(tmp_path):
    fake_grok = _write_fake_grok(tmp_path)
    adapter = GrokCLIAdapter(_agent_config(tmp_path), SimpleNamespace(grok_cmd=str(fake_grok)))

    response = await adapter.generate_response(
        "please empty",
        "req-grok-empty",
        is_retry=False,
        silent=False,
        on_stream_event=None,
    )

    assert response.is_success is False
    assert response.text == ""
    assert "Grok CLI returned no answer text after one retry" in response.error
    assert "empty_answer_pattern=thought_end_no_text" in response.error
    assert "stop_reason=EndTurn" in response.error
    assert response.stream_metadata["grok_empty_answer_retry_attempted"] is True
    assert response.stream_metadata["grok_empty_answer_retry_succeeded"] is False


@pytest.mark.asyncio
async def test_grok_empty_thought_end_retries_once_and_succeeds(tmp_path):
    fake_grok = _write_fake_grok(tmp_path)
    adapter = GrokCLIAdapter(_agent_config(tmp_path), SimpleNamespace(grok_cmd=str(fake_grok)))

    response = await adapter.generate_response(
        "please empty-retry-ok",
        "req-grok-empty-retry",
        is_retry=False,
        silent=False,
        on_stream_event=None,
    )

    assert response.is_success is True
    assert response.text == "Retry"
    assert response.stream_metadata["grok_empty_answer_retry_attempted"] is True
    assert response.stream_metadata["grok_empty_answer_retry_succeeded"] is True
    assert response.stream_metadata["grok_empty_answer_pattern"] == "thought_end_no_text"


@pytest.mark.asyncio
async def test_grok_empty_answer_skips_retry_when_is_retry_true(tmp_path):
    fake_grok = _write_fake_grok(tmp_path)
    adapter = GrokCLIAdapter(_agent_config(tmp_path), SimpleNamespace(grok_cmd=str(fake_grok)))

    response = await adapter.generate_response(
        "please empty",
        "req-grok-empty-no-retry",
        is_retry=True,
        silent=False,
        on_stream_event=None,
    )

    assert response.is_success is False
    assert response.text == ""
    assert "after one retry" not in response.error
    assert response.stream_metadata["grok_empty_answer_pattern"] == "thought_end_no_text"
    assert "grok_empty_answer_retry_attempted" not in response.stream_metadata


@pytest.mark.asyncio
async def test_grok_empty_answer_does_not_retry_after_side_effect_events(tmp_path):
    fake_grok = _write_fake_grok(tmp_path)
    adapter = GrokCLIAdapter(_agent_config(tmp_path), SimpleNamespace(grok_cmd=str(fake_grok)))

    response = await adapter.generate_response(
        "please empty side-effect",
        "req-grok-empty-side-effect",
        is_retry=False,
        silent=False,
        on_stream_event=None,
    )

    assert response.is_success is False
    assert response.text == ""
    assert "after one retry" not in response.error
    assert response.stream_metadata["grok_empty_answer_pattern"] == "side_effect_events_no_text"
    assert "grok_empty_answer_retry_attempted" not in response.stream_metadata
