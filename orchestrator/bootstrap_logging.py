from __future__ import annotations

import logging
import os
import sys
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from orchestrator.pathing import BridgePaths

C_RESET = "\033[0m"
C_MUTED = "\033[38;5;242m"
C_WARN = "\033[38;5;180m"
C_ERROR = "\033[38;5;203m"
C_OK = "\033[38;5;108m"
C_STOP = "\033[38;5;179m"


class AnimMute(logging.Filter):
    """Suppress console log output during startup/reboot animations."""

    def filter(self, _record: logging.LogRecord) -> bool:
        return False


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
        "Process bootstrap:",
        "Configured ",
        "Universal Orchestrator is online.",
        "Workbench API listening on",
        "API Gateway listening on",
        "API Gateway disabled",
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

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        msg = record.getMessage()
        name = record.name
        for prefix in self._SUPPRESS_INFO_PREFIXES:
            if name.startswith(prefix):
                return any(frag in msg for frag in self._ALLOW_INFO_FRAGMENTS)
        return any(frag in msg for frag in self._ALLOW_INFO_FRAGMENTS)


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
