"""Focused tests for the Antigravity CLI (agy) backend adapter.

Runs against the deterministic mock in tests/mocks/bin/agy, which emulates the
agy 1.2.3 headless contract verified on 2026-09-16 (stream-json NDJSON events,
json whole-response mode, exit-0-with-ERROR payloads, --conversation resume).
"""

from __future__ import annotations

import os
import json
import sys
import tempfile
import asyncio
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.antigravity_cli import AntigravityCLIAdapter
from adapters.registry import get_backend_class
from orchestrator.api_gateway_preflight import check_gateway_engine
from orchestrator.backend_preflight import BackendPreflight
from orchestrator.flexible_backend_registry import BACKEND_REGISTRY, CLI_ENGINES
from orchestrator.pathing import resolve_agy_executable

_MOCK_BIN = Path(__file__).resolve().parent / "mocks" / "bin"
if os.name == "nt":
    # The POSIX shell mock cannot be executed by native Windows CreateProcess
    # (WinError 193). Generate an agy.cmd wrapper that runs the cross-platform
    # Python mock through the adapter's own COMSPEC invocation path.
    _win_mock_home = Path(tempfile.gettempdir()) / "hashi-antigravity-mock"
    _win_mock_home.mkdir(parents=True, exist_ok=True)
    MOCK_AGY = _win_mock_home / "agy.cmd"
    _mock_py = Path(__file__).resolve().parent / "mocks" / "agy_mock.py"
    MOCK_AGY.write_text(
        "@echo off\r\n"
        f'"{sys.executable}" "{_mock_py}" %*\r\n',
        encoding="utf-8",
    )
else:
    MOCK_AGY = _MOCK_BIN / "agy"
MOCK_CID = "11111111-2222-3333-4444-555555555555"


def run(coro):
    return asyncio.run(coro)


def make_config(tmp_path: Path, extra=None):
    return SimpleNamespace(
        name="antigravity-test-agent",
        engine="antigravity-cli",
        model="gemini-3.8-flash-high",
        workspace_dir=tmp_path,
        extra=dict(extra or {}),
        resolve_access_root=lambda: tmp_path,
    )


def make_global():
    return SimpleNamespace(agy_cmd=str(MOCK_AGY))


def make_adapter(tmp_path: Path, extra=None):
    return AntigravityCLIAdapter(make_config(tmp_path, extra), make_global())


# ----------------------------------------------------------------- registry


def test_registry_entry_and_cli_engines():
    entry = BACKEND_REGISTRY["antigravity-cli"]
    assert entry["label"] == "antigravity"
    assert entry["default_model"] == "gemini-3.8-flash-high"
    assert entry["secret_keys"] == []
    assert len(entry["models"]) == 14
    assert "gemini-3.8-flash-high" in entry["models"]
    assert "gpt-oss-120b-medium" in entry["models"]
    assert "antigravity-cli" in CLI_ENGINES


def test_adapter_class_registered():
    assert get_backend_class("antigravity-cli") is AntigravityCLIAdapter


def test_timeout_constants_and_capabilities(tmp_path):
    adapter = make_adapter(tmp_path)
    assert AntigravityCLIAdapter.DEFAULT_IDLE_TIMEOUT_SEC == 60 * 60
    assert AntigravityCLIAdapter.USES_LEGACY_HARD_TIMEOUT is False
    assert adapter.capabilities.supports_sessions is True
    assert adapter.capabilities.supports_headless_mode is True
    assert adapter.capabilities.supports_tool_use is True


def test_global_config_exposes_agy_cmd_field():
    from orchestrator.config import GlobalConfig

    names = {f.name for f in fields(GlobalConfig)}
    assert "agy_cmd" in names


# --------------------------------------------------------------- discovery


