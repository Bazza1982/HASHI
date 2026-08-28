from __future__ import annotations
import sys
import os
import signal
import time
import asyncio
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from adapters.stream_events import StreamCallback
from adapters.timeout_policy import (
    AGENT_CONFIG_SOURCE,
    DEFAULT_SOURCE,
    HARD_TIMEOUT_KEY,
    IDLE_TIMEOUT_KEY,
    LEGACY_TIMEOUT_KEY,
    TIMEOUT_POLICY_META_KEY,
    parse_positive_timeout,
)


@dataclass
class BackendCapabilities:
    supports_sessions: bool
    supports_files: bool
    supports_tool_use: bool
    # True only when the backend exposes genuine provider reasoning content.
    # Generic busy/start messages must be emitted as progress instead.
    supports_thinking_stream: bool
    supports_headless_mode: bool
    # Model-authored interim commentary, distinct from private/provider
    # reasoning and from generic runtime/tool progress.
    supports_commentary_stream: bool = False
    # User-facing activity events such as started, planning, or elapsed work.
    supports_progress_stream: bool = False
    # Structured tool/file/shell start or result summaries.
    supports_tool_stream: bool = False
    # Assistant answer deltas. Kept for local observers; Telegram no longer
    # presents live answer previews.
    supports_answer_stream: bool = False
    # True when the selected model receives images directly from its provider.
    # Text-only models can instead opt into HASHI's vision_inspect tool.
    supports_native_vision: bool = False
    # Provider/model/modality-specific native input contract.  The legacy
    # booleans above remain compatibility signals, never final routing proof.
    input_modalities: frozenset[str] = frozenset({"text"})
    input_transports: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    input_limits: Mapping[str, int] = field(default_factory=dict)
    input_capability_source: str = "unknown_fail_closed"
    # Provider/model output support is independent from input support.  These
    # dimensions are explicit so an audio-input model is never assumed to
    # produce audio, use a particular endpoint, or support function calling.
    output_modalities: frozenset[str] = frozenset({"text"})
    api_surface: str = "unknown"
    input_formats: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    output_formats: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    supported_voices: tuple[str, ...] = ()
    output_streaming: str = "none"
    provider_output_transcript: bool = False
    function_calling: bool = False
    # Provider-neutral policy for models that technically accept text but
    # require an audio-bearing request shape for a particular route.
    input_policy: str = "auto"


@dataclass
class TokenUsage:
    """Real token usage from API response, or None for CLI backends."""
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0


@dataclass
class BackendResponse:
    text: str
    duration_ms: float
    error: Optional[str] = None
    is_success: bool = True
    tool_calls: Optional[list] = None   # Raw tool_calls list from API (V2.2+)
    stop_reason: Optional[str] = None   # e.g. "stop", "tool_calls", "length"
    usage: Optional[TokenUsage] = None  # Real token usage from API (V3.0+)
    cost_usd: Optional[float] = None    # Real cost from CLI/API when available
    tool_call_count: int = 0
    tool_loop_count: int = 0
    stream_metadata: Optional[dict[str, Any]] = None
    # Keep new fields at the end so older positional construction remains valid.
    # Consumers must still apply their own semantic schema to provider-native data.
    structured_data: Optional[dict[str, Any]] = None
    # Typed provider failure metadata.  These remain optional so every legacy
    # backend can roll forward without changing successful response creation.
    error_code: Optional[str] = None
    error_retryable: Optional[bool] = None
    http_status: Optional[int] = None
    provider_request_id: Optional[str] = None
    retry_after_s: Optional[float] = None
    side_effects_possible: bool = False
    # Provider-neutral output parts.  Durable parts contain asset references,
    # hashes, and metadata only; inline bytes/base64 are forbidden upstream.
    content: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.content is None:
            self.content = ()
        elif not isinstance(self.content, tuple):
            self.content = tuple(self.content)

    @property
    def has_deliverable_content(self) -> bool:
        if str(self.text or "").strip():
            return True
        return any(
            isinstance(part, Mapping)
            and str(part.get("type") or "").strip().casefold()
            in {"text", "audio"}
            and (
                bool(str(part.get("text") or "").strip())
                or bool(str(part.get("asset_id") or "").strip())
            )
            for part in self.content
        )


