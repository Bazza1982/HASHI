"""Qualified cross-interpreter launch with a private versioned JSON channel.

No process-global spawn executable, product imports, localhost listeners, or
pickled objects. The one-time admission handshake precedes product execution.
"""
from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import multiprocessing
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from orchestrator.function_worker_protocol import (
    MAX_FRAME_BYTES, decode_envelope, encode_envelope,
)
from orchestrator.kernel_artifact import verify_artifact
from orchestrator.runtime_contract import (
    CORE_HOST_ROLE, FUNCTIONS_ROLE, QualifiedRuntime, RuntimeContractError,
    RuntimeFingerprint, compare_runtime_boundary, compare_runtime_fingerprints,
    bind_process_runtime, current_process_runtime,
    enforce_runtime_contract, load_qualified_runtime, qualified_runtime_from_reference,
)

_START_LOCK = threading.Lock()
_BOOTSTRAP = Path(__file__).resolve().with_name("function_worker_bootstrap.py")


class WindowsProcessJob:
    """Kill only this owned process tree, including after the parent crashes."""

    def __init__(self, pid: int | None = None):
        from ctypes import wintypes
        size_t = ctypes.c_size_t

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD), ("min_working", size_t),
                ("max_working", size_t), ("active_processes", wintypes.DWORD),
                ("affinity", size_t), ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class Limits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
                ("process_memory", size_t), ("job_memory", size_t),
                ("peak_process_memory", size_t), ("peak_job_memory", size_t),
            ]

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        api.SetInformationJobObject.restype = wintypes.BOOL
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        api.AssignProcessToJobObject.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        self.api, self.handle = api, api.CreateJobObjectW(None, None)
        process = None
        try:
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = Limits()
            limits.basic.flags = 0x2000
            if not api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            if pid is not None:
                process = api.OpenProcess(0x0101, False, pid)
                if not process or not api.AssignProcessToJobObject(self.handle, process):
                    raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise
        finally:
            if process:
                api.CloseHandle(process)

    def include(self, pid):
        from ctypes import wintypes
        self.api.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        self.api.IsProcessInJob.restype = wintypes.BOOL
        handle = self.api.OpenProcess(0x1501, False, pid)
        if not handle:
            raise RuntimeContractError("Cannot own the admitted child")
        try:
            member = wintypes.BOOL()
            if not self.api.IsProcessInJob(handle, self.handle, ctypes.byref(member)):
                raise RuntimeContractError("Cannot observe admitted child Job membership")
            if not member.value:
                raise RuntimeContractError("Admitted child escaped its owned Job")
        finally:
            self.api.CloseHandle(handle)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None

    def __del__(self):
        if getattr(self, "handle", None):
            self.close()


class _WindowsProcessHandle:
    def __init__(self, api, value):
        self.api, self.value = api, value

    def __int__(self):
        return int(self.value)

    def Close(self):
        if self.value is not None:
            self.api.CloseHandle(self.value)
            self.value = None

    def __del__(self):
        if getattr(self, "value", None) is not None:
            self.Close()


