from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil

from adapters.codex_process_guard import (
    CodexProcessGuard,
    ProcessIdentity,
    evaluate_termination_command,
)


def _identity(pid: int, name: str, command_line: str = "") -> ProcessIdentity:
    return ProcessIdentity(
        pid=pid,
        create_time=1.0,
        name=name,
        command_line=command_line or name,
        role="test",
    )


def test_process_guard_rejects_protected_pid_and_name_but_allows_other_pid():
    protected = {
        4101: _identity(4101, "codex.exe"),
        4102: _identity(4102, "node.exe", "node hashi-mcp-server.js"),
    }

    by_pid = evaluate_termination_command(
        "Stop-Process -Id 4101 -Force",
        protected,
    )
    by_image = evaluate_termination_command(
        "taskkill /F /IM node.exe",
        protected,
    )
    other = evaluate_termination_command(
        "Stop-Process -Id 99991 -Force",
        protected,
    )

    assert by_pid.allowed is False
    assert by_pid.matched_pids == (4101,)
    assert by_image.allowed is False
    assert by_image.matched_pids == (4102,)
    assert other.allowed is True


def test_process_guard_fails_closed_for_dynamic_kill_selector_only():
    protected = {4101: _identity(4101, "codex.exe")}

    dynamic = evaluate_termination_command(
        "Stop-Process -Id $candidate -Force",
        protected,
    )
    ordinary = evaluate_termination_command(
        "Get-Process node | Select-Object -First 3",
        protected,
    )

    assert dynamic.allowed is False
    assert dynamic.reason_code == "termination_target_unresolved"
    assert ordinary.allowed is True


def test_hook_denies_engine_standin_without_terminating_it(tmp_path: Path):
    guard = CodexProcessGuard.create(
        tmp_path / "guards",
        request_id="req-safe-standin",
    )
    module_root = Path(__file__).resolve().parents[1]
    result_path = tmp_path / "standin-result.json"
    wrapper = (
        "import json, os, pathlib, subprocess, sys, time; "
        "payload={'hook_event_name':'PreToolUse','tool_name':'command_execution',"
        "'tool_input':{'command':\"pwsh.exe -NoProfile -Command "
        "'Stop-Process -Id $PID -Force'\"}}; "
        "child=subprocess.run([sys.executable, '-m', 'adapters.codex_process_guard'],"
        "input=json.dumps(payload), text=True, capture_output=True, check=False); "
        f"pathlib.Path({str(result_path)!r}).write_text(json.dumps("
        "{'stdout':child.stdout,'stderr':child.stderr,'returncode':child.returncode}),"
        "encoding='utf-8'); time.sleep(60)"
    )
    environment = guard.subprocess_environment()
    environment["PYTHONPATH"] = os.pathsep.join(
        item
        for item in (str(module_root), environment.get("PYTHONPATH", ""))
        if item
    )
    standin = subprocess.Popen(
        [sys.executable, "-c", wrapper],
        cwd=str(module_root),
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not result_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert result_path.exists(), standin.stderr.read()
        envelope = json.loads(result_path.read_text(encoding="utf-8"))
        assert envelope["returncode"] == 0, envelope
        result = json.loads(envelope["stdout"])
        decision = result["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert "unresolved process-termination selector" in decision[
            "permissionDecisionReason"
        ]
        assert psutil.pid_exists(standin.pid)
        assert standin.poll() is None
    finally:
        standin.terminate()
        standin.wait(timeout=10)
        guard.close()


def test_guard_cli_flags_trust_only_the_session_hook(tmp_path: Path):
    guard = CodexProcessGuard.create(tmp_path / "guards", request_id="req-flags")
    try:
        flags = guard.cli_flags()
        rendered = "\n".join(flags)
        assert flags[:2] == ["--enable", "hooks"]
        assert "hooks.PreToolUse=" in rendered
        assert 'matcher=".*"' in rendered
        assert "hooks.state={" in rendered
        assert "trusted_hash" in rendered
        assert "dangerously-bypass-hook-trust" not in rendered
    finally:
        context_path = guard.context_path
        guard.close()
    assert not context_path.exists()
