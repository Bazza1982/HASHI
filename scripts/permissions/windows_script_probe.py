"""Fixed disposable-account probe of the actual script entry and native Job limits."""
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import time

from orchestrator.permissions.windows_container import TaskContainerProfile, protect_task_scratch
from orchestrator.permissions.windows_ipc import current_process_user_sid
from orchestrator.permissions.windows_jobs import WindowsJob
from orchestrator.permissions.windows_process import launch_suspended
from orchestrator.permissions.windows_security import require_nonadmin_identity


def diagnostics(process):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    class Module(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("id", wintypes.DWORD), ("pid", wintypes.DWORD),
                    ("global_usage", wintypes.DWORD), ("process_usage", wintypes.DWORD),
                    ("base", wintypes.LPVOID), ("length", wintypes.DWORD), ("handle", wintypes.HMODULE),
                    ("name", wintypes.WCHAR * 256), ("path", wintypes.WCHAR * 260)]
    kernel.Module32FirstW.argtypes = kernel.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Module)]
    kernel.Module32FirstW.restype = kernel.Module32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
    handle = kernel.CreateToolhelp32Snapshot(0x18, process.observation.process_id)
    modules = []
    if handle not in (None, ctypes.c_void_p(-1).value):
        try:
            value = Module()
            value.size = ctypes.sizeof(value)
            ok = kernel.Module32FirstW(handle, ctypes.byref(value))
            while ok and len(modules) < 64:
                modules.append(value.name)
                ok = kernel.Module32NextW(handle, ctypes.byref(value))
        finally:
            if not kernel.CloseHandle(handle):
                raise OSError("diagnostic snapshot close failed")
    return {"modules": modules, "process": asdict(process.observation)}


def probe(expected_sid):
    observed_identity = require_nonadmin_identity(expected_sid)
    if current_process_user_sid() != expected_sid:
        raise RuntimeError("qualification account changed")
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.SetErrorMode.argtypes, kernel.SetErrorMode.restype = [ctypes.c_uint], ctypes.c_uint
    previous_error_mode = kernel.SetErrorMode(0x8003)
    runtime = Path(sys.executable)
    entry = Path(__file__).parent / "orchestrator/permissions/script_worker.py"
    parent_root = Path(os.environ["TEMP"])
    original = parent_root / "private-canary.txt"
    original.write_bytes(b"original-must-not-change")
    task = parent_root / "script-task"
    task.mkdir()
    profile = TaskContainerProfile()
    root = helper = process = None
    profile_deleted = False
    try:
        protect_task_scratch(task, runner_sid=expected_sid, appcontainer_sid=profile.sid)
        (task / "inputs").mkdir()
        (task / "outputs").mkdir()
        (task / "inputs/input.txt").write_bytes(b"snapshot")
        source = (
            "import sys\nprint('entered',file=sys.stderr,flush=True)\nfrom pathlib import Path\nimport json,socket,subprocess\nprint('imports-ready',file=sys.stderr,flush=True)\n"
            + "private=Path(" + repr(str(original)) + ")\n"
            + "flags={}\n"
            + "try:\n private.read_bytes()\nexcept PermissionError:\n flags['read_blocked']=True\n"
            + "try:\n private.write_bytes(b'forbidden')\nexcept PermissionError:\n flags['write_blocked']=True\n"
            + "print('before-child',file=sys.stderr,flush=True)\ntry:\n subprocess.run([sys.executable,'-I','-S','-c','pass'],check=True,timeout=2)\n"
            + "except OSError as error:\n flags['child_blocked']=getattr(error,'winerror',None) in (5,1816)\n"
            + "print('before-network',file=sys.stderr,flush=True)\nwith socket.socket() as connection:\n connection.settimeout(1)\n flags['network_blocked']=connection.connect_ex(('127.0.0.1',9))==10013\nprint('network-done',file=sys.stderr,flush=True)\n"
            + "value=Path('inputs/input.txt').read_text()\n"
            + "Path('outputs/result.txt').write_text(value.upper())\n"
            + "print(json.dumps(flags,sort_keys=True))\n"
            + "assert all(flags.get(name) is True for name in ('read_blocked','write_blocked','child_blocked','network_blocked'))\n"
        )
        (task / "task.py").write_text(source, encoding="utf-8")
        root, helper = WindowsJob(), WindowsJob(max_processes=2)
        memory = 128 * 1024 * 1024
        process = launch_suspended(
            executable=runtime, arguments=["-I", "-S", "-B", str(entry), str(task)], cwd=task,
            environment={"SystemRoot": os.environ["SystemRoot"], "WINDIR": os.environ["SystemRoot"],
                         "PATH": str(runtime.parent) + os.pathsep + str(Path(os.environ["SystemRoot"]) / "System32"),
                         "TMP": str(task), "TEMP": str(task)},
            expected_user_sid=expected_sid, executable_digest=sha256(runtime.read_bytes()).hexdigest(),
            appcontainer_sid=profile.sid, require_lpac=True, max_processes=1, memory_bytes=memory,
            parent_jobs=(int(root.handle), int(helper.handle)),
        )
        process.start()
        deadline = time.monotonic() + 10
        while not process.poll():
            if time.monotonic() > deadline:
                native = diagnostics(process)
                process.stop(timeout_ms=4500)
                details = {name: ((task / name).read_text(encoding="utf-8", errors="replace")[:2000]
                                  if (task / name).is_file() else "[not created]") for name in ("stdout.txt", "stderr.txt")}
                raise TimeoutError("fixed script did not finish: " + json.dumps({"native": native, "files": details}))
            time.sleep(.02)
        exit_code = process.exit_code()
        observation = asdict(process.observation)
        process.stop(timeout_ms=4500)
        if exit_code != 0:
            raise RuntimeError("native script exited before completion: " + json.dumps({"exit_code": exit_code,
                "stderr": ((task / "stderr.txt").read_text(encoding="utf-8")[:2000] if (task / "stderr.txt").exists() else "[not created]")}))
        flags = json.loads((task / "stdout.txt").read_text(encoding="utf-8"))
        if (exit_code != 0 or original.read_bytes() != b"original-must-not-change"
                or (task / "outputs/result.txt").read_bytes() != b"SNAPSHOT"
                or not all(flags.get(name) is True for name in ("read_blocked", "write_blocked", "child_blocked", "network_blocked"))):
            raise RuntimeError("native script boundary failed: " + json.dumps({"exit_code": exit_code, "flags": flags}))
        root.stop(timeout_ms=4500)
        helper.stop(timeout_ms=4500)
        profile.close()
        profile_deleted = True
        return {"identity": observed_identity, "process": observation, "checks": flags,
                "script_entry_sha256": sha256(entry.read_bytes()).hexdigest(),
                "native_memory_limit_bytes": memory, "temporary_output_verified": True,
                "original_input_unchanged": True, "all_jobs_stopped": True, "profile_deleted": profile_deleted}
    finally:
        if process is not None:
            process.stop(timeout_ms=4500)
        if helper is not None:
            helper.stop(timeout_ms=4500)
        if root is not None:
            root.stop(timeout_ms=4500)
        try:
            profile.close()
        finally:
            kernel.SetErrorMode(previous_error_mode)