class _WindowsProcess:
    """Small Popen-compatible owner for an atomically job-assigned process."""

    def __init__(self, api, handle, pid, args):
        self.api = api
        self._handle = _WindowsProcessHandle(api, handle)
        self.pid, self.args, self.returncode = int(pid), args, None

    def poll(self):
        from ctypes import wintypes
        if self.returncode is None:
            status = self.api.WaitForSingleObject(int(self._handle), 0)
            if status == 258:
                return None
            if status != 0:
                raise ctypes.WinError(ctypes.get_last_error())
            code = wintypes.DWORD()
            if not self.api.GetExitCodeProcess(int(self._handle), ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.returncode = int(code.value)
        return self.returncode

    def wait(self, timeout=None):
        if self.poll() is None:
            milliseconds = 0xffffffff if timeout is None else min(
                0xfffffffe, max(0, int(timeout * 1000)))
            result = self.api.WaitForSingleObject(int(self._handle), milliseconds)
            if result == 258:
                raise subprocess.TimeoutExpired(self.args, timeout)
            if result != 0:
                raise ctypes.WinError(ctypes.get_last_error())
        return self.poll()

    def kill(self):
        if self.poll() is None and not self.api.TerminateProcess(int(self._handle), 1):
            if self.poll() is None:
                raise ctypes.WinError(ctypes.get_last_error())


def _start_windows_process(command, *, environment, cwd, channel_handle, job):
    """Create with an explicit handle list and Job assignment in the same syscall.

    Assigning after Popen races the venv redirector. Suspending until a later
    assignment can strand a child if the parent dies in between. JOB_LIST binds
    ownership atomically, without running a debugger or requesting privileges.
    """
    from ctypes import wintypes

    class StartupInfo(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("reserved", wintypes.LPWSTR),
            ("desktop", wintypes.LPWSTR), ("title", wintypes.LPWSTR),
            ("x", wintypes.DWORD), ("y", wintypes.DWORD),
            ("x_size", wintypes.DWORD), ("y_size", wintypes.DWORD),
            ("x_chars", wintypes.DWORD), ("y_chars", wintypes.DWORD),
            ("fill", wintypes.DWORD), ("flags", wintypes.DWORD),
            ("show", wintypes.WORD), ("reserved_size", wintypes.WORD),
            ("reserved_bytes", ctypes.POINTER(ctypes.c_byte)),
            ("stdin", wintypes.HANDLE), ("stdout", wintypes.HANDLE),
            ("stderr", wintypes.HANDLE),
        ]

    class ExtendedStartup(ctypes.Structure):
        _fields_ = [("startup", StartupInfo), ("attributes", ctypes.c_void_p)]

    class ProcessInformation(ctypes.Structure):
        _fields_ = [("process", wintypes.HANDLE), ("thread", wintypes.HANDLE),
                    ("pid", wintypes.DWORD), ("tid", wintypes.DWORD)]

    api = job.api
    api.GetCurrentProcess.restype = wintypes.HANDLE
    api.GetStdHandle.argtypes = [wintypes.DWORD]
    api.GetStdHandle.restype = wintypes.HANDLE
    api.DuplicateHandle.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.DuplicateHandle.restype = wintypes.BOOL
    api.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)]
    api.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    api.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
    api.UpdateProcThreadAttribute.restype = wintypes.BOOL
    api.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    api.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.c_void_p, ctypes.POINTER(ProcessInformation)]
    api.CreateProcessW.restype = wintypes.BOOL
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    api.GetExitCodeProcess.restype = wintypes.BOOL
    api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.TerminateProcess.restype = wintypes.BOOL

    temporary = []
    attributes = None
    initialized = False
    process = ProcessInformation()
    succeeded = False

    def duplicate(handle):
        current = api.GetCurrentProcess()
        result = wintypes.HANDLE()
        if not api.DuplicateHandle(current, handle, current, ctypes.byref(result), 0, True, 2):
            raise ctypes.WinError(ctypes.get_last_error())
        temporary.append(result.value)
        return result.value

    def null_handle(access):
        handle = api.CreateFileW("NUL", access, 3, None, 3, 0x80, None)
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return duplicate(handle)
        finally:
            api.CloseHandle(handle)

    def standard_handle(number):
        handle = api.GetStdHandle(number & 0xffffffff)
        if not handle or handle == ctypes.c_void_p(-1).value:
            return null_handle(0x40000000)
        return duplicate(handle)

    try:
        stdin = null_handle(0x80000000)
        stdout, stderr = standard_handle(-11), standard_handle(-12)
        # Like Popen with redirected stdin, inherit only the private channel
        # and explicitly duplicated standard I/O. Never inherit the Job handle.
        inherited = (wintypes.HANDLE * 4)(channel_handle, stdin, stdout, stderr)
        jobs = (wintypes.HANDLE * 1)(job.handle)
        size = ctypes.c_size_t()
        api.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = ctypes.create_string_buffer(size.value)
        if not api.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        initialized = True
        for key, value in ((0x00020002, inherited), (0x0002000d, jobs)):
            if not api.UpdateProcThreadAttribute(
                    attributes, 0, key, ctypes.cast(value, ctypes.c_void_p),
                    ctypes.sizeof(value), None, None):
                raise ctypes.WinError(ctypes.get_last_error())
        startup = ExtendedStartup()
        startup.startup.cb = ctypes.sizeof(startup)
        startup.startup.flags = 0x100
        startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = stdin, stdout, stderr
        startup.attributes = ctypes.cast(attributes, ctypes.c_void_p)
        text = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
        block = ctypes.create_unicode_buffer(
            "\0".join(f"{key}={value}" for key, value in sorted(
                environment.items(), key=lambda item: item[0].upper())) + "\0\0")
        if not api.CreateProcessW(
                command[0], text, None, None, True, 0x00080000 | 0x00000400,
                block, str(cwd), ctypes.byref(startup), ctypes.byref(process)):
            raise ctypes.WinError(ctypes.get_last_error())
        created = _WindowsProcess(api, process.process, process.pid, command)
        succeeded = True
        return created
    finally:
        if process.thread:
            api.CloseHandle(process.thread)
        if not succeeded and process.process:
            api.TerminateProcess(process.process, 1)
            api.CloseHandle(process.process)
        if initialized:
            api.DeleteProcThreadAttributeList(attributes)
        for handle in temporary:
            api.CloseHandle(handle)


