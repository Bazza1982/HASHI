"""Request-scoped Codex shell guard for HASHI-owned process lifecycles.

Codex executes its native shell tool inside the CLI host, so those calls do not
pass through HASHI's ordinary tool registry.  This module is a real Codex
``PreToolUse`` hook: it can deny a matching shell call before the CLI executes
it.  It is intentionally not described as an OS sandbox; commands hidden
inside an arbitrary interpreter remain outside shell-text policy coverage.
"""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Mapping, Sequence
from uuid import uuid4

import psutil

from tools.private_files import protect_private_file


CONTEXT_ENV = "HASHI_CODEX_PROCESS_GUARD_CONTEXT"
CONTEXT_SCHEMA = "hashi.codex-process-guard.v1"
HOOK_TIMEOUT_SECONDS = 5
_HOST_CHAIN_LIMIT = 3  # Worker plus its bounded Shared/Core parent chain.
_CREATE_TIME_TOLERANCE_SECONDS = 0.01
_TERMINATION_TOOLS = frozenset(
    {
        "stop-process",
        "spps",
        "kill",
        "taskkill",
        "taskkill.exe",
        "tskill",
        "tskill.exe",
        "pkill",
        "killall",
    }
)
_SHELL_TOOL_NAMES = frozenset(
    {"bash", "shell", "shell_command", "unified_exec", "command_execution"}
)
_CODE_TOOL_NAMES = frozenset({"exec"})
_INTEGER = re.compile(r"^[0-9]+$")
_INTEGER_LIST = re.compile(r"^[0-9]+(?:\s*,\s*[0-9]+)*$")
_TERMINATION_MARKER = re.compile(
    r"(?<![\w-])(?:"
    + "|".join(re.escape(item) for item in sorted(_TERMINATION_TOOLS, key=len, reverse=True))
    + r")(?![\w-])",
    re.IGNORECASE,
)
_EXEC_COMMAND_LITERAL = re.compile(
    r"(?<![\w$])cmd\s*:\s*(?P<literal>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    create_time: float
    name: str
    command_line: str
    role: str


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reason_code: str = "allowed"
    matched_pids: tuple[int, ...] = ()
    detail: str = ""


def _identity(process: psutil.Process, *, role: str) -> ProcessIdentity:
    return ProcessIdentity(
        pid=int(process.pid),
        create_time=float(process.create_time()),
        name=str(process.name() or ""),
        command_line=" ".join(str(item) for item in (process.cmdline() or ())),
        role=role,
    )


def _same_process(process: psutil.Process, expected: Mapping[str, object]) -> bool:
    try:
        expected_pid = int(expected.get("pid") or 0)
        expected_created = float(expected.get("create_time") or 0.0)
        actual_created = float(process.create_time())
    except (psutil.Error, TypeError, ValueError):
        return False
    return (
        expected_pid == process.pid
        and expected_created > 0
        and abs(actual_created - expected_created) <= _CREATE_TIME_TOLERANCE_SECONDS
    )


def _capture_host_chain() -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    process = psutil.Process(os.getpid())
    for index in range(_HOST_CHAIN_LIMIT):
        identity = _identity(
            process,
            role=("worker" if index == 0 else f"host_parent_{index}"),
        )
        result.append(
            {
                "pid": identity.pid,
                "create_time": identity.create_time,
                "role": identity.role,
            }
        )
        try:
            parent = process.parent()
        except psutil.Error:
            parent = None
        if parent is None:
            break
        process = parent
    return result


def _hook_command() -> str:
    argv = [sys.executable, str(Path(__file__).resolve())]
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def _hook_identity(command: str) -> dict[str, object]:
    return {
        "event_name": "pre_tool_use",
        # Match every PreToolUse event, then make the allow/deny decision from
        # the typed payload below.  Codex reports native shell executions as
        # ``command_execution`` on current Windows builds, not as ``Bash``.
        "matcher": ".*",
        "hooks": [
            {
                "async": False,
                "command": command,
                "timeout": HOOK_TIMEOUT_SECONDS,
                "type": "command",
            }
        ],
    }


def _hook_hash(command: str) -> str:
    encoded = json.dumps(
        _hook_identity(command),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _session_hook_key() -> str:
    if os.name == "nt":
        source = r"C:\<session-flags>\config.toml"
    else:
        source = "/<session-flags>/config.toml"
    return f"{source}:pre_tool_use:0:0"


def _toml_string(value: str) -> str:
    # JSON strings and TOML basic strings share the escaping needed here.
    return json.dumps(str(value), ensure_ascii=False)


class CodexProcessGuard:
    """One Codex invocation's private process-ownership receipt and hook flags."""

    def __init__(self, context_path: Path):
        self.context_path = Path(context_path)
        self._closed = False

    @classmethod
    def create(cls, state_dir: Path, *, request_id: str) -> "CodexProcessGuard":
        root = Path(state_dir)
        root.mkdir(parents=True, exist_ok=True)
        context_path = root / f"guard-{uuid4().hex}.json"
        payload = {
            "schema": CONTEXT_SCHEMA,
            "request_digest": hashlib.sha256(
                str(request_id).encode("utf-8", errors="replace")
            ).hexdigest(),
            "host_chain": _capture_host_chain(),
        }
        descriptor = os.open(
            context_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)
        try:
            protect_private_file(context_path)
        except Exception:
            context_path.unlink(missing_ok=True)
            raise
        return cls(context_path)

    def subprocess_environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment[CONTEXT_ENV] = str(self.context_path)
        return environment

    def cli_flags(self) -> list[str]:
        command = _hook_command()
        hook_value = (
            "[{matcher=\".*\",hooks=[{type=\"command\",command="
            f"{_toml_string(command)},timeout={HOOK_TIMEOUT_SECONDS}"
            "}]}]"
        )
        hook_key = _session_hook_key()
        trust_value = _hook_hash(command)
        return [
            "--enable",
            "hooks",
            "-c",
            f"hooks.PreToolUse={hook_value}",
            "-c",
            (
                "hooks.state={"
                f"{_toml_string(hook_key)}={{trusted_hash={_toml_string(trust_value)}}}"
                "}"
            ),
        ]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.context_path.unlink(missing_ok=True)


def _load_context(path: Path) -> dict[str, object]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != CONTEXT_SCHEMA:
        raise ValueError("invalid process-guard context schema")
    chain = value.get("host_chain")
    if not isinstance(chain, list) or not chain or len(chain) > _HOST_CHAIN_LIMIT:
        raise ValueError("invalid process-guard host chain")
    return value


def protected_process_inventory(
    context_path: Path,
    *,
    hook_pid: int | None = None,
) -> dict[int, ProcessIdentity]:
    """Resolve only the host lineage and this hook's current engine branch."""

    context = _load_context(context_path)
    raw_chain = context["host_chain"]
    assert isinstance(raw_chain, list)
    identities: dict[int, ProcessIdentity] = {}
    for item in raw_chain:
        if not isinstance(item, dict):
            raise ValueError("invalid process-guard host identity")
        pid = int(item.get("pid") or 0)
        if pid <= 0:
            raise ValueError("invalid process-guard host pid")
        try:
            process = psutil.Process(pid)
        except psutil.Error as exc:
            raise RuntimeError(f"protected host process {pid} is unavailable") from exc
        if not _same_process(process, item):
            raise RuntimeError(f"protected host process identity changed for PID {pid}")
        identities[pid] = _identity(
            process,
            role=str(item.get("role") or "host"),
        )

    owner_pid = int(raw_chain[0]["pid"])
    process = psutil.Process(int(hook_pid or os.getpid()))
    ancestry = [process]
    while process.pid != owner_pid:
        try:
            parent = process.parent()
        except psutil.Error as exc:
            raise RuntimeError("cannot resolve process-guard hook ancestry") from exc
        if parent is None:
            raise RuntimeError("process-guard hook is outside the owning Worker")
        ancestry.append(parent)
        process = parent
        if len(ancestry) > 64:
            raise RuntimeError("process-guard hook ancestry is unbounded")

    if len(ancestry) < 2:
        raise RuntimeError("process-guard hook has no Codex engine branch")
    engine_root = ancestry[-2]
    try:
        engine_branch = [engine_root, *engine_root.children(recursive=True)]
    except psutil.Error as exc:
        raise RuntimeError("cannot enumerate the Codex engine branch") from exc
    for branch_process in engine_branch:
        try:
            identity = _identity(branch_process, role="codex_engine_branch")
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.Error as exc:
            raise RuntimeError("cannot identify a Codex engine child") from exc
        identities[identity.pid] = identity
    return identities


def _tokens(command: str) -> list[str]:
    lexer = shlex.shlex(str(command), posix=False, punctuation_chars=";&|")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _clean_token(value: str) -> str:
    value = str(value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return value.strip().rstrip(",")


def _termination_tokens(command: str) -> list[str]:
    tokens = _tokens(command)
    expanded: list[str] = []
    for token in tokens:
        cleaned = _clean_token(token)
        if cleaned != token and any(
            tool in cleaned.casefold() for tool in _TERMINATION_TOOLS
        ):
            expanded.extend(_tokens(cleaned))
        else:
            expanded.append(token)
    return expanded


def _pid_values(token: str) -> tuple[int, ...]:
    value = _clean_token(token)
    if not _INTEGER_LIST.fullmatch(value):
        return ()
    return tuple(int(item.strip()) for item in value.split(","))


def _dynamic(value: str) -> bool:
    return any(marker in value for marker in ("$", "%", "`", "(", ")", "{", "}"))


def _name_matches(selector: str, identity: ProcessIdentity) -> bool:
    selector = _clean_token(selector).casefold()
    if not selector:
        return False
    names = {
        identity.name.casefold(),
        Path(identity.name).name.casefold(),
    }
    names |= {name[:-4] for name in tuple(names) if name.endswith(".exe")}
    if any(char in selector for char in "*?["):
        return any(fnmatch.fnmatchcase(name, selector) for name in names)
    return selector in names or f"{selector}.exe" in names


def evaluate_termination_command(
    command: str,
    protected: Mapping[int, ProcessIdentity],
) -> GuardDecision:
    try:
        tokens = _termination_tokens(command)
    except ValueError:
        if any(tool in str(command).casefold() for tool in _TERMINATION_TOOLS):
            return GuardDecision(False, "termination_target_unresolved")
        return GuardDecision(True)

    tool_indexes = [
        index
        for index, token in enumerate(tokens)
        if _clean_token(token).casefold() in _TERMINATION_TOOLS
    ]
    if not tool_indexes:
        return GuardDecision(True)

    target_pids: set[int] = set()
    target_names: set[str] = set()
    regex_names: set[str] = set()
    unresolved = False
    for index in tool_indexes:
        tool = _clean_token(tokens[index]).casefold()
        args: list[str] = []
        for token in tokens[index + 1 :]:
            if token in {";", "&&", "||", "|"}:
                break
            args.append(_clean_token(token))
        if tool in {"stop-process", "spps"}:
            mode = "positional"
            for arg in args:
                lowered = arg.casefold()
                if lowered in {"-id", "-pid"}:
                    mode = "pid"
                    continue
                if lowered in {"-name", "-processname"}:
                    mode = "name"
                    continue
                if lowered.startswith("-"):
                    continue
                values = _pid_values(arg)
                if mode in {"pid", "positional"} and values:
                    target_pids.update(values)
                elif mode == "name" and not _dynamic(arg):
                    target_names.update(
                        item.strip() for item in arg.split(",") if item.strip()
                    )
                else:
                    unresolved = True
        elif tool in {"taskkill", "taskkill.exe"}:
            mode = ""
            for arg in args:
                lowered = arg.casefold()
                if lowered == "/pid":
                    mode = "pid"
                    continue
                if lowered == "/im":
                    mode = "name"
                    continue
                if lowered.startswith("/"):
                    continue
                values = _pid_values(arg)
                if mode == "pid" and values:
                    target_pids.update(values)
                elif mode == "name" and not _dynamic(arg):
                    target_names.add(arg)
                else:
                    unresolved = True
        elif tool in {"pkill", "killall"}:
            candidates = [arg for arg in args if arg and not arg.startswith("-")]
            for candidate in candidates:
                if _dynamic(candidate):
                    unresolved = True
                elif tool == "pkill":
                    regex_names.add(candidate)
                else:
                    target_names.add(candidate)
        else:  # POSIX kill, PowerShell kill alias, or tskill.
            candidates = [arg for arg in args if arg and not arg.startswith("-")]
            for candidate in candidates:
                values = _pid_values(candidate)
                if values:
                    target_pids.update(abs(value) for value in values)
                else:
                    unresolved = True

    # PowerShell pipeline form: Get-Process -Id/-Name ... | Stop-Process.
    if not target_pids and not target_names:
        for index, token in enumerate(tokens):
            if _clean_token(token).casefold() not in {"get-process", "gps", "ps"}:
                continue
            mode = "name"
            for arg in tokens[index + 1 :]:
                if arg == "|":
                    break
                cleaned = _clean_token(arg)
                lowered = cleaned.casefold()
                if lowered in {"-id", "-pid"}:
                    mode = "pid"
                    continue
                if lowered in {"-name", "-processname"}:
                    mode = "name"
                    continue
                if lowered.startswith("-"):
                    continue
                values = _pid_values(cleaned)
                if mode == "pid" and values:
                    target_pids.update(values)
                elif mode == "name" and not _dynamic(cleaned):
                    target_names.add(cleaned)
                else:
                    unresolved = True

    matched: set[int] = set(pid for pid in target_pids if pid in protected)
    for pid, identity in protected.items():
        if any(_name_matches(name, identity) for name in target_names):
            matched.add(pid)
        for pattern in regex_names:
            try:
                if re.search(pattern, identity.name, re.IGNORECASE):
                    matched.add(pid)
            except re.error:
                unresolved = True
    if matched:
        pids = tuple(sorted(matched))
        return GuardDecision(
            False,
            "protected_process_target",
            pids,
            "refusing to terminate HASHI Worker/Core/Shared or this Codex engine branch",
        )
    if unresolved or not (target_pids or target_names or regex_names):
        return GuardDecision(
            False,
            "termination_target_unresolved",
            (),
            "termination target must be an exact non-protected PID or process name",
        )
    return GuardDecision(True)


def _contains_termination_tool(command: str) -> bool:
    try:
        return any(
            _clean_token(token).casefold() in _TERMINATION_TOOLS
            for token in _termination_tokens(command)
        )
    except ValueError:
        return any(tool in str(command).casefold() for tool in _TERMINATION_TOOLS)


def _decode_code_literal(value: str) -> str:
    if value.startswith('"'):
        decoded = json.loads(value)
        if not isinstance(decoded, str):
            raise ValueError("exec command literal must be a string")
        return decoded
    body = value[1:-1]
    replacements = {
        "\\\\": "\\",
        "\\'": "'",
        '\\"': '"',
        "\\n": "\n",
        "\\r": "\r",
        "\\t": "\t",
    }

    def replace_escape(match: re.Match[str]) -> str:
        escaped = match.group(0)
        if escaped not in replacements:
            raise ValueError("unsupported escape in exec command literal")
        return replacements[escaped]

    return re.sub(r"\\.", replace_escape, body)


def _termination_commands_from_payload(
    payload: Mapping[str, object],
) -> tuple[tuple[str, ...], bool]:
    """Return literal termination commands and whether any target is unresolved."""

    tool_name = str(payload.get("tool_name") or "").casefold()
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, Mapping):
        return (), False
    if tool_name in _SHELL_TOOL_NAMES:
        command_value = tool_input.get("command")
        if isinstance(command_value, Sequence) and not isinstance(command_value, str):
            command = " ".join(str(item) for item in command_value)
        else:
            command = str(command_value or "")
        return ((command,), False) if _contains_termination_tool(command) else ((), False)
    if tool_name not in _CODE_TOOL_NAMES:
        return (), False

    code = tool_input.get("code")
    if not isinstance(code, str) or not _TERMINATION_MARKER.search(code):
        return (), False
    commands: list[str] = []
    unresolved = False
    masked = list(code)
    for match in _EXEC_COMMAND_LITERAL.finditer(code):
        try:
            command = _decode_code_literal(match.group("literal"))
        except (json.JSONDecodeError, ValueError):
            unresolved = True
            continue
        for index in range(match.start(), match.end()):
            masked[index] = " "
        if _TERMINATION_MARKER.search(command):
            commands.append(command)
    if _TERMINATION_MARKER.search("".join(masked)):
        unresolved = True
    return tuple(commands), unresolved


def _denial(reason: str) -> dict[str, object]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def evaluate_hook_payload(
    payload: Mapping[str, object],
    *,
    context_path: Path,
    hook_pid: int | None = None,
) -> dict[str, object] | None:
    commands, unresolved = _termination_commands_from_payload(payload)
    if not commands and not unresolved:
        return None
    if unresolved:
        return _denial(
            "Refused an unresolved process-termination command in Codex exec code. "
            "Use one literal exec_command cmd with an exact external PID or process name."
        )
    try:
        protected = protected_process_inventory(context_path, hook_pid=hook_pid)
    except Exception as exc:
        return _denial(
            "HASHI process guard is unavailable; refusing termination command "
            f"({type(exc).__name__}: {exc})."
        )
    for command in commands:
        decision = evaluate_termination_command(command, protected)
        if decision.allowed:
            continue
        if decision.matched_pids:
            targets = ", ".join(str(pid) for pid in decision.matched_pids)
            return _denial(
                "Refused to terminate a protected HASHI/Codex process "
                f"(PID {targets}). Use an exact PID belonging to the intended external process."
            )
        return _denial(
            "Refused an unresolved process-termination selector. Use an exact PID or "
            "process name that does not belong to HASHI Worker/Core/Shared, Codex, MCP, "
            "or this invocation's child processes."
        )
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be an object")
        context_value = os.environ.get(CONTEXT_ENV, "").strip()
        if not context_value:
            result = _denial(
                "HASHI process guard context is missing; refusing this tool call."
            )
        else:
            result = evaluate_hook_payload(
                payload,
                context_path=Path(context_value),
            )
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as exc:
        # Hook failures are non-blocking in Codex, so a valid explicit denial is
        # the only safe response for malformed/unavailable guard state.
        print(
            json.dumps(
                _denial(
                    "HASHI process guard failed closed "
                    f"({type(exc).__name__}: {exc})."
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
