"""Bounded trusted bootstrap for the independent-account Windows platform check.

This starts only fixed test code. It is not an untrusted runner launcher: the
normal secondary-logon API cannot carry the atomic extended Job attributes.
The trusted bootstrap stays suspended until assigned to its owned Job, then
creates its AppContainer children with the normal atomic creation attributes.
No process may be resumed on an identity or Job mismatch.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from hashlib import sha256
from pathlib import Path
import subprocess

from scripts.permissions.windows_probe_process import WindowsProbeProcess, ProbeCleanupUnverified
from scripts.permissions.windows_probe_staging import StageError


class WindowsTrustedProbeBootstrap(WindowsProbeProcess):
    def __init__(
        self, computer_name: str, *, probe_path: Path | None = None,
        probe_sha256: str | None = None, result_path: Path | None = None,
        profile_guard=None,
    ) -> None:
        super().__init__(computer_name)
        if probe_path is not None:
            if (result_path is None or not isinstance(probe_sha256, str)
                    or sha256(Path(probe_path).read_bytes()).hexdigest() != probe_sha256):
                raise StageError("a registered fixed platform probe is required")
        elif probe_sha256 is not None or result_path is not None:
            raise StageError("incomplete fixed probe registration")
        self._probe_path = probe_path
        self._probe_digest = probe_sha256
        self._result_path = result_path
        if profile_guard is not None and probe_path is None:
            raise StageError("profile loading is only allowed for the registered platform probe")
        self._profile_guard = profile_guard
        self.adv.CreateProcessWithLogonW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.c_void_p,
            wintypes.LPCWSTR, ctypes.POINTER(self.probe.STARTUPINFOW),
            ctypes.POINTER(self.probe.PROCESS_INFORMATION),
        ]
        self.adv.CreateProcessWithLogonW.restype = wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL

    def start_suspended(self, identity, executable, scratch, sentinel, nonce, job):
        if self._probe_path is None:
            program = "import sys; f=open(sys.argv[1],'xb'); f.write(bytes.fromhex(sys.argv[2])); f.close()"
            args = [str(sentinel), nonce.hex()]
        else:
            # The entry is registered by the test controller and staged read-only
            # to this identity. It is never an Agent supplied program.
            if sha256(self._probe_path.read_bytes()).hexdigest() != self._probe_digest:
                raise StageError("the registered platform probe changed")
            program = "import sys,runpy,json\nfrom pathlib import Path\nsys.path.insert(0,str(Path(sys.argv[1]).parent))\ntry:\n    p=runpy.run_path(sys.argv[1])\n    result=p['probe'](sys.argv[2])\nexcept BaseException as error:\n    with open(sys.argv[3],'x',encoding='utf-8') as output:\n        json.dump({'error_type':type(error).__name__,'error':str(error)[:2000]},output)\n    raise\nwith open(sys.argv[3],'x',encoding='utf-8') as output:\n    json.dump(result,output)\nwith open(sys.argv[4],'xb') as output:\n    output.write(bytes.fromhex(sys.argv[5]))\n"
            args = [str(self._probe_path), identity.sid, str(self._result_path), str(sentinel), nonce.hex()]
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(
            [str(executable), "-I", "-S", "-c", program, *args],
        ))
        windows = ctypes.create_unicode_buffer(32768)
        count = self.kernel.GetWindowsDirectoryW(windows, len(windows))
        if not count or count >= len(windows):
            raise StageError("cannot determine the Windows directory")
        env = {
            "SystemRoot": windows.value, "WINDIR": windows.value,
            "TEMP": str(scratch), "TMP": str(scratch), "USERPROFILE": str(scratch),
            "APPDATA": str(scratch), "LOCALAPPDATA": str(scratch),
        }
        environment = ctypes.create_unicode_buffer(
            "\0".join(f"{key}={value}" for key, value in sorted(env.items())) + "\0\0",
        )
        startup = self.probe.STARTUPINFOW()
        startup.cb = ctypes.sizeof(startup)
        process = self.probe.PROCESS_INFORMATION()
        if self._profile_guard is not None:
            self._profile_guard.before_logon(identity.sid)
        success = self.adv.CreateProcessWithLogonW(
            identity.name, self.computer_name, identity.password,
            1 if self._profile_guard is not None else 0,
            str(executable), command, 0x4 | 0x400 | 0x08000000,
            environment, str(scratch), ctypes.byref(startup), ctypes.byref(process),
        )
        if not success:
            raise StageError(f"trusted bootstrap creation failed (Windows error {ctypes.get_last_error()})")
        try:
            if not self.kernel.AssignProcessToJobObject(job, process.hProcess):
                raise StageError(f"trusted bootstrap Job assignment failed (Windows error {ctypes.get_last_error()})")
            return process
        except BaseException:
            # This code is still suspended. Only this retained handle may be
            # terminated, never a process selected by name or by a stale PID.
            cleanup_error = None
            try:
                self.terminate(process)
                if not self.stopped(process, 5000):
                    raise StageError("trusted bootstrap did not stop")
            except Exception as exc:
                cleanup_error = exc
            try:
                self.close_process(process)
            except Exception as exc:
                cleanup_error = exc
            if cleanup_error is not None:
                raise ProbeCleanupUnverified("trusted bootstrap cleanup is unverified") from cleanup_error
            raise
