"""Explicit Windows SCM host for installed permission Safety and Runner.

This is a replaceable platform/Functions entrypoint. Importing it neither
starts nor installs a service. Payloads remain the two fixed installation
readers; no arbitrary executable, module or model-supplied command is accepted.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import re
import threading


STOPPED, START_PENDING, STOP_PENDING, RUNNING = 1, 2, 3, 4
STOP, INTERROGATE, SHUTDOWN = 1, 4, 5
ERROR_SERVICE_SPECIFIC_ERROR = 1066


@dataclass(frozen=True)
class ServiceStatus:
    state: int
    checkpoint: int = 0
    wait_hint_ms: int = 0
    win32_exit_code: int = 0
    service_exit_code: int = 0


class ServiceHost:
    """Bridge SCM callbacks to one asyncio service and its confirmed cleanup."""

    def __init__(self, name, serve, installation, *, api=None, checkpoint_seconds=1):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name):
            raise ValueError("an explicit Windows service name is required")
        if checkpoint_seconds <= 0:
            raise ValueError("checkpoint interval must be positive")
        self.name, self.serve, self.installation = name, serve, installation
        self.api = api
        self.checkpoint_seconds = checkpoint_seconds
        self._lock = threading.RLock()
        self._status = None
        self._loop = self._stopped = None
        self._stop_requested = self._ready = False
        self._failure = None
        self._heartbeat_stop = threading.Event()

    def _publish(self, state, *, failed=False):
        # Callers hold the same lock as control callbacks and readiness.
        prior = self._status
        pending = state in (START_PENDING, STOP_PENDING)
        checkpoint = (prior.checkpoint + 1 if prior and prior.state == state else 1) if pending else 0
        status = ServiceStatus(
            state, checkpoint, 30000 if pending else 0,
            ERROR_SERVICE_SPECIFIC_ERROR if failed else 0, 1 if failed else 0,
        )
        self.api.set_status(status)
        self._status = status

    def _signal_stop(self):
        self._stop_requested = True
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._stopped.set)

    def control(self, kind):
        with self._lock:
            try:
                if kind == INTERROGATE:
                    if self._status is not None:
                        self.api.set_status(self._status)
                    return 0
                if kind not in (STOP, SHUTDOWN):
                    return 120  # ERROR_CALL_NOT_IMPLEMENTED
                if self._status is not None and self._status.state == STOPPED:
                    return 0
                self._signal_stop()
                if self._status is not None and self._status.state != STOP_PENDING:
                    self._publish(STOP_PENDING)
                return 0
            except BaseException as error:
                self._failure = error
                self._signal_stop()
                raise

    def ready(self):
        with self._lock:
            if self._stop_requested:
                return
            if self._status is None or self._status.state != START_PENDING or self._ready:
                raise RuntimeError("service readiness has already changed")
            self._publish(RUNNING)
            self._ready = True

    def _checkpoints(self):
        while not self._heartbeat_stop.wait(self.checkpoint_seconds):
            try:
                with self._lock:
                    if self._status is not None and self._status.state in (START_PENDING, STOP_PENDING):
                        self._publish(self._status.state)
            except BaseException as error:
                with self._lock:
                    self._failure = error
                    self._signal_stop()
                return

    async def _run(self):
        with self._lock:
            self._loop, self._stopped = asyncio.get_running_loop(), asyncio.Event()
            if self._stop_requested:
                self._stopped.set()
        try:
            await self.serve(self.installation, stopped=self._stopped, on_ready=self.ready)
            with self._lock:
                if not self._stop_requested:
                    raise RuntimeError("service exited without a stop request")
        finally:
            with self._lock:
                self._loop = self._stopped = None

    def service_main(self):
        # This is invoked on the SCM callback thread, not Python's main thread.
        heartbeat = None
        try:
            self.api.register(self.name, self.control)
            with self._lock:
                self._publish(START_PENDING)
            heartbeat = threading.Thread(target=self._checkpoints, name="permission-scm-status", daemon=True)
            heartbeat.start()
            asyncio.run(self._run())
        except BaseException as error:
            self._failure = error
        finally:
            self._heartbeat_stop.set()
            if heartbeat is not None:
                heartbeat.join()
            try:
                with self._lock:
                    if self._status is not None:
                        self._publish(STOPPED, failed=self._failure is not None)
            except BaseException as error:
                self._failure = error

    def run(self):
        if self.api is None:
            self.api = WindowsServiceAPI()
        self.api.dispatch(self.name, self.service_main)
        if self._status is None and self._failure is None:
            self._failure = RuntimeError("SCM did not dispatch the service")
        if self._failure is not None:
            # Do not expose private installation paths, keys or payload text.
            raise OSError("permission Windows service failed; inspect SCM exit status") from self._failure


class WindowsServiceAPI:
    """Small ctypes adapter; no PyWin32 or running-Core dependency installation."""

    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows SCM is required")
        import ctypes
        from ctypes import wintypes

        self.ctypes, self.types = ctypes, wintypes
        self.native = ctypes.WinDLL("advapi32", use_last_error=True)
        self.main_type = ctypes.WINFUNCTYPE(None, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))
        self.control_type = ctypes.WINFUNCTYPE(
            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID,
        )

        class NativeStatus(ctypes.Structure):
            _fields_ = [(name, wintypes.DWORD) for name in (
                "service_type", "state", "accepted", "win32_exit_code",
                "service_exit_code", "checkpoint", "wait_hint_ms",
            )]

        class TableEntry(ctypes.Structure):
            _fields_ = [("name", wintypes.LPWSTR), ("main", self.main_type)]

        self.status_type, self.table_type = NativeStatus, TableEntry
        self.native.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(TableEntry)]
        self.native.StartServiceCtrlDispatcherW.restype = wintypes.BOOL
        self.native.RegisterServiceCtrlHandlerExW.argtypes = [
            wintypes.LPCWSTR, self.control_type, wintypes.LPVOID,
        ]
        self.native.RegisterServiceCtrlHandlerExW.restype = wintypes.HANDLE
        self.native.SetServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(NativeStatus)]
        self.native.SetServiceStatus.restype = wintypes.BOOL
        self.handle = None
        self._main_callback = self._control_callback = None

    def dispatch(self, name, main):
        self._main_callback = self.main_type(lambda _count, _argv: main())
        entries = (self.table_type * 2)()
        entries[0] = self.table_type(name, self._main_callback)
        if not self.native.StartServiceCtrlDispatcherW(entries):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def register(self, name, control):
        def handler(kind, _event, _data, _context):
            try:
                return control(kind)
            except BaseException:
                return 1064  # ERROR_EXCEPTION_IN_SERVICE, never leak callback exceptions.

        self._control_callback = self.control_type(handler)
        self.handle = self.native.RegisterServiceCtrlHandlerExW(name, self._control_callback, None)
        if not self.handle:
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def set_status(self, status):
        accepted = 1 | 4 if status.state == RUNNING else 0  # STOP | SHUTDOWN
        native = self.status_type(
            0x10, status.state, accepted, status.win32_exit_code,
            status.service_exit_code, status.checkpoint, status.wait_hint_ms,
        )
        if not self.native.SetServiceStatus(self.handle, self.ctypes.byref(native)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run an explicitly installed permission Windows service")
    parser.add_argument("--service-name", required=True)
    parser.add_argument("--role", required=True, choices=("safety", "runner"))
    parser.add_argument("installation", type=Path)
    arguments = parser.parse_args(argv)
    if not arguments.installation.is_absolute():
        parser.error("an absolute private installation path is required")
    async def installed_serve(path, **lifecycle):
        # Imports/qualification happen after SCM dispatch and START_PENDING,
        # while the independent checkpoint thread is already responsive.
        if arguments.role == "safety":
            from orchestrator.permissions.installation import serve
        else:
            from orchestrator.permissions.runner_installation import serve
        await serve(path, **lifecycle)

    ServiceHost(arguments.service_name, installed_serve, arguments.installation).run()


if __name__ == "__main__":
    main()
