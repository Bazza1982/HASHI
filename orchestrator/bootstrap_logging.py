from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import threading
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from orchestrator import terminal_console
from orchestrator.pathing import BridgePaths

C_RESET = "\033[0m"
C_MUTED = "\033[38;5;242m"
C_WARN = "\033[38;5;180m"
C_ERROR = "\033[38;5;203m"
C_OK = "\033[38;5;108m"
C_STOP = "\033[38;5;179m"
_console_filter_lock = threading.RLock()
_LOG_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
        "Bearer [REDACTED]",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "[REDACTED_API_KEY]"),
    (
        re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{24,}(?![A-Za-z0-9_-])"),
        "[REDACTED_BOT_TOKEN]",
    ),
    (
        re.compile(
            r"(?i)(\b(?:api[_ -]?key|password|passwd|token|secret|authorization|cookie|"
            r"private[_ -]?key)\s*[:=]\s*)([^\s,;]+)"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)([?&](?:access_token|refresh_token|token|key|secret|signature)=)"
            r"[^&#\s]+"
        ),
        r"\1[REDACTED]",
    ),
)


def redact_log_text(value: object) -> str:
    """Remove credential-shaped text before it reaches any log sink."""
    text = str(value or "")
    for pattern, replacement in _LOG_SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class CredentialRedactingFormatter(logging.Formatter):
    """Format complete diagnostics while withholding embedded credentials."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_log_text(super().format(record))


class AnimMute(logging.Filter):
    """Suppress console log output during startup/reboot animations."""

    def filter(self, _record: logging.LogRecord) -> bool:
        return False


class WorkerLogRelay(logging.Handler):
    """Relay sanitized warnings while files retain sanitized diagnostics."""

    def __init__(self, peer):
        super().__init__(logging.WARNING)
        self.peer = peer
        self.loop = asyncio.get_running_loop()
        self.pending = set()

    def emit(self, record):
        payload = {"name": record.name, "level": record.levelno,
                   "message": redact_log_text(record.getMessage())[:8000],
                   "terminal_safe": bool(getattr(record, "terminal_safe", False)),
                   "terminal_detail": redact_log_text(
                       getattr(record, "terminal_detail", "")
                   )}

        def send():
            task = self.loop.create_task(self.peer.emit("worker.log", payload))
            self.pending.add(task)

            def done(completed):
                self.pending.discard(completed)
                if not completed.cancelled():
                    completed.exception()
            task.add_done_callback(done)

        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(send)

    async def drain(self):
        await asyncio.sleep(0)
        if self.pending:
            await asyncio.gather(*tuple(self.pending), return_exceptions=True)


def setup_worker_logging(bridge_home: Path, peer) -> WorkerLogRelay:
    directory = Path(bridge_home) / "logs" / "function-workers"
    directory.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(directory / f"worker-{os.getpid()}.log", encoding="utf-8")
    with suppress(OSError):
        Path(handler.baseFilename).chmod(0o600)
    handler.setFormatter(
        CredentialRedactingFormatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")
    )
    relay = WorkerLogRelay(peer)
    logging.basicConfig(level=logging.INFO, handlers=[handler, relay], force=True)
    logging.captureWarnings(True)
    return relay


def receive_worker_log(payload: dict) -> None:
    """Apply the shared console's level and animation filters to child records."""
    record = logging.makeLogRecord({
        "name": str(payload.get("name") or "BridgeU.FunctionWorker"),
        "levelno": int(payload.get("level") or logging.WARNING),
        "levelname": logging.getLevelName(int(payload.get("level") or logging.WARNING)),
        "msg": redact_log_text(payload.get("message") or ""),
        "args": (),
        "terminal_safe": bool(payload.get("terminal_safe", False)),
        "terminal_detail": redact_log_text(payload.get("terminal_detail") or ""),
    })
    logging.getLogger(record.name).handle(record)


def configure_console_encoding() -> None:
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"

    if os.name == "nt":
        with suppress(Exception):
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleCP(65001)
            kernel32.SetConsoleOutputCP(65001)

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except Exception:
                pass


class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: C_MUTED,
        logging.INFO: C_MUTED,
        logging.WARNING: C_WARN,
        logging.ERROR: C_ERROR,
        logging.CRITICAL: C_ERROR,
    }

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if "Telegram polling error" in msg and "NetworkError" in msg:
            color = C_MUTED
        else:
            color = self.COLORS.get(record.levelno, C_RESET)
        ts = self.formatTime(record, "%H:%M:%S")
        return f"{color}{ts} [{record.name}] {msg}{C_RESET}"


