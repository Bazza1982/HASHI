from __future__ import annotations
import sys
import os
import signal
import time
import asyncio
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from adapters.stream_events import StreamCallback


@dataclass
class BackendCapabilities:
    supports_sessions: bool
    supports_files: bool
    supports_tool_use: bool
    supports_thinking_stream: bool
    supports_headless_mode: bool


@dataclass
class BackendResponse:
    text: str
    duration_ms: float
    error: Optional[str] = None
    is_success: bool = True


class BaseBackend(ABC):
    DEFAULT_IDLE_TIMEOUT_SEC = 120
    DEFAULT_HARD_TIMEOUT_SEC = 600

    def __init__(self, agent_config, global_config, api_key: str = None):
        self.config = agent_config
        self.global_config = global_config
        self.api_key = api_key
        self.capabilities = self._define_capabilities()
        self._console_write_warned = False
        # epoch-seconds; updated by adapter whenever backend produces output.
        # Used by the runtime escalation loop to detect stalled sub-processes.
        self.last_activity_at: float = 0.0
        # cumulative count of output events (stdout lines for CLI, 1 for HTTP).
        # Codex increments this per stdout line; others increment once on start.
        self.output_line_count: int = 0

    @property
    def PROCESS_TIMEOUT_SEC(self) -> int:
        """
        Legacy timeout alias.
        Preserved for compatibility; prefer IDLE_TIMEOUT_SEC / HARD_TIMEOUT_SEC.
        """
        extra = getattr(self.config, "extra", {}) or {}
        return int(extra.get("process_timeout", self.DEFAULT_IDLE_TIMEOUT_SEC))

    def _coerce_timeout(self, value, fallback: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return fallback
        return parsed if parsed > 0 else fallback

    @property
    def IDLE_TIMEOUT_SEC(self) -> int:
        """
        Maximum allowed silence from the backend subprocess before it is treated
        as stalled. Configurable via `idle_timeout_sec`; falls back to the
        legacy `process_timeout` if present.
        """
        extra = getattr(self.config, "extra", {}) or {}
        if "idle_timeout_sec" in extra:
            return self._coerce_timeout(extra.get("idle_timeout_sec"), self.DEFAULT_IDLE_TIMEOUT_SEC)
        return self._coerce_timeout(extra.get("process_timeout"), self.DEFAULT_IDLE_TIMEOUT_SEC)

    @property
    def HARD_TIMEOUT_SEC(self) -> int:
        """
        Absolute wall-clock cap for a single backend request.
        Configurable via `hard_timeout_sec`.
        """
        extra = getattr(self.config, "extra", {}) or {}
        floor = max(self.IDLE_TIMEOUT_SEC, self.DEFAULT_HARD_TIMEOUT_SEC)
        return self._coerce_timeout(extra.get("hard_timeout_sec"), floor)

    def _touch_activity(self) -> None:
        """Record that the backend just produced output. Call on every stdout/stderr chunk."""
        self.last_activity_at = time.time()
        self.output_line_count += 1

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
                # that may be holding stdout/stderr pipes open.
                try:
                    pgid = os.getpgid(pid)
                    os.killpg(pgid, signal.SIGKILL)
                    if logger:
                        logger.warning(f"Forced killpg(pgid={pgid}) for pid={pid} reason={reason!r}")
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
    ) -> BackendResponse:
        """Generate response from the backend engine."""
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
