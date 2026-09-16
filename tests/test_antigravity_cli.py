"""Focused tests for the Antigravity CLI (agy) backend adapter.

Runs against the deterministic mock in tests/mocks/bin/agy, which emulates the
agy 1.2.3 headless contract verified on 2026-09-16 (stream-json NDJSON events,
json whole-response mode, exit-0-with-ERROR payloads, --conversation resume).
"""

from __future__ import annotations

import os
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
    # gemini-cli dual-track entry must stay untouched
    gemini = BACKEND_REGISTRY["gemini-cli"]
    assert gemini["label"] == "gemini"
    assert gemini["default_model"] == "gemini-2.5-flash"
    assert gemini["secret_keys"] == ["gemini-cli_key"]


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


def test_overlong_prompt_fits_argv_limit(tmp_path, monkeypatch):
    adapter = make_adapter(tmp_path)
    fitted = adapter._fit_prompt_for_argv("x" * 25000)
    assert len(fitted.encode("utf-8")) <= AntigravityCLIAdapter.MAX_PROMPT_BYTES
    assert "truncated by the antigravity-cli adapter" in fitted
    assert fitted.endswith("x" * 200)
    # The .cmd mock travels through cmd.exe, whose command line is limited
    # to 8191 chars, so exercise the fitting path end-to-end with a smaller
    # cap. Production agy.exe is a direct CreateProcess (32767 limit).
    monkeypatch.setattr(AntigravityCLIAdapter, "MAX_PROMPT_BYTES", 7000)
    resp = run(adapter.generate_response("x" * 8000, "req-1"))
    assert resp.is_success is True
    assert resp.text == "PONG"


def test_fit_prompt_cjk_bytes_under_limit(tmp_path):
    adapter = make_adapter(tmp_path)
    prompt = ("汉" * 20000) + "\n请回复：pong"
    fitted = adapter._fit_prompt_for_argv(prompt)
    assert len(fitted.encode("utf-8")) <= AntigravityCLIAdapter.MAX_PROMPT_BYTES
    assert "Truncation marker" in fitted
    assert fitted.endswith("请回复：pong")


def test_fit_prompt_keeps_head_and_tail(tmp_path):
    adapter = make_adapter(tmp_path)
    prompt = (
        "HEAD:SYSTEM INSTRUCTIONS START\n"
        + ("m" * 30000)
        + "\nTAIL:latest user request END"
    )
    fitted = adapter._fit_prompt_for_argv(prompt)
    assert fitted.startswith("HEAD:SYSTEM INSTRUCTIONS START")
    assert fitted.endswith("TAIL:latest user request END")
    assert "Truncation marker" in fitted


def test_fit_prompt_short_unchanged(tmp_path):
    adapter = make_adapter(tmp_path)
    prompt = "short prompt"
    assert adapter._fit_prompt_for_argv(prompt) == prompt


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
    assert any("stale please" in l and "--conversation" in l for l in lines)
    assert any("stale please" in l and "--conversation" not in l for l in lines)


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
    run(adapter.shutdown())


# -------------------------------------------------------------- preflights


def test_gateway_preflight_antigravity_available(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-such-dir"))
    cfg = SimpleNamespace(agy_cmd=str(MOCK_AGY))
    status = check_gateway_engine(cfg, {}, "antigravity-cli")
    assert status["available"] is True
    assert MOCK_AGY.name in status["reason"]


def test_gateway_preflight_antigravity_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-such-dir"))
    cfg = SimpleNamespace(agy_cmd=str(tmp_path / "no-such-agy"))
    status = check_gateway_engine(cfg, {}, "antigravity-cli")
    assert status["available"] is False


def test_backend_preflight_antigravity_available(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-such-dir"))
    preflight = BackendPreflight()
    global_cfg = SimpleNamespace(
        gemini_cmd="gemini",
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