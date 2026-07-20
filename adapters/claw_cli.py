from __future__ import annotations

import json
import hashlib
import logging
import os
import platform as py_platform
import shutil
import subprocess
import time
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from adapters.base import BaseBackend, BackendCapabilities, BackendResponse, TokenUsage
from adapters.stream_events import (
    StreamCallback,
    StreamEvent,
    KIND_ERROR,
    KIND_PROGRESS,
    KIND_TEXT_DELTA,
    KIND_THINKING,
    KIND_TOOL_END,
    KIND_TOOL_START,
)


DEFAULT_CLAW_TIMEOUT_SEC = 30
DEFAULT_CLAW_TASK_TIMEOUT_SEC = 1800
VALID_PERMISSION_MODES = {"read-only", "workspace-write", "danger-full-access"}
PERMISSION_MODE_RANK = {"read-only": 0, "workspace-write": 1, "danger-full-access": 2}
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OLLAMA_DUMMY_API_KEY = "__ollama_dummy__"
PACKAGED_CLAW_RUNTIME = "hashi-claw"
PACKAGED_CLAW_MANIFEST_VERSION = 1
CLAW_RUNTIME_POLICIES = {"prefer-packaged", "require-packaged", "system-only"}
SECRET_ENV_KEYS = {
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "XAI_API_KEY",
    "DASHSCOPE_API_KEY",
}
OS_ENV_ALLOWLIST = ("HOME", "USER", "TMPDIR", "TEMP", "PATH")


@dataclass
class ClawThinkingStreamUsage:
    thinking_chars: int = 0
    thinking_tokens: int = 0
    thinking_event_count: int = 0
    thinking_redacted_count: int = 0
    thinking_sources: set[str] = field(default_factory=set)
    saw_actual_thinking_event: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "thinking_chars": self.thinking_chars,
            "thinking_tokens": self.thinking_tokens,
            "thinking_event_count": self.thinking_event_count,
            "thinking_redacted_count": self.thinking_redacted_count,
            "thinking_sources": sorted(self.thinking_sources),
        }
CLAW_ENV_ALLOWLIST = (
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "XAI_API_KEY",
    "XAI_BASE_URL",
    *OS_ENV_ALLOWLIST,
)


class ClawError(RuntimeError):
    """Base class for Claw CLI diagnostic failures."""


class ClawBinaryNotFound(ClawError):
    """Raised when no executable Claw binary can be resolved."""


class ClawPackagedRuntimeError(ClawBinaryNotFound):
    """Raised when a packaged Claw runtime exists but is unsafe or unusable."""


class ClawProviderConfigError(ClawError):
    """Raised when a named Claw provider is missing or disabled."""


class ClawProviderSecretMissing(ClawProviderConfigError):
    """Raised when a provider references a missing secret."""


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


@dataclass(frozen=True)
class ClawPlatform:
    key: str
    rust_target_triple: str
    system: str
    machine: str
    is_wsl: bool = False
    candidate_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class PackagedClawBinarySpec:
    platform_key: str
    relative_path: Path
    sha256: str
    rust_target_triple: str
    binary_name: str


@dataclass(frozen=True)
class PackagedClawManifest:
    manifest_path: Path
    version: str
    binaries: dict[str, PackagedClawBinarySpec]


@dataclass(frozen=True)
class ClawBinaryResolution:
    path: Path
    source: str
    warnings: tuple[str, ...] = ()
    platform: ClawPlatform | None = None
    manifest_path: Path | None = None
    packaged_version: str | None = None


def redact_secret_text(text: str | None, extra_values: list[str] | None = None) -> str:
    if not text:
        return ""
    redacted = str(text)
    for key in SECRET_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            redacted = redacted.replace(value, "<redacted>")
    for value in extra_values or []:
        if value and value != OLLAMA_DUMMY_API_KEY:
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