def _windows_child_identity(pid: int) -> tuple[int, Path]:
    """Read actual OS parent and image, never trust a worker's PID assertion."""
    from ctypes import wintypes

    class BasicInformation(ctypes.Structure):
        _fields_ = [
            ("exit_status", wintypes.LONG), ("peb", ctypes.c_void_p),
            ("affinity", ctypes.c_size_t), ("priority", wintypes.LONG),
            ("pid", ctypes.c_size_t), ("parent", ctypes.c_size_t),
        ]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.QueryFullProcessImageNameW.restype = wintypes.BOOL
    nt = ctypes.WinDLL("ntdll")
    nt.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p]
    nt.NtQueryInformationProcess.restype = wintypes.LONG
    handle = api.OpenProcess(0x1400, False, pid)
    if not handle:
        raise RuntimeContractError("Cannot observe the child OS identity")
    try:
        basic = BasicInformation()
        if nt.NtQueryInformationProcess(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic), None) != 0:
            raise RuntimeContractError("Cannot observe the child OS parent")
        size = wintypes.DWORD(32768)
        image = ctypes.create_unicode_buffer(size.value)
        if not api.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
            raise RuntimeContractError("Cannot observe the child executable")
        return int(basic.parent), Path(image.value).resolve()
    finally:
        api.CloseHandle(handle)


