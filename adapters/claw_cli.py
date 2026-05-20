from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from adapters.base import BaseBackend, BackendCapabilities, BackendResponse, TokenUsage
from adapters.stream_events import StreamCallback, StreamEvent, KIND_PROGRESS, KIND_TOOL_END


DEFAULT_CLAW_TIMEOUT_SEC = 30
DEFAULT_CLAW_TASK_TIMEOUT_SEC = 1800
VALID_PERMISSION_MODES = {"read-only", "workspace-write", "danger-full-access"}
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
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


def redact_secret_text(text: str | None, extra_values: list[str] | None = None) -> str:
    if not text:
        return ""
    redacted = str(text)
    for key in SECRET_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            redacted = redacted.replace(value, "<redacted>")
    for value in extra_values or []:
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
    process_env = build_claw_env(env)
    secret_values = [process_env.get(key, "") for key in SECRET_ENV_KEYS]
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=process_env,
            stdin=subprocess.DEVNULL,
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
    stdout = redact_secret_text(completed.stdout, secret_values)
    stderr = redact_secret_text(completed.stderr, secret_values)
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
    skip_permissions: bool = False,
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

    args = build_claw_task_args(
        prompt,
        model,
        permission_mode=permission_mode,
        resume=resume,
        allowed_tools=allowed_tools,
        skip_permissions=skip_permissions,
    )

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


def build_claw_task_args(
    prompt: str,
    model: str,
    *,
    permission_mode: str = "workspace-write",
    resume: str | None = None,
    allowed_tools: list[str] | None = None,
    skip_permissions: bool = False,
) -> list[str]:
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
    if skip_permissions:
        args.append("--dangerously-skip-permissions")
    if resume:
        args.extend(["--resume", resume])
    args.extend(["prompt", prompt])
    return args


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


