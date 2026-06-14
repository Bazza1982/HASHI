from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.grok_cli import GrokCLIAdapter
from adapters.stream_events import KIND_TEXT_DELTA


def _agent_config(tmp_path: Path):
    return SimpleNamespace(
        name="test",
        model="grok-build",
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

print(json.dumps({{"type": "session", "session_id": "sess-123", "summary": "started"}}), flush=True)
print(json.dumps({{"type": "assistant_delta", "content": {{"text": "Hel"}}}}), flush=True)
print(json.dumps({{"sessionUpdate": "agent_message_chunk", "content": {{"text": "lo"}}}}), flush=True)
print(json.dumps({{"type": "completed", "result": {{"text": "Hello"}}}}), flush=True)
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
    assert response.stream_metadata == {"grok_text_delta_count": 2}
    assert adapter._session_id == "sess-123"
    text_events = [event for event in events if event.kind == KIND_TEXT_DELTA]
    assert [event.summary for event in text_events] == ["Hel", "lo"]


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