class BaseBackend(ABC):
    DEFAULT_IDLE_TIMEOUT_SEC = 1800
    # Compatibility value for the explicitly isolated HER v1 adapter. Active
    # backends do not enforce an absolute request clock.
    DEFAULT_HARD_TIMEOUT_SEC = 36000
    USES_LEGACY_HARD_TIMEOUT = False

    def __init__(self, agent_config, global_config, api_key: str = None):
        self.config = agent_config
        self.global_config = global_config
        self.api_key = api_key
        self._validate_timeout_configuration()
        self.capabilities = self._define_capabilities()
        extra = dict(getattr(self.config, "extra", {}) or {})
        configured_image_input = extra.get("image_input")
        image_input = str(configured_image_input or "none").strip().casefold()
        if image_input not in {"none", "native", "tool"}:
            raise ValueError("image_input must be one of: none, native, tool")
        capability = self.resolve_input_capability()
        self.input_capability = capability
        self.capabilities.input_modalities = capability.input_modalities
        self.capabilities.input_transports = dict(capability.input_transports)
        self.capabilities.input_limits = dict(capability.limits)
        self.capabilities.input_capability_source = capability.source
        self.capabilities.supports_native_vision = capability.supports("image")
        self._apply_declared_multimodal_capabilities(extra)
        self.image_input_mode = (
            image_input
            if configured_image_input is not None
            else ("native" if capability.supports("image") else "none")
        )
        self._console_write_warned = False
        # epoch-seconds; updated by adapter whenever backend produces output.
        # Used by the runtime escalation loop to detect stalled sub-processes.
        self.last_activity_at: float = 0.0
        # Process-local monotonic counterpart used for durations and deadlines.
        self.last_activity_monotonic: float = 0.0
        # cumulative count of output events (stdout lines for CLI, 1 for HTTP).
        # Codex increments this per stdout line; others increment once on start.
        self.output_line_count: int = 0

    @staticmethod
    def _configured_tuple(value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, (list, tuple, set, frozenset)):
            values = list(value)
        else:
            values = []
        normalized: list[str] = []
        for item in values:
            text = str(item or "").strip().casefold()
            if text and text not in normalized:
                normalized.append(text)
        return tuple(normalized)

    @classmethod
    def _configured_formats(cls, value: Any) -> dict[str, tuple[str, ...]]:
        if not isinstance(value, Mapping):
            return {}
        formats: dict[str, tuple[str, ...]] = {}
        for modality, entries in value.items():
            normalized = cls._configured_tuple(entries)
            if normalized:
                formats[str(modality or "").strip().casefold()] = normalized
        return formats

    def _apply_declared_multimodal_capabilities(
        self, extra: Mapping[str, Any]
    ) -> None:
        if "input_policy" in extra:
            input_policy = str(extra.get("input_policy") or "auto").strip().casefold()
            if input_policy not in {"auto", "audio_required"}:
                raise ValueError(
                    "input_policy must be one of: auto, audio_required"
                )
            self.capabilities.input_policy = input_policy
        output_modalities = self._configured_tuple(extra.get("output_modalities"))
        if output_modalities:
            self.capabilities.output_modalities = frozenset(output_modalities)
        self.capabilities.api_surface = str(
            extra.get("api_surface") or self.capabilities.api_surface
        ).strip().casefold()
        input_formats = self._configured_formats(extra.get("input_formats"))
        if input_formats:
            self.capabilities.input_formats = input_formats
        output_formats = self._configured_formats(extra.get("output_formats"))
        if output_formats:
            self.capabilities.output_formats = output_formats
        voices = self._configured_tuple(
            extra.get("supported_voices") or extra.get("native_audio_voices")
        )
        if voices:
            self.capabilities.supported_voices = voices
        self.capabilities.output_streaming = str(
            extra.get("output_streaming") or self.capabilities.output_streaming
        ).strip().casefold()
        if "provider_output_transcript" in extra:
            self.capabilities.provider_output_transcript = bool(
                extra.get("provider_output_transcript")
            )
        if "function_calling" in extra:
            self.capabilities.function_calling = bool(extra.get("function_calling"))

    def resolve_input_capability(self):
        """Resolve the current exact model on demand.

        HER stage profile options are installed after an ephemeral backend is
        constructed, so adapters must not rely only on the startup snapshot.
        """

        from orchestrator.multimodal_contract import resolve_input_capability

        engine = str(getattr(self.config, "engine", "") or "").strip()
        if not engine:
            engine = {
                "OpenRouterAdapter": "openrouter-api",
                "HashiApiAdapter": "hashi-api",
                "CodexCLIAdapter": "codex-cli",
                "HERv2Adapter": "her-v2",
            }.get(type(self).__name__, "")
        return resolve_input_capability(
            engine,
            getattr(self.config, "model", ""),
            config=dict(getattr(self.config, "extra", {}) or {}),
        )

    def authorized_media_roots(self) -> tuple[Path, ...]:
        candidates: list[Any] = [getattr(self.config, "workspace_dir", None)]
        try:
            candidates.append(self.effective_add_dir)
        except (AttributeError, OSError, TypeError, ValueError):
            pass
        candidates.append(getattr(self.global_config, "base_media_dir", None))
        roots: list[Path] = []
        for candidate in candidates:
            if not candidate:
                continue
            try:
                path = Path(candidate).expanduser().resolve()
            except (OSError, TypeError, ValueError):
                continue
            if path not in roots:
                roots.append(path)
        return tuple(roots)

    @property
    def PROCESS_TIMEOUT_SEC(self) -> int:
        """
        Legacy timeout alias.
        Preserved for compatibility; prefer IDLE_TIMEOUT_SEC / HARD_TIMEOUT_SEC.
        """
        return self.IDLE_TIMEOUT_SEC

    def _coerce_timeout(self, value, fallback: int, *, label: str) -> int:
        if value is None:
            return int(fallback)
        return parse_positive_timeout(value, label=label)

    @property
    def IDLE_TIMEOUT_SEC(self) -> int:
        """
        Maximum allowed silence from the backend subprocess before it is treated
        as stalled. Configurable via `idle_timeout_sec`; falls back to the
        legacy `process_timeout` if present.
        """
        extra = getattr(self.config, "extra", {}) or {}
        if "idle_timeout_sec" in extra:
            return self._coerce_timeout(
                extra.get("idle_timeout_sec"),
                self.DEFAULT_IDLE_TIMEOUT_SEC,
                label="idle timeout",
            )
        return self._coerce_timeout(
            extra.get("process_timeout"),
            self.DEFAULT_IDLE_TIMEOUT_SEC,
            label="idle timeout",
        )

    @property
    def HARD_TIMEOUT_SEC(self) -> int:
        """
        Legacy HER v1 absolute wall-clock cap.

        Active backends must not consult this property. It remains on the base
        interface only so the retired HER v1 compatibility adapter can be
        selected without duplicating its historical configuration parser.
        """
        extra = getattr(self.config, "extra", {}) or {}
        hard = self._coerce_timeout(
            extra.get("hard_timeout_sec"),
            self.DEFAULT_HARD_TIMEOUT_SEC,
            label="hard timeout",
        )
        if hard < self.IDLE_TIMEOUT_SEC:
            raise ValueError("hard timeout must be greater than or equal to idle timeout")
        return hard

    def _validate_timeout_configuration(self) -> None:
        _ = self.IDLE_TIMEOUT_SEC
        if self.USES_LEGACY_HARD_TIMEOUT:
            _ = self.HARD_TIMEOUT_SEC

    def _timeout_source(self, key: str) -> str:
        extra = getattr(self.config, "extra", {}) or {}
        meta = extra.get(TIMEOUT_POLICY_META_KEY)
        if isinstance(meta, dict):
            sources = meta.get("sources")
            if isinstance(sources, dict) and sources.get(key):
                return str(sources[key])
        if key == IDLE_TIMEOUT_KEY and (
            IDLE_TIMEOUT_KEY in extra or LEGACY_TIMEOUT_KEY in extra
        ):
            return AGENT_CONFIG_SOURCE
        if key == HARD_TIMEOUT_KEY and HARD_TIMEOUT_KEY in extra:
            return AGENT_CONFIG_SOURCE
        return DEFAULT_SOURCE

    def _timeout_diagnostic(self, timeout_kind: str, *, started_monotonic: float) -> str:
        total_runtime = max(0.0, time.perf_counter() - started_monotonic)
        last_output_age = self._last_activity_age()
        idle_source = self._timeout_source(IDLE_TIMEOUT_KEY).replace(" ", "_")
        diagnostic = (
            f"kind={timeout_kind}, idle_timeout_s={self.IDLE_TIMEOUT_SEC}, "
            f"idle_source={idle_source}, last_output_age_s={last_output_age:.2f}, "
            f"total_runtime_s={total_runtime:.2f}"
        )
        if not self.USES_LEGACY_HARD_TIMEOUT:
            return diagnostic
        hard_source = self._timeout_source(HARD_TIMEOUT_KEY).replace(" ", "_")
        return (
            f"{diagnostic}, legacy_her_hard_timeout_s={self.HARD_TIMEOUT_SEC}, "
            f"legacy_her_hard_source={hard_source}"
        )

    async def _wait_for_task_with_timeouts(
        self,
        task: asyncio.Future,
        *,
        started_monotonic: float,
    ) -> str | None:
        """Wait for a task using meaningful output idleness only."""
        while not task.done():
            idle_for = self._last_activity_age()
            if idle_for >= self.IDLE_TIMEOUT_SEC:
                return "idle"
            wait_slice = min(
                5.0,
                max(0.1, self.IDLE_TIMEOUT_SEC - idle_for),
            )
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=wait_slice)
            except asyncio.TimeoutError:
                continue
        return None

    def _last_activity_age(self) -> float:
        activity_monotonic = float(
            getattr(self, "last_activity_monotonic", 0.0) or 0.0
        )
        if activity_monotonic > 0:
            return max(0.0, time.monotonic() - activity_monotonic)
        # Compatibility for a backend instance created before a minimal hot reload.
        activity_wall = float(getattr(self, "last_activity_at", 0.0) or 0.0)
        if activity_wall > 0:
            return max(0.0, time.time() - activity_wall)
        return 0.0

    def _touch_activity(self) -> None:
        """Record that the backend just produced output. Call on every stdout/stderr chunk."""
        self.last_activity_at = time.time()
        self.last_activity_monotonic = time.monotonic()
        self.output_line_count += 1

    @property
    def workzone_dir(self) -> Path | None:
        extra = getattr(self.config, "extra", {}) or {}
        zone = str(extra.get("workzone_dir") or "").strip()
        if not zone:
            return None
        path = Path(zone).expanduser()
        try:
            path = path.resolve()
        except Exception:
            pass
        return path if path.is_dir() else None

    @property
    def effective_workdir(self) -> Path:
        if self.workzone_dir is not None:
            return self.workzone_dir
        return self.config.workspace_dir

    @property
    def effective_add_dir(self) -> str:
        if self.workzone_dir is not None:
            return str(self.workzone_dir)
        return str(self.config.resolve_access_root())

    def _preview_text(self, text: str | bytes | None, limit: int = 400) -> str:
        if text is None:
            return "<none>"
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        compact = " ".join(str(text).split())
        if not compact:
            return "<empty>"
        if len(compact) <= limit:
            return compact
        return compact[: limit - 16].rstrip() + " ...[truncated]"

    async def _describe_process(self, pid: int) -> str:
        if not pid:
            return "<no pid>"
        if os.name == "nt":
            def _tasklist():
                completed = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "LIST"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                return (completed.stdout or completed.stderr or "").strip()
            try:
                output = await asyncio.to_thread(_tasklist)
                return output or "<no tasklist output>"
            except Exception as exc:
                return f"<tasklist failed: {exc}>"
        return f"pid={pid}"

    async def force_kill_process_tree(self, proc, logger=None, reason: str = "") -> bool:
        if not proc:
            return False

        pid = getattr(proc, "pid", None)
        returncode = getattr(proc, "returncode", None)
        if returncode is not None:
            return False
        if not isinstance(pid, int) or pid <= 0:
            if logger:
                logger.error(
                    "Refusing to terminate a process tree without a valid pid "
                    f"reason={reason!r}"
                )
            return False

        try:
            if os.name == "nt" and pid:
                def _taskkill():
                    return subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )

                completed = await asyncio.to_thread(_taskkill)
                if logger:
                    stderr_preview = self._preview_text(completed.stderr)
                    stdout_preview = self._preview_text(completed.stdout)
                    logger.warning(
                        f"Forced taskkill for pid={pid} reason={reason!r} "
                        f"(rc={completed.returncode}, stdout={stdout_preview}, stderr={stderr_preview})"
                    )
            else:
                # On Linux/Mac: kill the entire process group to catch child processes
                # that may be holding stdout/stderr pipes open.  Only a process
                # that leads its own group is eligible for group termination.
                # An unisolated child inherits HASHI's group; killpg() against
                # that group would kill the Bridge and its launcher as well.
                try:
                    pgid = os.getpgid(pid)
                    own_pgid = os.getpgrp()
                    if pgid == own_pgid or pgid != pid:
                        proc.kill()
                        if logger:
                            logger.error(
                                "Refused unsafe killpg and killed only the child "
                                f"pid={pid} pgid={pgid} own_pgid={own_pgid} "
                                f"reason={reason!r}"
                            )
                    else:
                        os.killpg(pgid, signal.SIGKILL)
                        if logger:
                            logger.warning(
                                f"Forced killpg(pgid={pgid}) for pid={pid} "
                                f"reason={reason!r}"
                            )
                except (ProcessLookupError, PermissionError):
                    # Fallback if process group kill fails
                    proc.kill()
                    if logger:
                        logger.warning(f"Forced kill (pgid failed) for pid={pid} reason={reason!r}")
        except Exception as exc:
            if logger:
                logger.warning(f"Failed to terminate pid={pid} reason={reason!r}: {exc}")
            return False

        try:
            await asyncio.wait_for(proc.communicate(), timeout=5)
        except Exception:
            pass
        return True

    @abstractmethod
    def _define_capabilities(self) -> BackendCapabilities:
        pass

    @abstractmethod
    async def initialize(self) -> bool:
        """Probe sessions, authenticate, or setup workspace."""
        pass

    @abstractmethod
    async def generate_response(
        self, prompt: str, request_id: str, is_retry: bool = False, silent: bool = False,
        on_stream_event: StreamCallback = None,
        request_content: Mapping[str, Any] | None = None,
    ) -> BackendResponse:
        """Generate a response, optionally with canonical structured input."""
        pass

    @abstractmethod
    async def shutdown(self):
        """Clean up resources, terminate subprocesses."""
        pass

    @abstractmethod
    async def handle_new_session(self) -> bool:
        """Start a fresh session/context."""
        pass

    def should_bootstrap_on_startup(self) -> bool:
        return False

    def get_startup_bootstrap_prompt(self) -> str | None:
        return None

    def emit_console_text(self, text: str, logger=None):
        if not text:
            return

        stream = sys.stdout
        try:
            stream.write(text)
            stream.flush()
            return
        except (UnicodeEncodeError, OSError):
            encoding = getattr(stream, "encoding", None) or "utf-8"
            safe_text = text.encode(encoding, errors="backslashreplace").decode(
                encoding, errors="replace"
            )
        except Exception as exc:
            if logger and not self._console_write_warned:
                logger.warning(f"Console output disabled for this session: {exc}")
                self._console_write_warned = True
            return

        try:
            stream.write(safe_text)
            stream.flush()
        except Exception as exc:
            if logger and not self._console_write_warned:
                logger.warning(f"Console output disabled for this session: {exc}")
                self._console_write_warned = True