class RuntimeProcess:
    """The process interface used by existing lifecycle and recovery owners."""

    def __init__(self, process: subprocess.Popen | _WindowsProcess):
        self._process = process
        self.pid = process.pid
        self.job = None
        self.admission = None

    @property
    def exitcode(self):
        return self._process.poll()

    def is_alive(self):
        return self._process.poll() is None

    def join(self, timeout=None):
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return
        self._close_job()

    def _close_job(self):
        if self.job is not None:
            self.job.close()
            self.job = None

    def terminate(self):
        self.kill()

    def kill(self):
        if os.name == "nt":
            owned = self.job is not None
            self._close_job()
            if owned:
                # Job termination is asynchronous. Observe its completion
                # before requesting another termination on a dying process.
                self.join(5)
            if self.is_alive():
                self._process.kill()
        else:
            try:
                os.killpg(self._process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def close(self):
        if self.is_alive():
            self.kill()
        self.join(5)
        self._close_job()
        if os.name == "nt" and self._process.returncode is not None:
            handle = getattr(self._process, "_handle", None)
            if handle is not None:
                handle.Close()


def _selected_runtime(code_root: Path, bootstrap: dict, selected: QualifiedRuntime | dict | None):
    if any(key in bootstrap for key in ("executable", "runtime_path", "functions_python", "runtime_manifest")):
        raise RuntimeContractError("Untrusted executable override in Function bootstrap")
    if selected is None:
        own = current_process_runtime()
        if own is not None and own.fingerprint.role == FUNCTIONS_ROLE:
            selected = own
    expected = RuntimeFingerprint.from_mapping(bootstrap["runtime"])
    if expected.role != FUNCTIONS_ROLE:
        raise RuntimeContractError("Function process requires a Functions runtime")
    if selected is None:
        # Legacy Function-to-Function launch stays exact during staged migration.
        # A minimal Core host cannot silently become a Functions environment.
        observed = enforce_runtime_contract(code_root, role=FUNCTIONS_ROLE)
        compare_runtime_fingerprints(expected, observed)
        return expected, None
    if isinstance(selected, dict):
        qualified = qualified_runtime_from_reference(selected, code_root=code_root)
    else:
        qualified = load_qualified_runtime(
            selected.manifest_path, expected_digest=selected.manifest_digest,
            code_root=code_root, role=FUNCTIONS_ROLE,
        )
        compare_runtime_fingerprints(selected.fingerprint, qualified.fingerprint)
    compare_runtime_fingerprints(expected, qualified.fingerprint)
    if bootstrap.get("core_runtime"):
        core = RuntimeFingerprint.from_mapping(bootstrap["core_runtime"])
        if core.role != CORE_HOST_ROLE:
            raise RuntimeContractError("Invalid Core host boundary role")
        compare_runtime_boundary(core, expected, expected_functions=qualified.fingerprint)
    return qualified.fingerprint, qualified


def _validate_admission(process, result, nonce, generation, expected, qualified):
    if not isinstance(result, dict) or set(result) != {"nonce", "pid", "runtime", "generation_id"}:
        raise RuntimeContractError("Invalid process admission receipt")
    if result["nonce"] != nonce or result["generation_id"] != generation:
        raise RuntimeContractError("Process admission nonce or generation mismatch")
    if type(result["pid"]) is not int or result["pid"] <= 0:
        raise RuntimeContractError("Invalid process admission PID")
    compare_runtime_fingerprints(expected, RuntimeFingerprint.from_mapping(result["runtime"]))
    observed_pid = result["pid"]
    if os.name == "nt":
        parent_pid, image = _windows_child_identity(observed_pid)
        if qualified:
            record = json.loads(qualified.manifest_path.read_bytes())
            base_image = Path(record["base_root"]) / "python.exe"
        else:
            base_image = Path(getattr(sys, "_base_executable", sys.executable))
        base_image = base_image.resolve()
        selected_image = Path(expected.executable).resolve()
        allowed_images = {base_image}
        # A venv redirector is not the interpreter. A receipt naming its PID
        # must never authorize product execution. Direct copied CPython images
        # are admitted only when their bytes match the sealed base executable.
        if selected_image == base_image or (
                hashlib.sha256(selected_image.read_bytes()).digest()
                == hashlib.sha256(base_image.read_bytes()).digest()):
            allowed_images.add(selected_image)
        if image not in allowed_images:
            raise RuntimeContractError("Child OS executable does not match the selected runtime")
        if observed_pid != process._process.pid and parent_pid != process._process.pid:
            raise RuntimeContractError("Child OS parent does not match the launched process")
    elif observed_pid != process._process.pid:
        raise RuntimeContractError("Child OS PID does not match the launched process")
    if not process.is_alive():
        raise RuntimeContractError("Admitted child already exited")
    if os.name == "nt" and process.job is not None:
        process.job.include(observed_pid)
    process.pid = observed_pid
    process.admission = dict(result)


def _request_generation(bootstrap, purpose):
    if purpose == "generation":
        verify_artifact(Path(bootstrap["generation_root"]), bootstrap["manifest"])
        module, separator, _ = str(bootstrap["entrypoint"]).partition(":")
        if not separator or module not in {e["module"] for e in bootstrap["manifest"]["entries"]}:
            raise ValueError("entrypoint is outside the qualified generation")
        return bootstrap["manifest"]["generation_id"]
    if purpose not in {"qualification", "candidate-probe"}:
        raise RuntimeContractError("Unknown protected process purpose")
    return "request:sha256:" + hashlib.sha256(
        json.dumps(bootstrap, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def launch_function_process(bootstrap: dict, *, runtime: QualifiedRuntime | dict | None = None,
                            timeout: float = 30.0, cancel_event=None, purpose="generation"):
    """Launch the exact selected FR, then authenticate it before product imports."""
    code_root = Path(bootstrap["code_root"]).resolve()
    if code_root != _BOOTSTRAP.parents[1]:
        raise RuntimeContractError("Bootstrap code root does not match the protected entry")
    expected, qualified = _selected_runtime(code_root, bootstrap, runtime)
    generation = _request_generation(bootstrap, purpose)
    nonce = secrets.token_hex(32)
    params = {"nonce": nonce, "bootstrap": bootstrap, "purpose": purpose,
              "installation": ({"path": str(qualified.manifest_path), "digest": qualified.manifest_digest}
                               if qualified else None)}
    payload = encode_envelope({"version": 1, "kind": "request", "id": nonce,
                               "method": "process.bootstrap", "params": params})
    parent, child = multiprocessing.get_context("spawn").Pipe(duplex=True)
    process = None
    owned_job = None
    try:
        command = [str(Path(expected.executable)), "-I", "-B", str(_BOOTSTRAP), "--ipc-handle", str(child.fileno())]
        options = {"cwd": str(code_root), "close_fds": True, "stdin": subprocess.DEVNULL}
        environment = dict(os.environ)
        for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONUSERBASE",
                    "__PYVENV_LAUNCHER__", "LD_PRELOAD", "LD_LIBRARY_PATH",
                    "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH"):
            environment.pop(key, None)
        options["env"] = environment
        if os.name == "nt":
            owned_job = WindowsProcessJob()
            with _START_LOCK:
                os.set_handle_inheritable(child.fileno(), True)
                try:
                    native = _start_windows_process(
                        command, environment=environment, cwd=code_root,
                        channel_handle=child.fileno(), job=owned_job)
                    process = RuntimeProcess(native)
                    process.job = owned_job
                finally:
                    os.set_handle_inheritable(child.fileno(), False)
        else:
            process = RuntimeProcess(subprocess.Popen(
                command, pass_fds=(child.fileno(),), start_new_session=True, **options))
        child.close()
        parent.send_bytes(payload)
        deadline = time.monotonic() + timeout
        while not parent.poll(0.05):
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeContractError("Function process launch cancelled")
            if not process.is_alive():
                raise RuntimeContractError("Function process exited before admission")
            if time.monotonic() >= deadline:
                raise TimeoutError("Function process admission timed out")
        receipt = decode_envelope(parent.recv_bytes(MAX_FRAME_BYTES))
        if receipt.get("kind") != "response" or receipt.get("id") != nonce or not receipt.get("ok"):
            raise RuntimeContractError("Function process rejected private admission")
        _validate_admission(process, receipt.get("result"), nonce, generation, expected, qualified)
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeContractError("Function process launch cancelled")
        # Child cannot run product code until the parent has verified OS identity.
        parent.send_bytes(encode_envelope({"version": 1, "kind": "request", "id": nonce,
                                          "method": "process.accept", "params": {}}))
        return process, parent
    except BaseException:
        parent.close()
        if process is not None:
            process.close()
        if owned_job is not None:
            owned_job.close()
        raise
    finally:
        child.close()


async def _async_launch(function, *args, **kwargs):
    cancellation = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(
        function, *args, cancel_event=cancellation, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancellation.set()
        try:
            result = await asyncio.shield(task)
        except Exception:
            pass
        else:
            if function is launch_function_process:
                result[1].close()
                await asyncio.to_thread(result[0].close)
        raise


async def launch_function_process_async(bootstrap: dict, **kwargs):
    return await _async_launch(launch_function_process, bootstrap, **kwargs)


def run_protected_call(bootstrap: dict, *, purpose: str, runtime=None,
                       timeout=240.0, cancel_event=None):
    process, connection = launch_function_process(
        bootstrap, purpose=purpose, runtime=runtime,
        timeout=min(timeout, 30), cancel_event=cancel_event)
    try:
        deadline = time.monotonic() + timeout
        while not connection.poll(.05):
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeContractError("Protected call cancelled")
            if not process.is_alive():
                raise RuntimeContractError("Protected call exited without a result")
            if time.monotonic() >= deadline:
                raise TimeoutError("Protected call timed out")
        result = decode_envelope(connection.recv_bytes(MAX_FRAME_BYTES))
        if result.get("kind") != "response" or result.get("id") != process.admission["nonce"]:
            raise RuntimeContractError("Protected call response identity mismatch")
        if not result.get("ok"):
            raise RuntimeContractError(str(result.get("error")))
        return result["result"]
    finally:
        connection.close()
        process.join(5)
        process.close()


async def run_protected_call_async(bootstrap: dict, **kwargs):
    return await _async_launch(run_protected_call, bootstrap, **kwargs)


def receive_private_bootstrap(handle: int):
    """Fixed child entry: channel admission before any Function import."""
    if os.name == "nt":
        from multiprocessing.connection import PipeConnection
        connection = PipeConnection(handle)
        os.set_handle_inheritable(handle, False)
    else:
        from multiprocessing.connection import Connection
        connection = Connection(handle)
        os.set_inheritable(handle, False)
    try:
        if not connection.poll(30):
            raise TimeoutError("Missing private bootstrap")
        request = decode_envelope(connection.recv_bytes(MAX_FRAME_BYTES))
        if request["kind"] != "request" or request.get("method") != "process.bootstrap":
            raise RuntimeContractError("Invalid private bootstrap")
        params = request["params"]
        if set(params) != {"nonce", "bootstrap", "installation", "purpose"}:
            raise RuntimeContractError("Invalid private bootstrap fields")
        nonce = params["nonce"]
        if not isinstance(nonce, str) or len(nonce) != 64 or request["id"] != nonce:
            raise RuntimeContractError("Invalid private bootstrap nonce")
        bootstrap = params["bootstrap"]
        code_root = Path(bootstrap["code_root"]).resolve()
        if code_root != _BOOTSTRAP.parents[1]:
            raise RuntimeContractError("Private bootstrap root mismatch")
        observed = enforce_runtime_contract(code_root, role=FUNCTIONS_ROLE)
        installation = params["installation"]
        if installation is not None:
            qualified = load_qualified_runtime(
                Path(installation["path"]), expected_digest=installation["digest"],
                code_root=code_root, role=FUNCTIONS_ROLE)
            observed = replace(observed, installation_digest=qualified.fingerprint.installation_digest)
            compare_runtime_fingerprints(qualified.fingerprint, observed)
            bind_process_runtime(qualified, code_root=code_root)
        compare_runtime_fingerprints(RuntimeFingerprint.from_mapping(bootstrap["runtime"]), observed)
        generation = _request_generation(bootstrap, params["purpose"])
        result = {"nonce": nonce, "pid": os.getpid(), "runtime": observed.to_dict(),
                  "generation_id": generation}
        connection.send_bytes(encode_envelope({"version": 1, "kind": "response", "id": nonce,
                                              "ok": True, "result": result}))
        if not connection.poll(30):
            raise TimeoutError("Parent did not accept private admission")
        acceptance = decode_envelope(connection.recv_bytes(MAX_FRAME_BYTES))
        if acceptance != {"version": 1, "kind": "request", "id": nonce,
                          "method": "process.accept", "params": {}}:
            raise RuntimeContractError("Parent rejected private admission")
        return connection, bootstrap, params["purpose"], nonce
    except BaseException:
        connection.close()
        raise
# HASHI-Fingerprint: hfp_6bc2831d077f4ab190f30f72f8be1a26
