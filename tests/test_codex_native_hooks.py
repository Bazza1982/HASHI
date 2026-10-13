import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from orchestrator.codex_native_hooks import hook_overrides, termination_reason


@pytest.mark.parametrize("command", [
    "Stop-Process -Id 1234 -Force", "Get-Process node | Stop-Process", "spps -Id 1234",
    "taskkill /PID 1234 /T /F", "kill 1234", "pkill codex", "killall node",
])
def test_native_termination_requires_managed_process_tool(command):
    assert "HASHI_NATIVE_PROCESS_PROTECTED" in termination_reason(command)


@pytest.mark.parametrize("command", ["Write-Output safe", "Get-Process node", "rg 'Stop-Process' docs", "git status --short"])
def test_inspection_and_normal_shell_work_remain_available(command):
    assert termination_reason(command) is None


def test_owned_hook_returns_explicit_denial_before_any_target_is_terminated(tmp_path):
    hook = Path(__file__).parents[1] / "orchestrator" / "codex_native_hooks.py"
    receipt = tmp_path / "loaded.json"
    event = {"hook_event_name":"PreToolUse", "tool_name":"Bash", "tool_input":{"command":f"Stop-Process -Id {os.getpid()} -Force"}}
    result = subprocess.run([sys.executable, str(hook), "--receipt", str(receipt)], input=json.dumps(event),
        text=True, capture_output=True, timeout=8)
    assert result.returncode == 0
    output = json.loads(result.stdout)['hookSpecificOutput']
    assert output['permissionDecision'] == 'deny'
    assert 'process_kill' in output['permissionDecisionReason']
    assert os.getpid() > 0
    assert not receipt.exists()


def test_hook_config_is_command_scoped_and_does_not_enable_user_profiles(tmp_path):
    flags = hook_overrides(receipt_path=tmp_path / 'loaded.json')
    assert '--ignore-user-config' in flags
    assert '--enable' in flags and 'hooks' in flags
    assert '-p' not in flags
    assert any(value.startswith('hooks.PreToolUse=') and 'matcher="Bash"' in value for value in flags)