def test_resolve_agy_executable_prefers_localappdata(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    agy = tmp_path / "agy" / "bin" / "agy.exe"
    agy.parent.mkdir(parents=True)
    agy.write_text("stub", encoding="utf-8")
    assert resolve_agy_executable("") == str(agy)
    assert resolve_agy_executable(None) == str(agy)


def test_resolve_agy_executable_prefers_explicit_file(tmp_path):
    agy = tmp_path / "my-agy"
    agy.write_text("#!/bin/sh\necho ok", encoding="utf-8")
    assert resolve_agy_executable(str(agy)) == str(agy)


# -------------------------------------------------------------- initialize


def test_initialize_ok_with_mock(tmp_path):
    adapter = make_adapter(tmp_path)
    assert run(adapter.initialize()) is True


def test_initialize_missing_binary_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-such-dir"))
    monkeypatch.setenv("PATH", str(tmp_path))
    adapter = AntigravityCLIAdapter(
        make_config(tmp_path),
        SimpleNamespace(agy_cmd=str(tmp_path / "no-such-agy")),
    )
    assert run(adapter.initialize()) is False


# --------------------------------------------------------------- roundtrip


def test_stream_json_roundtrip_and_conversation_persist(tmp_path, monkeypatch):
    log = tmp_path / "argv.log"
    monkeypatch.setenv("AGY_MOCK_LOG", str(log))
    adapter = make_adapter(tmp_path)
    assert run(adapter.initialize()) is True

    resp = run(adapter.generate_response("hello there", "req-1"))
    assert resp.is_success is True
    assert resp.text == "PONG"
    assert resp.stream_metadata["conversation_id"] == MOCK_CID
    assert resp.stream_metadata["num_turns"] == 1
    assert resp.usage is not None and resp.usage.input_tokens == 10
    assert adapter._conversation_id == MOCK_CID
    assert (tmp_path / ".hashi-antigravity-session.json").exists()

    resp2 = run(adapter.generate_response("hello again", "req-2"))
    assert resp2.is_success is True

    argv_lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(argv_lines) == 2
    assert "--conversation" not in argv_lines[0]
    assert f"--conversation {MOCK_CID}" in argv_lines[1]


def test_conversation_restored_across_instances(tmp_path, monkeypatch):
    log = tmp_path / "argv.log"
    monkeypatch.setenv("AGY_MOCK_LOG", str(log))
    adapter = make_adapter(tmp_path)
    assert run(adapter.initialize()) is True
    run(adapter.generate_response("hello", "req-1"))

    adapter2 = make_adapter(tmp_path)
    assert run(adapter2.initialize()) is True
    run(adapter2.generate_response("hello again", "req-2"))

    argv_lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(argv_lines) == 2
    assert "--conversation" not in argv_lines[0]
    assert f"--conversation {MOCK_CID}" in argv_lines[1]


def test_json_mode_fallback_with_noise_line(tmp_path, monkeypatch):
    monkeypatch.setenv("AGY_MOCK_NOISE_STDOUT", "1")
    adapter = make_adapter(tmp_path, extra={"output_format": "json"})
    assert run(adapter.initialize()) is True
    resp = run(adapter.generate_response("hello", "req-1"))
    assert resp.is_success is True
    assert resp.text == "PONG"
    assert resp.stream_metadata["conversation_id"] == MOCK_CID


def test_error_status_authoritative_even_with_exit_zero(tmp_path):
    adapter = make_adapter(tmp_path)
    assert run(adapter.initialize()) is True
    resp = run(adapter.generate_response("please error out", "req-1"))
    assert resp.is_success is False
    assert "Simulated agy error" in (resp.error or "")


def test_nonzero_exit_without_payload_reports_stderr(tmp_path):
    adapter = make_adapter(tmp_path)
    assert run(adapter.initialize()) is True
    resp = run(adapter.generate_response("please crash", "req-1"))
    assert resp.is_success is False
    assert "crashed" in (resp.error or "")


def test_new_session_clears_persistence(tmp_path, monkeypatch):
    log = tmp_path / "argv.log"
    monkeypatch.setenv("AGY_MOCK_LOG", str(log))
    adapter = make_adapter(tmp_path)
    assert run(adapter.initialize()) is True
    run(adapter.generate_response("hello", "req-1"))
    run(adapter.handle_new_session())
    assert adapter._conversation_id is None
    assert not (tmp_path / ".hashi-antigravity-session.json").exists()
    run(adapter.generate_response("hello again", "req-2"))
    argv_lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert "--conversation" not in argv_lines[1]


def test_session_mode_off_never_resumes(tmp_path, monkeypatch):
    log = tmp_path / "argv.log"
    monkeypatch.setenv("AGY_MOCK_LOG", str(log))
    adapter = make_adapter(tmp_path)
    adapter.set_session_mode(False)
    assert run(adapter.initialize()) is True
    run(adapter.generate_response("hello", "req-1"))
    argv_lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert all("--conversation" not in line for line in argv_lines)


def test_empty_prompt_rejected(tmp_path):
    adapter = make_adapter(tmp_path)
    resp = run(adapter.generate_response("   ", "req-1"))
    assert resp.is_success is False
    assert "Empty prompt" in (resp.error or "")


@pytest.mark.parametrize("body", ["x" * 4000, "汉" * 40000], ids=["short", "long-cjk"])
def test_prompt_is_delivered_verbatim_over_stdin_not_argv(tmp_path, monkeypatch, body):
    argv_log = tmp_path / "argv.log"
    input_log = tmp_path / "input.jsonl"
    monkeypatch.setenv("AGY_MOCK_LOG", str(argv_log))
    monkeypatch.setenv("AGY_MOCK_INPUT_LOG", str(input_log))
    prompt = "permanent_system:DO_NOT_OMIT\n" + body + "\ncurrent_user_request:保留全文"
    adapter = make_adapter(tmp_path)
    response = run(adapter.generate_response(prompt, "req-stdin"))
    assert response.is_success is True, response.error
    assert json.loads(input_log.read_text(encoding="utf-8"))["prompt"] == prompt
    argv = argv_log.read_text(encoding="utf-8")
    assert "permanent_system" not in argv
    assert "current_user_request" not in argv
    assert "--input-format stream-json" in argv
    transport = response.stream_metadata["prompt_transport"]
    assert transport["transport"] == "stdin_stream_json"
    assert transport["degraded"] is False
    assert transport["original_bytes"] == transport["sent_bytes"] == len(prompt.encode("utf-8"))
    assert transport["omitted_source_bytes"] == 0
    assert transport["pipe_write_state"] == "complete"
    assert transport["pipe_write_complete"] is True
    assert transport["provider_consumption"] == "confirmed_by_result"


def test_stdin_is_written_in_bounded_chunks(tmp_path, monkeypatch):
    class RecordingStdin:
        def __init__(self):
            self.chunks = []
            self.closed = False

        def write(self, data):
            self.chunks.append(bytes(data))

        async def drain(self):
            return None

        def close(self):
            self.closed = True

    async def exercise():
        stdout = asyncio.StreamReader()
        stderr = asyncio.StreamReader()
        stdout.feed_data(
            (
                '{"event":"result","result":{"conversation_id":"cid",'
                '"status":"SUCCESS","response":"PONG","duration_seconds":0,'
                '"num_turns":1,"usage":{"input_tokens":1,'
                '"output_tokens":1,"thinking_tokens":0,"total_tokens":2}}}\n'
            ).encode()
        )
        stdout.feed_eof()
        stderr.feed_eof()

        class Proc:
            pid = 424242
            returncode = None
            stdin = RecordingStdin()

            async def wait(self):
                self.returncode = 0
                return 0

        proc = Proc()

        async def fake_spawn(*args, **kwargs):
            proc.stdout = stdout
            proc.stderr = stderr
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
        monkeypatch.setattr(AntigravityCLIAdapter, "STDIN_WRITE_CHUNK_BYTES", 32)
        adapter = make_adapter(tmp_path)
        prompt = "汉" * 200
        response = await adapter.generate_response(prompt, "req-chunks")
        return proc, prompt, response, adapter

    proc, prompt, response, adapter = run(exercise())
    assert response.is_success is True
    assert proc.stdin.closed is True
    assert max(map(len, proc.stdin.chunks)) <= 32
    envelope = json.loads(b"".join(proc.stdin.chunks).decode("utf-8"))
    assert envelope["message"]["content"] == prompt
    assert response.stream_metadata["prompt_transport"]["pipe_write_complete"] is True
    assert adapter.current_proc is None
    assert adapter._active_read_tasks == []


def test_stdin_failure_kills_process_and_reports_unconfirmed_delivery(tmp_path, monkeypatch):
    class FailingStdin:
        def __init__(self):
            self.closed = False

        def write(self, data):
            return None

        async def drain(self):
            raise OSError("simulated stdin failure")

        def close(self):
            self.closed = True

    async def exercise():
        stdout = asyncio.StreamReader()
        stderr = asyncio.StreamReader()
        stopped = asyncio.Event()

        class Proc:
            pid = 424243
            returncode = None
            stdin = FailingStdin()

            async def wait(self):
                await stopped.wait()
                return self.returncode

        proc = Proc()
        proc.stdout = stdout
        proc.stderr = stderr
        killed = []

        async def fake_spawn(*args, **kwargs):
            return proc

        async def fake_kill(target, logger=None, reason=""):
            killed.append(reason)
            target.returncode = -9
            stdout.feed_eof()
            stderr.feed_eof()
            stopped.set()
            return True

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
        adapter = make_adapter(tmp_path)
        monkeypatch.setattr(adapter, "force_kill_process_tree", fake_kill)
        response = await adapter.generate_response("hello", "req-write-fail")
        return proc, killed, response, adapter

    proc, killed, response, adapter = run(exercise())
    assert response.is_success is False
    assert "simulated stdin failure" in (response.error or "")
    assert killed == ["transport-error:req-write-fail"]
    assert proc.stdin.closed is True
    assert response.side_effects_possible is True
    transport = response.stream_metadata["prompt_transport"]
    assert transport["pipe_write_state"] == "writing"
    assert transport["pipe_write_complete"] is False
    assert transport["sent_bytes"] is None
    assert transport["provider_consumption"] == "unconfirmed"
    assert adapter.current_proc is None
    assert adapter._active_read_tasks == []


def test_hollow_success_detected_in_json_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("AGY_MOCK_HOLLOW", "1")
    adapter = make_adapter(tmp_path, extra={"output_format": "json"})
    assert run(adapter.initialize()) is True
    resp = run(adapter.generate_response("hollow me", "req-1"))
    assert resp.is_success is False
    assert "empty result" in (resp.error or "")


def test_hollow_success_detected_in_stream_json_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("AGY_MOCK_HOLLOW", "1")
    adapter = make_adapter(tmp_path)
    assert run(adapter.initialize()) is True
    resp = run(adapter.generate_response("hollow me", "req-1"))
    assert resp.is_success is False
    assert "empty result" in (resp.error or "")


def test_stale_conversation_rebuild_retry(tmp_path, monkeypatch):
    marker = tmp_path / "stale-once"
    monkeypatch.setenv("AGY_MOCK_STALE_ONCE", str(marker))
    log = tmp_path / "argv.log"
    monkeypatch.setenv("AGY_MOCK_LOG", str(log))
    adapter = make_adapter(tmp_path)
    assert run(adapter.initialize()) is True
    resp0 = run(adapter.generate_response("prime", "req-0"))
    assert resp0.is_success is True
    resp1 = run(adapter.generate_response("stale please", "req-1"))
    assert resp1.is_success is True
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    assert "--conversation" in lines[1]
    assert "--conversation" not in lines[2]


def test_stale_marker_without_resumed_conversation_does_not_retry(tmp_path, monkeypatch):
    marker = tmp_path / "stale-once"
    log = tmp_path / "argv.log"
    monkeypatch.setenv("AGY_MOCK_STALE_ONCE", str(marker))
    monkeypatch.setenv("AGY_MOCK_LOG", str(log))
    adapter = make_adapter(tmp_path)
    response = run(adapter.generate_response("stale please", "req-initial"))
    assert response.is_success is False
    assert len(log.read_text(encoding="utf-8").strip().splitlines()) == 1
    assert adapter._conversation_id is None


def test_stale_after_tool_activity_is_not_retried(tmp_path, monkeypatch):
    log = tmp_path / "argv.log"
    monkeypatch.setenv("AGY_MOCK_LOG", str(log))
    adapter = make_adapter(tmp_path)
    assert run(adapter.generate_response("prime", "req-prime")).is_success is True
    monkeypatch.setenv("AGY_MOCK_STALE_WITH_TOOL", "1")
    response = run(adapter.generate_response("stale with tool", "req-stale-tool"))
    assert response.is_success is False
    assert response.side_effects_possible is True
    assert response.stream_metadata["stale_retry_suppressed"] == (
        "effects_not_proven_absent"
    )
    observation = response.stream_metadata["antigravity_attempt"]
    assert observation["tool_activity_observed"] is True
    assert len(log.read_text(encoding="utf-8").strip().splitlines()) == 2
    assert adapter._conversation_id is None


def test_stale_after_answer_output_is_not_retried(tmp_path, monkeypatch):
    log = tmp_path / "argv.log"
    monkeypatch.setenv("AGY_MOCK_LOG", str(log))
    adapter = make_adapter(tmp_path)
    assert run(adapter.generate_response("prime", "req-prime")).is_success is True
    monkeypatch.setenv("AGY_MOCK_STALE_WITH_ANSWER", "1")
    response = run(adapter.generate_response("stale with answer", "req-stale-answer"))
    assert response.is_success is False
    assert response.side_effects_possible is True
    observation = response.stream_metadata["antigravity_attempt"]
    assert observation["answer_output_observed"] is True
    assert len(log.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_stale_conversation_marker_matching(tmp_path):
    adapter = make_adapter(tmp_path)
    assert adapter._stale_conversation_error("Error: conversation not found") is True
    assert (
        adapter._stale_conversation_error(
            "Please sign in to view available models."
        )
        is False
    )


def test_idle_timeout_kills_process(tmp_path, monkeypatch):
    monkeypatch.setattr(AntigravityCLIAdapter, "DEFAULT_IDLE_TIMEOUT_SEC", 1)
    adapter = make_adapter(tmp_path)
    assert run(adapter.initialize()) is True
    resp = run(adapter.generate_response("slow answer please", "req-1"))
    assert resp.is_success is False
    assert "idle" in (resp.error or "")


def test_cancel_propagates_and_kills(tmp_path):
    adapter = make_adapter(tmp_path)
    assert run(adapter.initialize()) is True

    async def _cancel():
        task = asyncio.create_task(adapter.generate_response("slow answer please", "req-1"))
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(_cancel())
    assert adapter.current_proc is None
    assert adapter._active_read_tasks == []


# -------------------------------------------------------------- preflights


def test_gateway_preflight_antigravity_available(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-such-dir"))
    cfg = SimpleNamespace(agy_cmd=str(MOCK_AGY))
    status = check_gateway_engine(cfg, {}, "antigravity-cli")
    assert status["available"] is True
    assert MOCK_AGY.name in status["reason"]


def test_gateway_preflight_antigravity_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-such-dir"))
    monkeypatch.setenv("PATH", "")
    cfg = SimpleNamespace(agy_cmd=str(tmp_path / "no-such-agy"))
    status = check_gateway_engine(cfg, {}, "antigravity-cli")
    assert status["available"] is False


def test_backend_preflight_antigravity_available(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-such-dir"))
    preflight = BackendPreflight()
    global_cfg = SimpleNamespace(
        claude_cmd="claude",
        codex_cmd="codex",
        grok_cmd="grok",
        agy_cmd=str(MOCK_AGY),
    )
    agent = SimpleNamespace(engine="antigravity-cli")
    status = preflight.check_backend_availability(global_cfg, [agent], {})
    assert status["antigravity-cli"][0] is True


# ------------------------------------------------------------ plan A launcher


def test_launch_mode_defaults_to_direct(tmp_path):
    adapter = make_adapter(tmp_path)
    assert adapter._launch_mode == "direct"


def test_launcher_argv_direct_mode_keeps_command_unchanged(tmp_path):
    adapter = make_adapter(tmp_path)
    cmd = [str(MOCK_AGY), "-p", "hello", "--output-format", "json"]
    assert adapter._launcher_argv(cmd) == tuple(cmd)


@pytest.mark.skipif(os.name != "nt", reason="user-session launcher is Windows-only")
def test_launcher_argv_user_session_prefixes_launcher(tmp_path):
    global_cfg = SimpleNamespace(
        agy_cmd=str(MOCK_AGY), agy_launch_mode="user-session"
    )
    adapter = AntigravityCLIAdapter(make_config(tmp_path), global_cfg)
    assert adapter._launch_mode == "user-session"
    cmd = [str(MOCK_AGY), "-p", "hello"]
    argv = adapter._launcher_argv(cmd)
    assert argv[0] == sys.executable
    assert argv[1].replace("\\", "/").endswith(
        "adapters/agy_user_session_launcher.py"
    )
    assert Path(argv[1]).is_file()
    if argv[2] == "--cwd":
        assert argv[3] == str(tmp_path)
        assert argv[4] == "--"
        assert tuple(argv[5:]) == tuple(cmd)
    else:
        assert argv[2] == "--"
        assert tuple(argv[3:]) == tuple(cmd)


def test_launch_mode_unknown_falls_back_to_direct(tmp_path):
    global_cfg = SimpleNamespace(agy_cmd=str(MOCK_AGY), agy_launch_mode="nonsense")
    adapter = AntigravityCLIAdapter(make_config(tmp_path), global_cfg)
    assert adapter._launch_mode == "direct"


def test_global_config_exposes_agy_launch_mode_field():
    from orchestrator.config import GlobalConfig

    names = {f.name for f in fields(GlobalConfig)}
    assert "agy_launch_mode" in names


def test_init_event_emits_verbose_renderable_progress(tmp_path):
    """The init event must also emit a progress line the verbose digest can
    actually render (its summary carries the digest "still working" marker)."""
    from adapters.stream_events import KIND_PROGRESS

    adapter = make_adapter(tmp_path)
    events = []

    async def collect(se):
        events.append(se)

    async def parse():
        done, err = adapter._parse_stream_json_line(
            '{"event": "init", "conversation_id": "abc-123"}', collect, []
        )
        assert done is False and err == ""
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    run(parse())
    progresses = [e for e in events if e.kind == KIND_PROGRESS]
    assert any(e.summary == "Antigravity task started" for e in progresses)
    assert any("still working" in (e.summary or "") for e in progresses)
