from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


DEFAULT_CLAW_TIMEOUT_SEC = 30
DEFAULT_CLAW_TASK_TIMEOUT_SEC = 1800
VALID_PERMISSION_MODES = {"read-only", "workspace-write", "danger-full-access"}
SECRET_ENV_KEYS = {
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "XAI_API_KEY",
    "DASHSCOPE_API_KEY",
}
OS_ENV_ALLOWLIST = ("HOME", "USER", "TMPDIR", "TEMP", "PATH")
CLAW_ENV_ALLOWLIST = ("OPENAI_BASE_URL", "OPENAI_API_KEY", *OS_ENV_ALLOWLIST)


class ClawError(RuntimeError):
    """Base class for Claw CLI diagnostic failures."""


class ClawBinaryNotFound(ClawError):
    """Raised when no executable Claw binary can be resolved."""


class ClawCommandError(ClawError):
    """Raised when Claw exits non-zero."""

    def __init__(
        self,
        message: str,
        *,
        returncode: int,
        stdout: str = "",
        stderr: str = "",
        parsed_error: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.parsed_error = parsed_error


class ClawJsonError(ClawError):
    """Raised when Claw output is expected to be JSON but is not parseable."""

    def __init__(self, message: str, *, output: str):
        super().__init__(message)
        self.output = output


class ClawTimeoutError(ClawError):
    """Raised when Claw does not finish before the configured timeout."""

    def __init__(self, message: str, *, timeout_s: float):
        super().__init__(message)
        self.timeout_s = timeout_s


@dataclass(frozen=True)
class ClawCommandResult:
    command: list[str]
    cwd: str
    returncode: int
    stdout: str
    stderr: str
    duration_ms: float
    json_data: dict[str, Any]


@dataclass(frozen=True)
class ClawTaskResult:
    text: str
    model: str
    permission_mode: str
    cwd: str
    returncode: int
    duration_ms: float
    stdout: str
    stderr: str
    json_data: dict[str, Any]
    tool_uses: list[Any]
    tool_results: list[Any]
    iterations: int | None = None
    estimated_cost: str | None = None


def redact_secret_text(text: str | None) -> str:
    if not text:
        return ""
    redacted = str(text)
    for key in SECRET_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            redacted = redacted.replace(value, "<redacted>")
    return redacted


def build_claw_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a minimal environment for Claw subprocesses."""
    source = source or os.environ
    env: dict[str, str] = {}
    for key in CLAW_ENV_ALLOWLIST:
        value = source.get(key)
        if value:
            env[key] = str(value)
    if "PATH" not in env and os.environ.get("PATH"):
        env["PATH"] = os.environ["PATH"]
    if "HOME" not in env and os.environ.get("HOME"):
        env["HOME"] = os.environ["HOME"]
    return env


def find_claw_binary(
    configured_path: str | os.PathLike[str] | None = None,
    *,
    global_config: Any | None = None,
    agent_config: Any | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve an executable Claw binary without requiring Cargo."""
    candidates: list[str | os.PathLike[str]] = []
    if configured_path:
        candidates.append(configured_path)

    extra = getattr(agent_config, "extra", None) or {}
    if isinstance(extra, Mapping):
        for key in ("claw_binary_path", "claw_cmd"):
            value = extra.get(key)
            if value:
                candidates.append(value)

    for key in ("claw_binary_path", "claw_cmd"):
        value = getattr(global_config, key, None)
        if value:
            candidates.append(value)

    env = env or os.environ
    for key in ("CLAW_BINARY", "CLAW_BIN"):
        value = env.get(key)
        if value:
            candidates.append(value)

    candidates.append("claw")

    failures: list[str] = []
    for candidate in candidates:
        raw = str(candidate).strip()
        if not raw:
            continue
        resolved = shutil.which(raw) if os.path.basename(raw) == raw else raw
        if not resolved:
            failures.append(f"{raw}: not found on PATH")
            continue
        path = Path(resolved).expanduser()
        if not path.exists():
            failures.append(f"{path}: does not exist")
            continue
        if not path.is_file():
            failures.append(f"{path}: not a file")
            continue
        if not os.access(path, os.X_OK):
            failures.append(f"{path}: not executable")
            continue
        return path.resolve()

    detail = "; ".join(failures) if failures else "no candidate configured"
    raise ClawBinaryNotFound(f"Claw binary not found ({detail})")


def _parse_json_output(text: str, *, command: list[str]) -> dict[str, Any]:
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        preview = " ".join(text.split())[:400] or "<empty>"
        raise ClawJsonError(
            f"Claw command produced non-JSON output for {' '.join(command)}: {preview}",
            output=text,
        ) from exc
    if not isinstance(loaded, dict):
        raise ClawJsonError(
            f"Claw command JSON output was not an object for {' '.join(command)}",
            output=text,
        )
    return loaded


def run_claw_json_command(
    args: list[str],
    *,
    cwd: str | os.PathLike[str],
    binary_path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    timeout_s: float = DEFAULT_CLAW_TIMEOUT_SEC,
) -> ClawCommandResult:
    binary = find_claw_binary(binary_path, env=env)
    command = [str(binary), *args]
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=build_claw_env(env),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClawTimeoutError(
            f"Claw command timed out after {timeout_s}s: {' '.join(command)}",
            timeout_s=timeout_s,
        ) from exc

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    stdout = redact_secret_text(completed.stdout)
    stderr = redact_secret_text(completed.stderr)
    output = stdout.strip() or stderr.strip()
    parsed = _parse_json_output(output, command=command) if output else {}

    if completed.returncode != 0:
        message = parsed.get("error") if isinstance(parsed, dict) else None
        raise ClawCommandError(
            message or f"Claw command exited with code {completed.returncode}",
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            parsed_error=parsed if parsed else None,
        )

    return ClawCommandResult(
        command=command,
        cwd=str(cwd),
        returncode=completed.returncode,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
        json_data=parsed,
    )


def run_claw_task(
    workspace_dir: str | os.PathLike[str],
    prompt: str,
    model: str,
    *,
    permission_mode: str = "workspace-write",
    resume: str | None = None,
    allowed_tools: list[str] | None = None,
    binary_path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    timeout_s: float = DEFAULT_CLAW_TASK_TIMEOUT_SEC,
) -> ClawTaskResult:
    """Run one Claw prompt and return the machine-readable result."""
    if permission_mode not in VALID_PERMISSION_MODES:
        raise ValueError(
            f"invalid Claw permission_mode {permission_mode!r}; "
            f"expected one of {sorted(VALID_PERMISSION_MODES)}"
        )
    if not str(prompt or "").strip():
        raise ValueError("prompt must not be empty")
    if not str(model or "").strip():
        raise ValueError("model must not be empty")

    args = [
        "--model",
        model,
        "--permission-mode",
        permission_mode,
        "--output-format",
        "json",
    ]
    if allowed_tools:
        args.extend(["--allowedTools", ",".join(allowed_tools)])
    if resume:
        args.extend(["--resume", resume])
    args.extend(["prompt", prompt])

    result = run_claw_json_command(
        args,
        cwd=workspace_dir,
        binary_path=binary_path,
        env=env,
        timeout_s=timeout_s,
    )
    data = result.json_data
    return ClawTaskResult(
        text=str(data.get("message") or ""),
        model=str(data.get("model") or model),
        permission_mode=permission_mode,
        cwd=result.cwd,
        returncode=result.returncode,
        duration_ms=result.duration_ms,
        stdout=result.stdout,
        stderr=result.stderr,
        json_data=data,
        tool_uses=list(data.get("tool_uses") or []),
        tool_results=list(data.get("tool_results") or []),
        iterations=data.get("iterations") if isinstance(data.get("iterations"), int) else None,
        estimated_cost=data.get("estimated_cost") if isinstance(data.get("estimated_cost"), str) else None,
    )


def run_claw_version(
    cwd: str | os.PathLike[str],
    env: Mapping[str, str] | None = None,
    *,
    binary_path: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_CLAW_TIMEOUT_SEC,
) -> dict[str, Any]:
    return run_claw_json_command(
        ["version", "--output-format", "json"],
        cwd=cwd,
        binary_path=binary_path,
        env=env,
        timeout_s=timeout_s,
    ).json_data


def run_claw_doctor(
    cwd: str | os.PathLike[str],
    env: Mapping[str, str] | None = None,
    *,
    binary_path: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_CLAW_TIMEOUT_SEC,
) -> dict[str, Any]:
    return run_claw_json_command(
        ["doctor", "--output-format", "json"],
        cwd=cwd,
        binary_path=binary_path,
        env=env,
        timeout_s=timeout_s,
    ).json_data


def run_claw_status(
    cwd: str | os.PathLike[str],
    env: Mapping[str, str] | None = None,
    *,
    binary_path: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_CLAW_TIMEOUT_SEC,
) -> dict[str, Any]:
    return run_claw_json_command(
        ["status", "--output-format", "json"],
        cwd=cwd,
        binary_path=binary_path,
        env=env,
        timeout_s=timeout_s,
    ).json_data


def run_claw_state(
    cwd: str | os.PathLike[str],
    env: Mapping[str, str] | None = None,
    *,
    binary_path: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_CLAW_TIMEOUT_SEC,
) -> dict[str, Any]:
    return run_claw_json_command(
        ["state", "--output-format", "json"],
        cwd=cwd,
        binary_path=binary_path,
        env=env,
        timeout_s=timeout_s,
    ).json_data