class ClawCLIAdapter(BaseBackend):
    DEFAULT_IDLE_TIMEOUT_SEC = 300
    DEFAULT_HARD_TIMEOUT_SEC = 1800

    def _define_capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_sessions=True,
            supports_files=True,
            supports_tool_use=True,
            supports_thinking_stream=False,
            supports_headless_mode=True,
        )

    def __init__(self, agent_config, global_config, api_key: str = None):
        super().__init__(agent_config, global_config, api_key)
        self.logger = logging.getLogger(f"Backend.Claw.{self.config.name}")
        self.current_proc = None
        self._binary: Path | None = None
        self._fresh_next = False

    @property
    def _extra(self) -> dict[str, Any]:
        extra = getattr(self.config, "extra", None) or {}
        return dict(extra) if isinstance(extra, Mapping) else {}

    def _allowed_tools(self) -> list[str] | None:
        raw = self._extra.get("allowed_tools")
        if raw is None:
            return None
        if isinstance(raw, str):
            return [item.strip() for item in raw.split(",") if item.strip()]
        if isinstance(raw, list):
            return [str(item).strip() for item in raw if str(item).strip()]
        return None

    def _permission_mode(self) -> str:
        return str(self._extra.get("permission_mode") or "workspace-write")

    def _skip_permissions(self) -> bool:
        return bool(self._extra.get("skip_permissions") or self._extra.get("dangerously_skip_permissions"))

    def _openai_base_url(self) -> str:
        return str(self._extra.get("openai_base_url") or DEFAULT_OPENROUTER_BASE_URL)

    def _task_env(self) -> dict[str, str]:
        env_source = dict(os.environ)
        if self.api_key:
            env_source["OPENAI_API_KEY"] = str(self.api_key)
        if self._openai_base_url():
            env_source["OPENAI_BASE_URL"] = self._openai_base_url()
        return build_claw_env(env_source)

    def _has_saved_session(self) -> bool:
        sessions_dir = self.effective_workdir / ".claw" / "sessions"
        return sessions_dir.is_dir() and any(sessions_dir.glob("*.jsonl"))

    def _resume_target(self) -> str | None:
        if self._fresh_next:
            return None
        configured = self._extra.get("resume")
        if configured is False or configured == "none":
            return None
        if configured:
            return str(configured)
        return "latest" if self._has_saved_session() else None

    async def initialize(self) -> bool:
        self.logger.info("Initializing Claw CLI backend...")
        self.config.workspace_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._binary = find_claw_binary(
                global_config=self.global_config,
                agent_config=self.config,
            )
            version = await asyncio.to_thread(
                run_claw_version,
                self.effective_workdir,
                binary_path=self._binary,
                env=self._task_env(),
                timeout_s=30,
            )
            self.logger.info(
                "Claw CLI version check passed "
                f"(binary={self._binary}, version={version.get('version')}, git_sha={version.get('git_sha')})"
            )
            return True
        except ClawError as exc:
            self.logger.error(f"Claw CLI unavailable: {exc}")
            self._binary = None
            return False
        except Exception as exc:
            self.logger.error(f"Claw CLI initialization failed: {exc}")
            self._binary = None
            return False

    async def handle_new_session(self) -> bool:
        self._fresh_next = True
        self.logger.info("Claw handle_new_session: next request will start without --resume.")
        return True

    async def shutdown(self):
        if self.current_proc:
            await self.force_kill_process_tree(
                self.current_proc,
                logger=self.logger,
                reason="shutdown",
            )
        self.current_proc = None

    async def generate_response(
        self,
        prompt: str,
        request_id: str,
        is_retry: bool = False,
        silent: bool = False,
        on_stream_event: StreamCallback = None,
    ) -> BackendResponse:
        if self._binary is None:
            try:
                self._binary = find_claw_binary(global_config=self.global_config, agent_config=self.config)
            except ClawError as exc:
                return BackendResponse(text="", duration_ms=0, error=str(exc), is_success=False)

        if on_stream_event is not None:
            await on_stream_event(StreamEvent(kind=KIND_PROGRESS, summary="Claw task started"))

        started = time.perf_counter()
        resume = self._resume_target()
        self._fresh_next = False
        try:
            result = await self._run_task_async(
                prompt,
                resume=resume,
            )
        except asyncio.CancelledError:
            if self.current_proc:
                await self.force_kill_process_tree(
                    self.current_proc,
                    logger=self.logger,
                    reason=f"cancelled:{request_id}",
                )
            raise
        except ClawTimeoutError as exc:
            return BackendResponse(text="", duration_ms=self._duration_ms(started), error=str(exc), is_success=False)
        except ClawCommandError as exc:
            return BackendResponse(text="", duration_ms=self._duration_ms(started), error=str(exc), is_success=False)
        except (ClawError, ValueError) as exc:
            return BackendResponse(text="", duration_ms=self._duration_ms(started), error=str(exc), is_success=False)

        if on_stream_event is not None:
            for tool in result.tool_uses:
                if isinstance(tool, dict):
                    await on_stream_event(
                        StreamEvent(
                            kind=KIND_TOOL_END,
                            summary=f"Claw used {tool.get('name') or 'tool'}",
                            tool_name=str(tool.get("name") or ""),
                        )
                    )
        return BackendResponse(
            text=result.text,
            duration_ms=result.duration_ms,
            is_success=True,
            usage=TokenUsage(
                input_tokens=int((result.json_data.get("usage") or {}).get("input_tokens") or 0),
                output_tokens=int((result.json_data.get("usage") or {}).get("output_tokens") or 0),
                thinking_tokens=0,
            ),
            cost_usd=None,
            tool_call_count=len(result.tool_uses),
            tool_loop_count=result.iterations or 0,
        )

    @staticmethod
    def _duration_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 2)

    async def _run_task_async(self, prompt: str, *, resume: str | None) -> ClawTaskResult:
        if self._binary is None:
            raise ClawBinaryNotFound("Claw binary not initialized")
        args = build_claw_task_args(
            prompt,
            self.config.model,
            permission_mode=self._permission_mode(),
            resume=resume,
            allowed_tools=self._allowed_tools(),
            skip_permissions=self._skip_permissions(),
        )
        command = [str(self._binary), *args]
        started = time.perf_counter()
        extra_kwargs = {}
        if os.name != "nt":
            extra_kwargs["start_new_session"] = True
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.effective_workdir),
            env=self._task_env(),
            limit=1024 * 1024,
            **extra_kwargs,
        )
        self.current_proc = proc
        self._touch_activity()
        try:
            stdout_data, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=self.HARD_TIMEOUT_SEC)
        except asyncio.TimeoutError as exc:
            await self.force_kill_process_tree(
                proc,
                logger=self.logger,
                reason=f"hard-timeout:{self.HARD_TIMEOUT_SEC}s",
            )
            raise ClawTimeoutError(
                f"Claw command timed out after {self.HARD_TIMEOUT_SEC}s: {' '.join(command)}",
                timeout_s=self.HARD_TIMEOUT_SEC,
            ) from exc
        finally:
            self.current_proc = None

        duration_ms = self._duration_ms(started)
        env = self._task_env()
        secret_values = [env.get(key, "") for key in SECRET_ENV_KEYS]
        stdout = redact_secret_text(stdout_data.decode(errors="replace"), secret_values)
        stderr = redact_secret_text(stderr_data.decode(errors="replace"), secret_values)
        output = stdout.strip() or stderr.strip()
        parsed = _parse_json_output(output, command=command) if output else {}
        if proc.returncode != 0:
            message = parsed.get("error") if isinstance(parsed, dict) else None
            raise ClawCommandError(
                message or f"Claw command exited with code {proc.returncode}",
                returncode=proc.returncode or 1,
                stdout=stdout,
                stderr=stderr,
                parsed_error=parsed if parsed else None,
            )
        return ClawTaskResult(
            text=str(parsed.get("message") or ""),
            model=str(parsed.get("model") or self.config.model),
            permission_mode=self._permission_mode(),
            cwd=str(self.effective_workdir),
            returncode=proc.returncode or 0,
            duration_ms=duration_ms,
            stdout=stdout,
            stderr=stderr,
            json_data=parsed,
            tool_uses=list(parsed.get("tool_uses") or []),
            tool_results=list(parsed.get("tool_results") or []),
            iterations=parsed.get("iterations") if isinstance(parsed.get("iterations"), int) else None,
            estimated_cost=parsed.get("estimated_cost") if isinstance(parsed.get("estimated_cost"), str) else None,
        )
