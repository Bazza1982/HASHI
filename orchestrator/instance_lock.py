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

    def __init__(
        self,
        path: Path,
        *,
        pid_path: Path | None = None,
        instance_id: str = "HASHI",
    ):
        self.path = path
        self.pid_path = pid_path or path.with_suffix(".pid")
        self.instance_id = str(instance_id or "HASHI")
        self._fh = None
        self._acquired = False

    def acquire(self):
        fh = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
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
            self._acquired = True
            self._write_pid_file(our_pid)

        except (OSError, IOError) as exc:
            with suppress(Exception):
                if fh:
                    fh.close()
            pid = self._read_pid_file()
            hint = f"Run: taskkill /PID {pid} /T /F" if sys.platform == "win32" else f"Run: kill {pid}"
            raise RuntimeError(
                f"HASHI instance {self.instance_id!r} is already running (PID {pid}). "
                "Other HASHI instances are not affected. "
                f"Shut down only this instance first. Hint: {hint}"
            ) from exc

    def _write_pid_file(self, pid_str: str):
        try:
            self.pid_path.parent.mkdir(parents=True, exist_ok=True)
            self.pid_path.write_text(pid_str, encoding="utf-8")
            main_logger.debug("Wrote PID %s to %s", pid_str, self.pid_path)
        except Exception as e:
            main_logger.warning("Failed to write PID file %s: %s", self.pid_path, e)

    def _read_pid_file(self) -> str:
        try:
            return self.pid_path.read_text(encoding="utf-8").strip() or "?"
        except Exception:
            return "?"

    def release(self):
        if not self._acquired:
            return
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
        self._acquired = False
        # Keep the empty lock file in place. Deleting a lock file after unlock
        # creates an inode race where another process can acquire the old file
        # while a third process creates and locks a new one at the same path.
        try:
            recorded_pid = self.pid_path.read_text(encoding="utf-8").strip()
            if recorded_pid == str(os.getpid()):
                self.pid_path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        except Exception as e:
            main_logger.debug("PID file unlink warning (non-fatal): %s", e)
