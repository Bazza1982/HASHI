"""Codex's native shell guard, scoped to its qualified Function invocation.

This is a guardrail for ordinary shell termination commands. Process effects
belong to the managed process tool; arbitrary native programs and vendor hook
failures are not an operating-system security boundary.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

_PROCESS_COMMANDS = frozenset({
    "stop-process", "spps", "kill", "pkill", "killall", "taskkill", "taskkill.exe", "tskill", "tskill.exe",
})
_POWERSHELL_COMMANDS = r"""
$text = [Console]::In.ReadToEnd()
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tokens, [ref]$errors)
$names = @($ast.FindAll({param($node) $node -is [System.Management.Automation.Language.CommandAst]}, $true) |
    ForEach-Object { $_.GetCommandName() })
$methods = @($ast.FindAll({param($node) $node -is [System.Management.Automation.Language.InvokeMemberExpressionAst]}, $true) |
    ForEach-Object { $_.Member.Value })
@{ names = $names; methods = $methods; parse_errors = @($errors).Count } | ConvertTo-Json -Compress
"""


def termination_reason(command: str) -> str | None:
    if not command.strip() or len(command.encode("utf-8")) > 65536:
        return "The native shell command cannot be checked safely."
    if os.name == "nt":
        parsed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _POWERSHELL_COMMANDS],
            input=command, text=True, encoding="utf-8", capture_output=True, timeout=3,
        )
        if parsed.returncode:
            return "The native shell command could not be checked. Use the managed HASHI shell tool."
        payload = json.loads(parsed.stdout)
        names = [str(name or "").casefold() for name in payload.get("names", ())]
        methods = [str(name or "").casefold() for name in payload.get("methods", ())]
        if payload.get("parse_errors"):
            return "The native shell command has an invalid parse; use the managed HASHI shell tool."
    else:
        names = [Path(token).name.casefold() for token in shlex.split(command, posix=True)]
        methods = []
    if any(Path(name).name in _PROCESS_COMMANDS for name in names) or any(
        method in {"kill", "terminate"} for method in methods
    ):
        return (
            "HASHI_NATIVE_PROCESS_PROTECTED: native process termination is refused. "
            "Use HASHI process_list to identify the exact target, then the managed process_kill tool. "
            "HASHI and the Codex engine must remain running."
        )
    return None


def hook_overrides(*, receipt_path: Path) -> list[str]:
    """Supply only owned session hooks; never install a user-wide hook/profile."""
    script = Path(__file__).resolve()
    command = subprocess.list2cmdline([sys.executable, str(script), "--receipt", str(receipt_path)]) if os.name == "nt" else shlex.join(
        [sys.executable, str(script), "--receipt", str(receipt_path)]
    )
    values = ["--ignore-user-config", "--enable", "hooks", "--dangerously-bypass-hook-trust"]
    for event in ("SessionStart", "PreToolUse"):
        matcher = 'matcher="Bash",' if event == "PreToolUse" else ""
        values += ["-c", f"hooks.{event}=[{{{matcher}hooks=[{{type=\"command\",command={json.dumps(command)},timeout=5}}]}}]"]
    return values


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    event_name = "PreToolUse"
    try:
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("hook input is too large")
        event = json.loads(raw)
        event_name = event.get("hook_event_name")
        if event_name == "SessionStart":
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            args.receipt.write_text(json.dumps({"type":"hashi.native-shell-guard", "version":1, "loaded":True}), encoding="utf-8")
            print(json.dumps({"hookSpecificOutput":{"hookEventName":"SessionStart", "additionalContext":
                "HASHI native process protection is active. Use HASHI's managed process tools for termination."}}))
            return
        tool_input = event.get("tool_input") or {}
        reason = termination_reason(str(tool_input.get("command") or tool_input.get("cmd") or ""))
    except Exception as error:
        reason = f"HASHI_NATIVE_PROCESS_PROTECTED: native command checks failed ({type(error).__name__}); use managed HASHI tools."
    output = {"hookEventName": event_name}
    if reason:
        output.update({"permissionDecision":"deny", "permissionDecisionReason":reason})
    print(json.dumps({"hookSpecificOutput":output}))


if __name__ == "__main__":
    main()