class ConsoleOutputFilter(logging.Filter):
    """
    Keep the console focused on operator-relevant events only.
    Conversations and external traffic use dedicated colorized print helpers.
    Routine backend/runtime chatter still goes to log files.
    """

    _SUPPRESS_INFO_PREFIXES = (
        "Backend.",
        "BackendMgr.",
        "Runtime.",
        "FlexRuntime.",
        "BridgeU.APIGateway",
        "WhatsApp",
        "telegram",
        "aiohttp.",
        "httpx",
        "httpcore",
    )
    _ALLOW_INFO_FRAGMENTS = (
        "Configured ",
        "Local command extension compatibility:",
        "API Gateway failed to start",
        "Shutdown requested",
        "Shutdown already requested",
        "Shutdown signal received.",
        "Shutting down active agents",
        "Shutting down ",
        "Telegram preflight failed",
        "WhatsApp transport started.",
        "WhatsApp transport stopped.",
        "WhatsApp transport failed to start",
        "WhatsApp shutdown warning",
        "All agents failed to start.",
        "No agents can start",
        "Skipping agent",
        "Will start ",
        "Flex agent '",
        "Restart requested",
        "Hot restart:",
        "Hot restart complete.",
    )
    _DEBUG_INFO_FRAGMENTS = (
        "Process bootstrap:",
        "HASHI is online.",
        "Universal Orchestrator is online.",
        "Startup complete:",
        "Workbench API listening on",
        "API Gateway listening on",
        "API Gateway disabled",
    )
    _CONTENT_RISK_PREFIXES = (
        "Backend.",
        "BackendMgr.",
        "Runtime.",
        "FlexRuntime.",
        "BridgeU.APIGateway",
        "DualBrain.",
        "EphemeralInvoker",
        "Anatta.",
        "HASHI.HERv2",
        "VoiceTranscriber",
        "WhatsApp",
        "telegram",
        "aiohttp.",
        "httpx",
        "httpcore",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        name = record.name
        allowed_lifecycle = any(frag in msg for frag in self._ALLOW_INFO_FRAGMENTS)
        debug_lifecycle = any(
            frag in msg for frag in self._DEBUG_INFO_FRAGMENTS
        )
        level = terminal_console.get_level()

        # Raw is the historical terminal: warning/error records plus the same
        # bounded lifecycle INFO allowlist used before /terminal existed.
        if level == terminal_console.LEVEL_RAW:
            if record.levelno >= logging.WARNING:
                return True
            for prefix in self._SUPPRESS_INFO_PREFIXES:
                if name.startswith(prefix):
                    return allowed_lifecycle or debug_lifecycle
            return allowed_lifecycle or debug_lifecycle

        requested_detail = str(getattr(record, "terminal_detail", "") or "")
        if bool(getattr(record, "terminal_safe", False)):
            if requested_detail == terminal_console.LEVEL_DEBUG:
                return level == terminal_console.LEVEL_DEBUG
            if requested_detail == terminal_console.LEVEL_ACTIVITY:
                return level in {
                    terminal_console.LEVEL_ACTIVITY,
                    terminal_console.LEVEL_DEBUG,
                }
            return True

        # Explicit lifecycle messages are content-free and visible in every
        # production level, including those emitted by runtime loggers.
        if allowed_lifecycle:
            return True
        if debug_lifecycle:
            return level == terminal_console.LEVEL_DEBUG

        # Runtime/provider logs may contain prompts, completions, reasoning,
        # command arguments, or provider error bodies.  They remain complete
        # in file logs but never reach non-raw stdout implicitly.
        if any(name.startswith(prefix) for prefix in self._CONTENT_RISK_PREFIXES):
            return False

        # Preserve operator-visible service/configuration failures.  Request
        # failures have their own sanitised terminal event path.
        if record.levelno >= logging.WARNING:
            return True

        for prefix in self._SUPPRESS_INFO_PREFIXES:
            if name.startswith(prefix):
                return False
        return False


def refresh_console_output_filters() -> int:
    """Replace stale console filters after an in-process code reload.

    The live root handler is owned by ``main.py`` and survives ``/reboot``.
    Replacing only filters with this exact class name preserves temporary
    animation muting and any unrelated handler policy.
    """

    refreshed = 0
    with _console_filter_lock:
        for handler in logging.getLogger().handlers:
            stale = [
                filter_
                for filter_ in list(handler.filters)
                if filter_.__class__.__name__ == "ConsoleOutputFilter"
            ]
            if not stale:
                continue
            for filter_ in stale:
                handler.removeFilter(filter_)
            handler.addFilter(ConsoleOutputFilter())
            refreshed += 1
        return refreshed


def configure_terminal_console(bridge_home: str | Path) -> str:
    """Load the instance-wide terminal level after bridge-home resolution."""

    return terminal_console.configure(bridge_home)


def setup_console_logging() -> logging.StreamHandler:
    configure_console_encoding()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ColorFormatter())
    handler.addFilter(ConsoleOutputFilter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.Updater").setLevel(logging.CRITICAL)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.server").setLevel(logging.WARNING)
    return handler


def setup_bridge_file_logging(global_cfg, bridge_logger: logging.Logger, scheduler_logger: logging.Logger | None = None):
    """Route bridge-level audit logs and scheduler logs to bridge.log."""
    bridge_log_path = global_cfg.base_logs_dir / "bridge.log"
    global_cfg.base_logs_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(bridge_log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    bridge_logger.handlers.clear()
    bridge_logger.setLevel(logging.DEBUG)
    bridge_logger.propagate = False
    bridge_logger.addHandler(handler)

    if scheduler_logger is not None:
        scheduler_logger.handlers.clear()
        scheduler_logger.setLevel(logging.DEBUG)
        scheduler_logger.propagate = False
        scheduler_logger.addHandler(handler)

    return handler


def write_bridge_audit_line(log_path: Path, level: int, message: str) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        level_name = logging.getLevelName(level)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{ts} [BridgeU.Bridge] {level_name:<8} {message}\n")
    except Exception:
        pass


def emit_bridge_audit(
    paths: BridgePaths | None,
    level: int,
    message: str,
    bridge_logger: logging.Logger | None = None,
) -> None:
    if bridge_logger and bridge_logger.handlers:
        bridge_logger.log(level, message)
        return
    if paths is None:
        return
    write_bridge_audit_line(paths.bridge_home / "logs" / "bridge.log", level, message)