def detect_hashi_claw_platform(
    *,
    system: str | None = None,
    machine: str | None = None,
    release: str | None = None,
) -> ClawPlatform:
    raw_system = (system or py_platform.system() or "").strip().lower()
    raw_machine = (machine or py_platform.machine() or "").strip().lower()
    raw_release = (release or py_platform.release() or "").strip().lower()
    normalized_machine = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "x86-64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(raw_machine, raw_machine)
    normalized_system = {
        "linux": "linux",
        "windows": "windows",
        "darwin": "macos",
    }.get(raw_system, raw_system)
    is_wsl = normalized_system == "linux" and "microsoft" in raw_release
    mapping = {
        ("linux", "x86_64"): ("linux-x86_64", "x86_64-unknown-linux-gnu"),
        ("linux", "arm64"): ("linux-arm64", "aarch64-unknown-linux-gnu"),
        ("windows", "x86_64"): ("windows-x86_64", "x86_64-pc-windows-msvc"),
        ("windows", "arm64"): ("windows-arm64", "aarch64-pc-windows-msvc"),
        ("macos", "x86_64"): ("macos-x86_64", "x86_64-apple-darwin"),
        ("macos", "arm64"): ("macos-arm64", "aarch64-apple-darwin"),
    }
    resolved = mapping.get((normalized_system, normalized_machine))
    if not resolved:
        raise ClawPackagedRuntimeError(
            "Unsupported packaged Claw platform "
            f"(system={raw_system or '<unknown>'}, machine={raw_machine or '<unknown>'})"
        )
    key, triple = resolved
    candidate_keys = (f"{key}-wsl", key) if is_wsl else (key,)
    return ClawPlatform(
        key=key,
        rust_target_triple=triple,
        system=normalized_system,
        machine=normalized_machine,
        is_wsl=is_wsl,
        candidate_keys=candidate_keys,
    )


