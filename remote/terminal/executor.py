"""
Terminal executor for Hashi Remote.

Allows remote HASHI instances to execute shell commands on this machine.
Authorization levels mirror Lily Remote's design:

  L0 — health check, read-only queries       → auto-allowed
  L1 — read files, list processes            → auto-allowed
  L2 — write files, short-lived commands     → requires auth token
  L3 — start/restart HASHI processes         → requires human approval
  L4 — shutdown / reboot machine            → strictly disabled (safety)

In LAN mode, L0-L2 are auto-allowed; L3 requires explicit approval.
L4 is always disabled regardless of mode.
"""

import asyncio
import logging
import shlex
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

logger = logging.getLogger(__name__)


class AuthLevel(IntEnum):
    L0_READ_ONLY = 0      # health, status, list
    L1_READ_FILES = 1     # cat, grep, ls, ps
    L2_WRITE = 2          # touch, mkdir, file push, short-lived commands
    L3_RESTART = 3        # kill, systemctl restart, HASHI start/restart
    L4_SYSTEM = 4         # shutdown, reboot (DISABLED)


# Commands classified by required auth level
_L0_PATTERNS = {"echo", "date", "hostname", "uname", "whoami", "pwd", "uptime"}
_L1_PATTERNS = {"cat", "ls", "find", "grep", "ps", "df", "free", "head", "tail", "wc"}
_L3_PATTERNS = {"kill", "pkill", "systemctl", "service", "reboot", "shutdown", "halt", "poweroff"}
_L4_BLOCKED = {"reboot", "shutdown", "halt", "poweroff", "init 0", "init 6"}


def _classify_command(command: str) -> AuthLevel:
    """Determine the minimum auth level required for a command."""
    try:
        parts = shlex.split(command.strip())
    except ValueError:
        return AuthLevel.L3_RESTART  # Unparseable = treat as high risk

    if not parts:
        return AuthLevel.L0_READ_ONLY

    cmd = parts[0].lower().split("/")[-1]  # basename only

    if cmd in _L4_BLOCKED or any(b in command.lower() for b in _L4_BLOCKED):
        return AuthLevel.L4_SYSTEM

    if cmd in _L3_PATTERNS:
        return AuthLevel.L3_RESTART

    if cmd in _L1_PATTERNS:
        return AuthLevel.L1_READ_FILES

    if cmd in _L0_PATTERNS:
        return AuthLevel.L0_READ_ONLY

    return AuthLevel.L2_WRITE  # Default: unknown commands need write auth


@dataclass
class ExecResult:
    command: str
    stdout: str
    stderr: str
    returncode: int
    duration_ms: float
    auth_level: AuthLevel
    allowed: bool
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.allowed and self.returncode == 0

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "returncode": self.returncode,
            "duration_ms": round(self.duration_ms, 2),
            "auth_level": self.auth_level.name,
            "allowed": self.allowed,
            "success": self.success,
            "error": self.error,
        }


class TerminalExecutor:
    """
    Executes shell commands on behalf of a remote HASHI peer.

    Authorization is enforced based on the command's risk level and
    the current mode (LAN mode = L0-L2 auto-allowed).
    """

    def __init__(self, lan_mode: bool = True, max_allowed_level: AuthLevel = AuthLevel.L2_WRITE):
        self._lan_mode = lan_mode
        self._max_allowed = max_allowed_level
        self._timeout = 30  # seconds

    def set_max_level(self, level: AuthLevel) -> None:
        self._max_allowed = level

    def allows_level(self, level: AuthLevel) -> bool:
        """Return whether this executor is configured to allow a risk level."""
        return level != AuthLevel.L4_SYSTEM and level <= self._max_allowed

    def is_allowed(self, command: str) -> tuple[bool, AuthLevel]:
        """Check if a command is allowed. Returns (allowed, required_level)."""
        level = _classify_command(command)
        if level == AuthLevel.L4_SYSTEM:
            return False, level
        allowed = level <= self._max_allowed
        return allowed, level

    async def execute(self, command: str, cwd: Optional[str] = None) -> ExecResult:
        """Execute a shell command and return the result."""
        allowed, auth_level = self.is_allowed(command)

        if not allowed:
            return ExecResult(
                command=command,
                stdout="",
                stderr="",
                returncode=-1,
                duration_ms=0.0,
                auth_level=auth_level,
                allowed=False,
                error=f"Command requires {auth_level.name} authorization, which is not permitted",
            )

        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self._timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return ExecResult(
                    command=command,
                    stdout="",
                    stderr="",
                    returncode=-1,
                    duration_ms=(time.monotonic() - start) * 1000,
                    auth_level=auth_level,
                    allowed=True,
                    error=f"Command timed out after {self._timeout}s",
                )

            return ExecResult(
                command=command,
                stdout=stdout.decode(errors="replace"),
                stderr=stderr.decode(errors="replace"),
                returncode=proc.returncode,
                duration_ms=(time.monotonic() - start) * 1000,
                auth_level=auth_level,
                allowed=True,
            )

        except Exception as e:
            logger.error("Terminal execute error: %s", e)
            return ExecResult(
                command=command,
                stdout="",
                stderr="",
                returncode=-1,
                duration_ms=(time.monotonic() - start) * 1000,
                auth_level=auth_level,
                allowed=True,
                error=str(e),
            )
