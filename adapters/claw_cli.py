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
