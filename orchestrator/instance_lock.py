from __future__ import annotations

import logging
import os
import sys
from contextlib import suppress
from pathlib import Path

main_logger = logging.getLogger("BridgeU.Orchestrator")


class InstanceLock:
    """
    Single-instance guard using OS-level file locking.

    Uses msvcrt on Windows and fcntl on Unix-like systems.
    The lock is tied to the process file descriptor and auto-released by the OS.
    """

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self):
        fh = None
        try:
            try:
                fh = open(str(self.path), "r+b")
            except FileNotFoundError:
                fh = open(str(self.path), "w+b")

            fh.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            our_pid = str(os.getpid())
            fh.seek(0)
            fh.truncate(0)
            fh.write(our_pid.encode("utf-8"))
            fh.flush()
            self._fh = fh
            self._write_pid_file(our_pid)

        except (OSError, IOError) as exc:
            with suppress(Exception):
                if fh:
                    fh.close()
            pid = self._read_pid_file()
            hint = f"Run: taskkill /PID {pid} /T /F" if sys.platform == "win32" else f"Run: kill {pid}"
            raise RuntimeError(
                f"bridge-u-f is already running (PID {pid}). "
                f"Shut down the existing instance first. Hint: {hint}"
            ) from exc

    def _write_pid_file(self, pid_str: str):
        pid_path = self.path.parent / ".bridge_u_f.pid"
        try:
            pid_path.write_text(pid_str, encoding="utf-8")
            main_logger.debug("Wrote PID %s to %s", pid_str, pid_path)
        except Exception as e:
            main_logger.warning("Failed to write PID file %s: %s", pid_path, e)

    def _read_pid_file(self) -> str:
        pid_path = self.path.parent / ".bridge_u_f.pid"
        try:
            return pid_path.read_text(encoding="utf-8").strip() or "?"
        except Exception:
            return "?"

    def release(self):
        if self._fh is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except Exception as e:
                main_logger.debug("Lock unlock warning (non-fatal): %s", e)
            try:
                self._fh.close()
            except Exception as e:
                main_logger.debug("Lock file close warning (non-fatal): %s", e)
            self._fh = None
        try:
            self.path.unlink(missing_ok=True)
        except Exception as e:
            main_logger.debug("Lock file unlink warning (non-fatal): %s", e)
        pid_path = self.path.parent / ".bridge_u_f.pid"
        try:
            pid_path.unlink(missing_ok=True)
        except Exception as e:
            main_logger.debug("PID file unlink warning (non-fatal): %s", e)
