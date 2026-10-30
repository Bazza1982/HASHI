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


def runtime_access(process, paths):
    """Inspect the actual suspended token against required runtime DACLs."""
    import ctypes
    from ctypes import wintypes

    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer, handle = wintypes.LPVOID, wintypes.HANDLE
    class Mapping(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in ("read", "write", "execute", "all")]
    security.OpenProcessToken.argtypes = [handle, wintypes.DWORD, ctypes.POINTER(handle)]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.DuplicateToken.argtypes = [handle, ctypes.c_int, ctypes.POINTER(handle)]
    security.DuplicateToken.restype = wintypes.BOOL
    security.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        pointer, pointer, pointer, pointer, ctypes.POINTER(pointer)]
    security.GetNamedSecurityInfoW.restype = wintypes.DWORD
    security.AccessCheck.argtypes = [pointer, handle, wintypes.DWORD, ctypes.POINTER(Mapping),
        pointer, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.BOOL)]
    security.AccessCheck.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [handle], wintypes.BOOL
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [pointer], pointer
    token, impersonation = handle(), handle()
    if not security.OpenProcessToken(process.process_handle, 0x0008 | 0x0002, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not security.DuplicateToken(token, 2, ctypes.byref(impersonation)):
            raise ctypes.WinError(ctypes.get_last_error())
        result = {}
        for path in paths:
            descriptor = pointer()
            error = security.GetNamedSecurityInfoW(str(path), 1, 7, None, None, None, None, ctypes.byref(descriptor))
            if error:
                result[path.name] = {"descriptor_error": error}
                continue
            try:
                privileges = ctypes.create_string_buffer(4096)
                length, granted, allowed = wintypes.DWORD(4096), wintypes.DWORD(), wintypes.BOOL()
                mapping = Mapping(0x120089, 0x120116, 0x1200a0, 0x1f01ff)
                if not security.AccessCheck(descriptor, impersonation, 0x1200a9, ctypes.byref(mapping),
                    privileges, ctypes.byref(length), ctypes.byref(granted), ctypes.byref(allowed)):
                    raise ctypes.WinError(ctypes.get_last_error())
                result[path.name] = {"read_execute": bool(allowed.value), "granted": int(granted.value)}
            finally:
                kernel.LocalFree(descriptor)
        return result
    finally:
        if impersonation and not kernel.CloseHandle(impersonation):
            raise OSError("diagnostic token close failed")
        if not kernel.CloseHandle(token):
            raise OSError("diagnostic original token close failed")


def probe(expected_sid):
    try:
        return _probe(expected_sid)
    except Exception as error:
        import traceback

        frames = [{"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
                  for frame in traceback.extract_tb(error.__traceback__)[-16:]]
        raise RuntimeError(json.dumps({"type": type(error).__name__, "error": str(error)[:1200],
                                       "frames": frames})) from error


def _probe(expected_sid):
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
    def progress(step):
        with (parent_root / "native-progress.json").open("a", encoding="utf-8") as output:
            output.write(json.dumps({"step": step}) + "\n")

    progress("profile")
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
            + "print('before-network',file=sys.stderr,flush=True)\nnetwork=[]\n"
            + "for family,target in ((socket.AF_INET,('192.0.2.1',9)),(socket.AF_INET6,('2001:db8::1',9))):\n"
            + " for kind in (socket.SOCK_STREAM,socket.SOCK_DGRAM):\n"
            + "  blocked=False\n  try:\n   with socket.socket(family,kind) as connection:\n    connection.settimeout(.5)\n"
            + "    if kind==socket.SOCK_STREAM:\n     blocked=connection.connect_ex(target)==10013\n"
            + "    else:\n     connection.sendto(b'fixed-probe',target)\n"
            + "  except OSError as error:\n   blocked=getattr(error,'winerror',None)==10013\n  network.append(blocked)\n"
            + "flags['network_blocked']=all(network)\nflags['network_variants']=len(network)\nprint('network-done',file=sys.stderr,flush=True)\n"
            + "value=Path('inputs/input.txt').read_text()\n"
            + "Path('outputs/result.txt').write_text(value.upper())\n"
            + "print(json.dumps(flags,sort_keys=True))\n"
            + "assert all(flags.get(name) is True for name in ('read_blocked','write_blocked','child_blocked','network_blocked'))\n"
        )
        (task / "task.py").write_text(source, encoding="utf-8")
        root, helper = WindowsJob(), WindowsJob(max_processes=2)
        memory = 128 * 1024 * 1024
        progress("create-restricted-child")
        process = launch_suspended(
            executable=runtime, arguments=["-I", "-S", "-B", str(entry), str(task)], cwd=task,
            environment={"SystemRoot": os.environ["SystemRoot"], "WINDIR": os.environ["SystemRoot"],
                         "PATH": str(runtime.parent) + os.pathsep + str(Path(os.environ["SystemRoot"]) / "System32"),
                         "TMP": str(task), "TEMP": str(task)},
            expected_user_sid=expected_sid, executable_digest=sha256(runtime.read_bytes()).hexdigest(),
            appcontainer_sid=profile.sid, require_lpac=True, python_runtime=True, max_processes=1, memory_bytes=memory,
            parent_jobs=(int(root.handle), int(helper.handle)),
        )
        required = [runtime, runtime.parent / f"python{sys.version_info.major}{sys.version_info.minor}.dll",
                    *runtime.parent.glob("vcruntime*.dll"),
                    *[Path(os.environ["SystemRoot"]) / "System32" / name
                      for name in ("ntdll.dll", "kernel32.dll", "KernelBase.dll", "ucrtbase.dll")]]
        access = runtime_access(process, required)
        progress("resume-restricted-child")
        process.start()
        progress("wait-restricted-child")
        deadline = time.monotonic() + 10
        while not process.poll():
            if time.monotonic() > deadline:
                progress("timeout-diagnostics")
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
            raise RuntimeError("native script exited before completion: " + json.dumps({"exit_code": exit_code, "runtime_access": access,
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
        progress("cleanup-child")
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
            progress("cleanup-complete")