def _packaged_claw_roots(global_config: Any | None = None) -> list[Path]:
    project_root = getattr(global_config, "project_root", None)
    if project_root:
        roots = [Path(project_root).expanduser() / "hashi_assets" / "claw"]
    else:
        roots = [Path(__file__).resolve().parent.parent / "hashi_assets" / "claw"]
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_packaged_claw_manifest(manifest_path: Path) -> PackagedClawManifest:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ClawPackagedRuntimeError(f"Packaged Claw manifest not found: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ClawPackagedRuntimeError(f"Packaged Claw manifest is invalid JSON: {manifest_path}") from exc
    if not isinstance(payload, dict):
        raise ClawPackagedRuntimeError(f"Packaged Claw manifest must be an object: {manifest_path}")
    manifest_version = payload.get("manifest_version")
    if manifest_version != PACKAGED_CLAW_MANIFEST_VERSION:
        raise ClawPackagedRuntimeError(
            f"Packaged Claw manifest_version must be {PACKAGED_CLAW_MANIFEST_VERSION}; got {manifest_version!r}"
        )
    runtime = str(payload.get("runtime") or "").strip()
    if runtime != PACKAGED_CLAW_RUNTIME:
        raise ClawPackagedRuntimeError(
            f"Packaged Claw manifest runtime must be {PACKAGED_CLAW_RUNTIME!r}; got {runtime!r}"
        )
    version = str(payload.get("version") or "").strip()
    if not version:
        raise ClawPackagedRuntimeError(f"Packaged Claw manifest missing version: {manifest_path}")
    raw_binaries = payload.get("binaries")
    if not isinstance(raw_binaries, Mapping):
        raise ClawPackagedRuntimeError(f"Packaged Claw manifest binaries must be an object: {manifest_path}")
    binaries: dict[str, PackagedClawBinarySpec] = {}
    for platform_key, raw_spec in raw_binaries.items():
        if not isinstance(raw_spec, Mapping):
            raise ClawPackagedRuntimeError(f"Packaged Claw binary entry must be an object: {platform_key}")
        rel_path = Path(str(raw_spec.get("path") or "").strip())
        sha256 = str(raw_spec.get("sha256") or "").strip().lower()
        rust_target_triple = str(raw_spec.get("rust_target_triple") or raw_spec.get("triple") or "").strip()
        binary_name = str(raw_spec.get("binary_name") or rel_path.name).strip()
        if not rel_path.as_posix() or rel_path.is_absolute() or ".." in rel_path.parts:
            raise ClawPackagedRuntimeError(f"Packaged Claw path must be relative and stay under root: {platform_key}")
        if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
            raise ClawPackagedRuntimeError(f"Packaged Claw sha256 must be a 64-character hex digest: {platform_key}")
        if not rust_target_triple:
            raise ClawPackagedRuntimeError(f"Packaged Claw rust_target_triple missing: {platform_key}")
        binaries[str(platform_key)] = PackagedClawBinarySpec(
            platform_key=str(platform_key),
            relative_path=rel_path,
            sha256=sha256,
            rust_target_triple=rust_target_triple,
            binary_name=binary_name,
        )
    return PackagedClawManifest(manifest_path=manifest_path, version=version, binaries=binaries)


def resolve_packaged_claw_binary(
    packaged_root: Path,
    *,
    platform: ClawPlatform | None = None,
) -> ClawBinaryResolution:
    platform = platform or detect_hashi_claw_platform()
    manifest = load_packaged_claw_manifest(packaged_root / "manifest.json")
    spec = next((manifest.binaries.get(key) for key in platform.candidate_keys if manifest.binaries.get(key)), None)
    if spec is None:
        supported = ", ".join(sorted(manifest.binaries)) or "<none>"
        raise ClawPackagedRuntimeError(
            f"Packaged Claw manifest has no binary for {platform.key}; supported={supported}"
        )
    if spec.rust_target_triple != platform.rust_target_triple:
        raise ClawPackagedRuntimeError(
            f"Packaged Claw target mismatch for {spec.platform_key}: "
            f"expected {platform.rust_target_triple}, got {spec.rust_target_triple}"
        )
    binary_path = (packaged_root / spec.relative_path).resolve()
    try:
        binary_path.relative_to(packaged_root.resolve())
    except ValueError as exc:
        raise ClawPackagedRuntimeError(f"Packaged Claw binary escapes packaged root: {spec.relative_path}") from exc
    if not binary_path.is_file():
        raise ClawPackagedRuntimeError(f"Packaged Claw binary missing: {binary_path}")
    if not os.access(binary_path, os.X_OK):
        raise ClawPackagedRuntimeError(f"Packaged Claw binary is not executable: {binary_path}")
    actual_sha256 = _sha256_file(binary_path)
    if actual_sha256 != spec.sha256:
        raise ClawPackagedRuntimeError(
            f"Packaged Claw checksum mismatch for {binary_path}: "
            f"expected {spec.sha256[:12]}..., got {actual_sha256[:12]}..."
        )
    return ClawBinaryResolution(
        path=binary_path,
        source="packaged",
        platform=platform,
        manifest_path=manifest.manifest_path,
        packaged_version=manifest.version,
    )


def _claw_runtime_policy(global_config: Any | None = None, agent_config: Any | None = None) -> str:
    extra = getattr(agent_config, "extra", None) or {}
    if isinstance(extra, Mapping) and extra.get("claw_runtime_policy"):
        policy = str(extra.get("claw_runtime_policy")).strip().lower()
    else:
        global_claw = getattr(global_config, "claw_providers", None) or {}
        policy = str(global_claw.get("runtime_policy") or "prefer-packaged").strip().lower() if isinstance(global_claw, Mapping) else "prefer-packaged"
    if policy not in CLAW_RUNTIME_POLICIES:
        raise ClawBinaryNotFound(f"Invalid claw_runtime_policy={policy!r}; expected one of {sorted(CLAW_RUNTIME_POLICIES)}")
    return policy


def _resolve_executable_candidate(candidate: str | os.PathLike[str]) -> tuple[Path | None, str | None]:
    raw = str(candidate).strip()
    if not raw:
        return None, None
    resolved = shutil.which(raw) if os.path.basename(raw) == raw else raw
    if not resolved:
        return None, f"{raw}: not found on PATH"
    path = Path(resolved).expanduser()
    if not path.exists():
        return None, f"{path}: does not exist"
    if not path.is_file():
        return None, f"{path}: not a file"
    if not os.access(path, os.X_OK):
        return None, f"{path}: not executable"
    return path.resolve(), None


def discover_claw_binary(
    configured_path: str | os.PathLike[str] | None = None,
    *,
    global_config: Any | None = None,
    agent_config: Any | None = None,
    env: Mapping[str, str] | None = None,
) -> ClawBinaryResolution:
    """Resolve an executable Claw binary without requiring Cargo."""
    policy = _claw_runtime_policy(global_config, agent_config)
    early_candidates: list[tuple[str, str | os.PathLike[str]]] = []
    if configured_path:
        early_candidates.append(("configured", configured_path))

    extra = getattr(agent_config, "extra", None) or {}
    if isinstance(extra, Mapping):
        for key in ("claw_binary_path", "claw_cmd"):
            value = extra.get(key)
            if value:
                early_candidates.append((f"agent:{key}", value))

    for key in ("claw_binary_path", "claw_cmd"):
        value = getattr(global_config, key, None)
        if value:
            early_candidates.append((f"global:{key}", value))

    global_claw = getattr(global_config, "claw_providers", None) or {}
    if isinstance(global_claw, Mapping):
        for key in ("binary_path", "claw_binary_path", "claw_cmd"):
            value = global_claw.get(key)
            if value:
                early_candidates.append((f"global.claw_providers:{key}", value))

    failures: list[str] = []
    if policy != "require-packaged":
        for source, candidate in early_candidates:
            path, failure = _resolve_executable_candidate(candidate)
            if path is not None:
                return ClawBinaryResolution(path=path, source=source)
            if failure:
                failures.append(failure)
        if early_candidates:
            detail = "; ".join(failures) if failures else "configured Claw runtime is unavailable"
            raise ClawBinaryNotFound(f"Configured Claw binary not found ({detail})")

    packaged_errors: list[str] = []
    if policy != "system-only":
        for root in _packaged_claw_roots(global_config):
            manifest_path = root / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                return resolve_packaged_claw_binary(root)
            except ClawPackagedRuntimeError as exc:
                packaged_errors.append(str(exc))
        failures.extend(packaged_errors)
        if policy == "require-packaged":
            detail = "; ".join(failures) if failures else "no packaged Claw manifest found"
            raise ClawBinaryNotFound(f"Packaged Claw runtime required but unavailable ({detail})")

    candidates: list[tuple[str, str | os.PathLike[str]]] = []
    env = env or os.environ
    for key in ("CLAW_BINARY", "CLAW_BIN"):
        value = env.get(key)
        if value:
            candidates.append((f"env:{key}", value))

    candidates.append(("PATH", "claw"))

    for source, candidate in candidates:
        path, failure = _resolve_executable_candidate(candidate)
        if path is not None:
            warnings = tuple(packaged_errors) if packaged_errors and source.startswith(("env:", "PATH")) else ()
            return ClawBinaryResolution(path=path, source=source, warnings=warnings)
        if failure:
            failures.append(failure)

    detail = "; ".join(failures) if failures else "no candidate configured"
    raise ClawBinaryNotFound(f"Claw binary not found ({detail})")


def find_claw_binary(
    configured_path: str | os.PathLike[str] | None = None,
    *,
    global_config: Any | None = None,
    agent_config: Any | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    return discover_claw_binary(
        configured_path,
        global_config=global_config,
        agent_config=agent_config,
        env=env,
    ).path


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


def _parse_stream_json_output(text: str, *, command: list[str]) -> dict[str, Any]:
    final: dict[str, Any] | None = None
    last_error: dict[str, Any] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ClawJsonError(
                f"Claw stream-json produced non-JSON line for {' '.join(command)}: {line[:400]}",
                output=text,
            ) from exc
        if isinstance(event, dict):
            if event.get("kind") == "run_finished":
                final = event
            elif event.get("kind") == "error" or event.get("type") == "error" or event.get("error"):
                last_error = event
    if final is None:
        if last_error is not None:
            return last_error
        raise ClawJsonError(
            f"Claw stream-json did not include run_finished for {' '.join(command)}",
            output=text,
        )
    return final


def _stream_json_usage(text: str) -> dict[str, Any]:
    usage = ClawThinkingStreamUsage()
    legacy_summary_chars = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping):
            continue
        kind = event.get("kind")
        if kind in {"thinking_delta", "thinking_redacted"}:
            thinking_chars = int(event.get("thinking_chars") or 0)
            usage.thinking_chars += thinking_chars
            usage.thinking_event_count += 1
            usage.saw_actual_thinking_event = True
            source = str(event.get("reasoning_source") or "").strip()
            if source:
                usage.thinking_sources.add(source)
            if kind == "thinking_redacted":
                usage.thinking_redacted_count += 1
        elif kind == "thinking_summary":
            legacy_summary_chars += int(event.get("thinking_chars") or 0)
        if event.get("kind") == "usage":
            thinking_tokens = int(
                event.get("thinking_tokens")
                or (event.get("completion_tokens_details") or {}).get("reasoning_tokens")
                or 0
            )
            usage.thinking_tokens = max(usage.thinking_tokens, thinking_tokens)
    if not usage.saw_actual_thinking_event and legacy_summary_chars > 0:
        usage.thinking_chars += legacy_summary_chars
        usage.thinking_event_count += 1
    if usage.thinking_tokens == 0 and usage.thinking_chars > 0:
        usage.thinking_tokens = max(1, usage.thinking_chars // 4)
    return usage.to_dict()


def _claw_jsonl_to_stream_event(event: Mapping[str, Any]) -> StreamEvent | None:
    kind = str(event.get("kind") or "")
    if kind == "run_started":
        model = event.get("model") or "model unknown"
        return StreamEvent(
            kind=KIND_THINKING,
            summary=f"Claw stream started ({model})",
        )
    if kind == "thinking_delta":
        text = str(event.get("text") or "")
        thinking_chars = int(event.get("thinking_chars") or len(text))
        source = str(event.get("reasoning_source") or "").strip()
        detail_parts = [f"thinking_chars={thinking_chars}"] if thinking_chars > 0 else []
        if source:
            detail_parts.append(f"source={source}")
        return (
            StreamEvent(kind=KIND_THINKING, summary=text[:400], detail=";".join(detail_parts))
            if text
            else None
        )
    if kind == "thinking_redacted":
        summary = str(event.get("summary") or "provider emitted redacted reasoning block")
        thinking_chars = int(event.get("thinking_chars") or 0)
        source = str(event.get("reasoning_source") or "").strip()
        detail_parts = [f"thinking_chars={thinking_chars}", "redacted=true"]
        if source:
            detail_parts.append(f"source={source}")
        return StreamEvent(kind=KIND_THINKING, summary=summary[:400], detail=";".join(detail_parts))
    if kind == "thinking_summary":
        summary = str(event.get("summary") or "Claw thinking")
        thinking_chars = int(event.get("thinking_chars") or 0)
        detail = f"thinking_chars={thinking_chars}" if thinking_chars > 0 else ""
        return StreamEvent(kind=KIND_THINKING, summary=summary[:400], detail=detail)
    if kind == "assistant_delta":
        text = str(event.get("text") or "")
        return StreamEvent(kind=KIND_TEXT_DELTA, summary=text[:200]) if text else None
    if kind in {"tool_call", "tool_start"}:
        name = str(event.get("name") or event.get("tool_name") or "tool")
        summary = str(event.get("summary") or f"Claw tool started: {name}")
        return StreamEvent(kind=KIND_TOOL_START, summary=summary[:200], tool_name=name)
    if kind == "tool_end":
        name = str(event.get("name") or event.get("tool_name") or "tool")
        summary = str(event.get("summary") or f"Claw tool finished: {name}")
        detail = str(event.get("output_preview") or "")
        return StreamEvent(kind=KIND_TOOL_END, summary=summary[:200], detail=detail[:500], tool_name=name)
    if kind == "usage":
        thinking_tokens = int(
            event.get("thinking_tokens")
            or (event.get("completion_tokens_details") or {}).get("reasoning_tokens")
            or 0
        )
        return StreamEvent(
            kind=KIND_PROGRESS,
            summary=(
                f"Claw usage input={int(event.get('input_tokens') or 0)} "
                f"output={int(event.get('output_tokens') or 0)} "
                f"thinking={thinking_tokens} "
                f"thinking_source={event.get('thinking_token_source') or 'unavailable'}"
            ),
        )
    if kind == "error":
        return StreamEvent(kind=KIND_ERROR, summary=str(event.get("error") or event)[:400])
    if event.get("type") == "error" or event.get("error"):
        return StreamEvent(kind=KIND_ERROR, summary=str(event.get("error") or event)[:400])
    if kind in {"message_stop", "run_finished", "prompt_cache"}:
        return None
    return StreamEvent(kind=KIND_PROGRESS, summary=f"Claw event: {kind}"[:200])


def claw_supports_stream_json(
    binary_path: str | os.PathLike[str],
    cwd: str | os.PathLike[str],
    env: Mapping[str, str] | None = None,
    timeout_s: float = 5,
) -> bool:
    process_env = build_claw_env(env)
    try:
        completed = subprocess.run(
            [str(binary_path), "--help"],
            cwd=cwd,
            env=process_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "stream-json" in f"{completed.stdout}\n{completed.stderr}"


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
    output_format: str = "json",
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
    if output_format not in {"json", "stream-json"}:
        raise ValueError("output_format must be json or stream-json")
    args = [
        "--model",
        model,
        "--permission-mode",
        permission_mode,
        "--output-format",
        output_format,
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
        capabilities = BackendCapabilities(
            supports_sessions=True,
            supports_files=True,
            supports_tool_use=True,
            supports_thinking_stream=False,
            supports_headless_mode=True,
        )
        capabilities.supports_answer_stream = False
        return capabilities

    def __init__(self, agent_config, global_config, api_key: str = None):
        super().__init__(agent_config, global_config, api_key)
        self.logger = logging.getLogger(f"Backend.Claw.{self.config.name}")
        self.current_proc = None
        self._binary: Path | None = None
        self._binary_resolution: ClawBinaryResolution | None = None
        self._supports_stream_json = False
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

    def _global_claw_config(self) -> dict[str, Any]:
        raw = getattr(self.global_config, "claw_providers", None) or {}
        return dict(raw) if isinstance(raw, Mapping) else {}

    def _provider_configs(self) -> dict[str, Any]:
        providers = self._global_claw_config().get("providers") or {}
        return dict(providers) if isinstance(providers, Mapping) else {}

    def _provider_and_model(self) -> tuple[str | None, str]:
        model = str(self.config.model or "").strip()
        provider = self._extra.get("provider")
        if provider:
            return str(provider).strip(), model
        if ":" in model:
            maybe_provider, maybe_model = model.split(":", 1)
            if maybe_provider in self._provider_configs() and maybe_model:
                return maybe_provider, maybe_model
        return None, model

    def _claw_model(self) -> str:
        return self._provider_and_model()[1]

    def _permission_mode(self) -> str:
        requested = str(self._extra.get("permission_mode") or "workspace-write")
        if requested not in VALID_PERMISSION_MODES:
            return requested
        max_mode = str(self._global_claw_config().get("max_permission_mode") or "").strip()
        if max_mode in VALID_PERMISSION_MODES and PERMISSION_MODE_RANK[requested] > PERMISSION_MODE_RANK[max_mode]:
            self.logger.warning(
                "Claw permission_mode %s exceeds global max_permission_mode %s; using %s.",
                requested,
                max_mode,
                max_mode,
            )
            return max_mode
        return requested

    def _skip_permissions(self) -> bool:
        return bool(self._extra.get("skip_permissions") or self._extra.get("dangerously_skip_permissions"))

    def _legacy_openai_base_url(self) -> str:
        return str(self._extra.get("openai_base_url") or DEFAULT_OPENROUTER_BASE_URL)

    def _hashi_secrets(self) -> dict[str, Any]:
        raw = getattr(self.config, "_hashi_secrets", None)
        if isinstance(raw, Mapping):
            return dict(raw)
        raw = getattr(self.global_config, "secrets", None)
        return dict(raw) if isinstance(raw, Mapping) else {}

    def _env_from_agent_extra(self) -> dict[str, str]:
        env_source = dict(os.environ)
        if self.api_key:
            env_source["OPENAI_API_KEY"] = str(self.api_key)
        if self._legacy_openai_base_url():
            env_source["OPENAI_BASE_URL"] = self._legacy_openai_base_url()
        return build_claw_env(env_source)

    def _provider_auth_mode(self, provider: Mapping[str, Any]) -> str:
        return str(provider.get("auth_mode") or "").strip().lower()

    def _env_from_hashi_xai_oauth(self, provider_name: str, provider: Mapping[str, Any]) -> dict[str, str]:
        """Inject HASHI-native xAI OAuth access token into Claw (no Hermes, no grok-cli)."""
        from adapters.hashi_xai_oauth import (
            HashiXaiOAuthError,
            resolve_base_url,
            resolve_hashi_xai_credentials,
        )

        try:
            creds = resolve_hashi_xai_credentials(
                global_config=self.global_config,
                provider_cfg=provider,
                force_refresh=False,
            )
        except HashiXaiOAuthError as exc:
            raise ClawProviderConfigError(
                f"Claw provider {provider_name} HASHI xAI OAuth unavailable: {exc}"
            ) from exc

        env_api_key = str(provider.get("env_api_key") or "XAI_API_KEY").strip() or "XAI_API_KEY"
        env_base_url = str(provider.get("env_base_url") or "XAI_BASE_URL").strip() or "XAI_BASE_URL"
        base_url = str(provider.get("base_url") or creds.base_url or resolve_base_url(self.global_config)).strip()

        env_source = dict(os.environ)
        env_source[env_api_key] = creds.access_token
        if base_url:
            env_source[env_base_url] = base_url
            # Some OpenAI-compat paths also honor OPENAI_*; keep XAI_* primary for Claw xAI routing.
            if env_api_key == "OPENAI_API_KEY":
                env_source["OPENAI_BASE_URL"] = base_url
        self.logger.info(
            "Claw provider %s using HASHI xAI OAuth (source=%s).",
            provider_name,
            creds.source,
        )
        return build_claw_env(env_source)

    def _env_from_provider(self, provider_name: str) -> dict[str, str]:
        providers = self._provider_configs()
        provider = providers.get(provider_name)
        if not isinstance(provider, Mapping):
            raise ClawProviderConfigError(f"Claw provider is not configured: {provider_name}")

        status = str(provider.get("status") or "stable").strip().lower()
        if status == "disabled":
            raise ClawProviderConfigError(f"Claw provider is disabled: {provider_name}")
        if status == "provisional":
            self.logger.warning("Claw provider %s is provisional; running with warning diagnostics.", provider_name)

        auth_mode = self._provider_auth_mode(provider)
        if auth_mode in {"hashi_oauth", "hashi-xai-oauth", "xai_oauth"}:
            return self._env_from_hashi_xai_oauth(provider_name, provider)

        base_url = str(provider.get("base_url") or "").strip()
        if not base_url:
            raise ClawProviderConfigError(f"Claw provider {provider_name} has no base_url")

        secret_name = provider.get("secret")
        api_key = None
        if secret_name:
            secrets = self._hashi_secrets()
            api_key = secrets.get(str(secret_name))
            if not api_key:
                raise ClawProviderSecretMissing(
                    f"Claw provider {provider_name} requires missing secret: {secret_name}"
                )
        else:
            api_key = provider.get("dummy_api_key")

        env_source = dict(os.environ)
        env_source["OPENAI_BASE_URL"] = base_url
        if api_key:
            env_source["OPENAI_API_KEY"] = str(api_key)
        return build_claw_env(env_source)

    def _resolve_task_env(self) -> dict[str, str]:
        provider_name, _ = self._provider_and_model()
        if provider_name:
            if self._extra.get("openai_base_url"):
                self.logger.warning(
                    "Claw provider=%s overrides legacy openai_base_url for %s.",
                    provider_name,
                    self.config.name,
                )
            return self._env_from_provider(provider_name)
        return self._env_from_agent_extra()

    def _task_env(self) -> dict[str, str]:
        return self._resolve_task_env()

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
            self._binary_resolution = discover_claw_binary(
                global_config=self.global_config,
                agent_config=self.config,
            )
            self._binary = self._binary_resolution.path
            for warning in self._binary_resolution.warnings:
                self.logger.warning("Claw binary discovery warning: %s", warning)
            version = await asyncio.to_thread(
                run_claw_version,
                self.effective_workdir,
                binary_path=self._binary,
                env=self._task_env(),
                timeout_s=30,
            )
            self.logger.info(
                "Claw CLI version check passed "
                f"(binary={self._binary}, source={self._binary_resolution.source}, "
                f"packaged_version={self._binary_resolution.packaged_version}, "
                f"manifest={self._binary_resolution.manifest_path}, "
                f"version={version.get('version')}, git_sha={version.get('git_sha')})"
            )
            self._supports_stream_json = await asyncio.to_thread(
                claw_supports_stream_json,
                self._binary,
                self.effective_workdir,
                self._task_env(),
            )
            self.capabilities.supports_thinking_stream = self._supports_stream_json
            self.capabilities.supports_answer_stream = self._supports_stream_json
            if not self._supports_stream_json:
                self.logger.warning("Claw binary does not advertise stream-json; verbose mode will use JSON fallback.")
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
                self._binary_resolution = discover_claw_binary(global_config=self.global_config, agent_config=self.config)
                self._binary = self._binary_resolution.path
                for warning in self._binary_resolution.warnings:
                    self.logger.warning("Claw binary discovery warning: %s", warning)
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
                on_stream_event=on_stream_event,
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
        usage_data = result.json_data.get("usage") or {}
        stream_usage = _stream_json_usage(result.stdout) if on_stream_event is not None else {}
        thinking_tokens = int(
            usage_data.get("thinking_tokens")
            or (usage_data.get("completion_tokens_details") or {}).get("reasoning_tokens")
            or stream_usage.get("thinking_tokens")
            or 0
        )
        return BackendResponse(
            text=result.text,
            duration_ms=result.duration_ms,
            is_success=True,
            usage=TokenUsage(
                input_tokens=int(usage_data.get("input_tokens") or 0),
                output_tokens=int(usage_data.get("output_tokens") or 0),
                thinking_tokens=thinking_tokens,
            ),
            cost_usd=None,
            tool_call_count=len(result.tool_uses),
            tool_loop_count=result.iterations or 0,
            stream_metadata={"claw_thinking": stream_usage} if stream_usage else None,
        )

    @staticmethod
    def _duration_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 2)

    async def _run_task_async(
        self,
        prompt: str,
        *,
        resume: str | None,
        on_stream_event: StreamCallback = None,
    ) -> ClawTaskResult:
        if self._binary is None:
            raise ClawBinaryNotFound("Claw binary not initialized")
        args = build_claw_task_args(
            prompt,
            self._claw_model(),
            permission_mode=self._permission_mode(),
            resume=resume,
            allowed_tools=self._allowed_tools(),
            skip_permissions=self._skip_permissions(),
            output_format="stream-json" if on_stream_event is not None and self._supports_stream_json else "json",
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
            if on_stream_event is not None and self._supports_stream_json:
                stdout_data, stderr_data = await asyncio.wait_for(
                    self._communicate_stream_json(proc, command, on_stream_event),
                    timeout=self.HARD_TIMEOUT_SEC,
                )
            else:
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
        parsed = (
            _parse_stream_json_output(output, command=command)
            if on_stream_event is not None and self._supports_stream_json
            else (_parse_json_output(output, command=command) if output else {})
        )
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
            model=str(parsed.get("model") or self._claw_model()),
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

    async def _communicate_stream_json(
        self,
        proc: asyncio.subprocess.Process,
        command: list[str],
        on_stream_event: StreamCallback,
    ) -> tuple[bytes, bytes]:
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        async def read_stdout() -> None:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                stdout_chunks.append(line)
                self._touch_activity()
                try:
                    event = json.loads(line.decode(errors="replace"))
                except json.JSONDecodeError:
                    self.logger.warning("Ignoring non-JSON Claw stream line: %r", line[:200])
                    continue
                stream_event = _claw_jsonl_to_stream_event(event)
                if stream_event is not None:
                    await on_stream_event(stream_event)

        async def read_stderr() -> None:
            assert proc.stderr is not None
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                stderr_chunks.append(line)
                self._touch_activity()

        await asyncio.gather(read_stdout(), read_stderr())
        await proc.wait()
        return b"".join(stdout_chunks), b"".join(stderr_chunks)
