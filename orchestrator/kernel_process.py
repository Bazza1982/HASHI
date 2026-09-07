"""Small process supervisor. No product modules execute in this process."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import signal
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from orchestrator.function_worker_bootstrap import (
    run_generation_process,
    QUALIFICATION_RESULT_PREFIX,
)
from orchestrator.function_worker_protocol import JsonConnectionPeer
from orchestrator.kernel_artifact import verify_artifact
from orchestrator.runtime_contract import (
    RuntimeFingerprint,
    compare_runtime_fingerprints,
    enforce_runtime_contract,
)

RECEIPT_PREFIX = QUALIFICATION_RESULT_PREFIX


def canonical_instance_home(code_root: Path, override=None) -> Path:
    raw = (
        str(override).strip()
        if override is not None
        else os.environ.get("BRIDGE_HOME", "").strip()
    )
    if '"' in raw:
        raw = raw[: raw.index('"')]
    raw = raw.strip().rstrip("'")
    return (
        Path(os.path.expanduser(os.path.expandvars(raw))).resolve()
        if raw
        else Path(code_root).resolve()
    )


def instance_runtime_dir(bridge_home: Path) -> Path:
    return Path(bridge_home) / "state" / "instance"


def write_record(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


async def qualify(
    code_root: Path, bridge_home: Path, runtime: RuntimeFingerprint
) -> dict:
    """Run product qualification out of process; verify its exact receipt here."""
    compare_runtime_fingerprints(runtime, enforce_runtime_contract(code_root))
    profile = json.loads((code_root / "runtime-entry.json").read_text(encoding="utf-8"))
    script = code_root / "orchestrator" / "function_worker_bootstrap.py"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(script),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=code_root,
    )
    payload = {
        "code_root": str(code_root),
        "bridge_home": str(bridge_home),
        "runtime": runtime.to_dict(),
        "entrypoint": profile["entrypoint"],
        "qualifier": profile["qualifier"],
    }
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(json.dumps(payload).encode()),
            240,
        )
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    lines = [
        line[len(RECEIPT_PREFIX) :]
        for line in stdout.decode().splitlines()
        if line.startswith(RECEIPT_PREFIX)
    ]
    if process.returncode or len(lines) != 1:
        raise RuntimeError(
            "Function qualification failed: " + stderr.decode(errors="replace")[-2000:]
        )
    release = json.loads(lines[0])
    compare_runtime_fingerprints(
        runtime, RuntimeFingerprint.from_mapping(release["runtime"])
    )
    compare_runtime_fingerprints(runtime, enforce_runtime_contract(code_root))
    artifact = Path(release["generation_root"]).resolve()
    if not artifact.is_relative_to(bridge_home / "state" / "function_generations"):
        raise ValueError("qualified artifact is outside this instance")
    if release["entrypoint"] != profile["entrypoint"]:
        raise ValueError("qualification entrypoint mismatch")
    verify_artifact(artifact, release["manifest"])
    return release


class _WindowsProcessJob:
    """OS-owned descendant cleanup even if the shared process dies first."""

    def __init__(self, pid: int):
        import ctypes
        from ctypes import wintypes

        size_t = ctypes.c_size_t

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("min_working", size_t),
                ("max_working", size_t),
                ("active_processes", wintypes.DWORD),
                ("affinity", size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class Limits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits),
                ("io", ctypes.c_uint64 * 6),
                ("process_memory", size_t),
                ("job_memory", size_t),
                ("peak_process_memory", size_t),
                ("peak_job_memory", size_t),
            ]

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
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
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not api.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            process = api.OpenProcess(0x0101, False, pid)
            if not process or not api.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise
        finally:
            if process:
                api.CloseHandle(process)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class ProcessClient:
    def __init__(self, process, connection):
        self.process = process
        self.requested_exit = False
        self.owns_group = False
        self.job = None
        self.peer = JsonConnectionPeer(
            connection, label=f"functions:{process.pid}", event_handler=self._event
        )
        self.peer.start()

    async def _event(self, event, payload):
        if event == "application.stopped":
            self.requested_exit = True

    @classmethod
    async def prepare(cls, bootstrap: dict):
        verify_artifact(Path(bootstrap["generation_root"]), bootstrap["manifest"])
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        process = context.Process(
            target=run_generation_process, args=(child, bootstrap), daemon=False
        )
        process.start()
        child.close()
        client = cls(process, parent)
        try:
            if os.name == "nt" and bootstrap.get("process_group"):
                client.job = _WindowsProcessJob(process.pid)
            ready = await client.peer.request("prepare", timeout=240)
            if (
                ready.get("pid") != process.pid
                or ready.get("generation_id") != bootstrap["manifest"]["generation_id"]
            ):
                raise RuntimeError("Function process READY identity mismatch")
            client.ready_metadata = ready
            client.owns_group = bool(bootstrap.get("process_group"))
            return client
        except BaseException:
            await client.close()
            raise

    async def close(self):
        if self.process.is_alive() and not self.peer.is_closed:
            try:
                await self.peer.request("stop", timeout=45)
            except Exception:
                pass
        await self.peer.close()
        await asyncio.to_thread(self.process.join, 5)
        if self.process.is_alive():
            # Each child is an isolated process group. Never signal another
            # instance or the supervising Core. Windows uses an exact PID tree.
            if os.name == "nt":
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
            else:
                try:
                    if os.getpgid(self.process.pid) == self.process.pid:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    else:
                        self.process.kill()
                except ProcessLookupError:
                    pass
            await asyncio.to_thread(self.process.join, 5)
        if self.job is not None:
            self.job.close()
            self.job = None
        if os.name != "nt" and self.owns_group:
            deadline = asyncio.get_running_loop().time() + 30
            while True:
                try:
                    os.killpg(self.process.pid, 0)
                except ProcessLookupError:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    break
                await asyncio.sleep(0.1)


class KernelRuntime:
    """Retain the process/lock while independently replacing Functions.

    Product execution and service state live exclusively in the child. Agent
    replacements are handled there without involving this broad handoff.
    """

    def __init__(
        self,
        code_root: Path,
        bridge_home: Path,
        runtime: RuntimeFingerprint,
        *,
        arguments: dict | None = None,
    ):
        self.code_root = code_root.resolve()
        self.bridge_home = bridge_home.resolve()
        self.runtime = runtime
        self.arguments = arguments or {}
        self.active = None
        self.release = None
        self.stop_event = asyncio.Event()
        self._switch_lock = asyncio.Lock()
        self.state_dir = instance_runtime_dir(self.bridge_home)

    def bootstrap(
        self, release, handoff=None, *, recovery=False, restore=False, initial=False
    ):
        terminal = None
        try:
            if sys.stdin.isatty():
                terminal = (
                    "CONIN$" if os.name == "nt" else os.ttyname(sys.stdin.fileno())
                )
        except (OSError, ValueError, AttributeError):
            pass
        return {
            **release,
            "code_root": str(self.code_root),
            "bridge_home": str(self.bridge_home),
            "runtime": self.runtime.to_dict(),
            "arguments": self.arguments,
            "handoff": handoff or {},
            "kernel_pid": os.getpid(),
            "process_group": True,
            "stdin_terminal": terminal,
            "recovery": recovery,
            "restore": restore,
            "initial": initial,
        }

    def publish(self, phase, **extra):
        try:
            write_record(
                self.state_dir / "kernel.json",
                {
                    "kernel_pid": os.getpid(),
                    "phase": phase,
                    "function_pid": self.active.process.pid if self.active else None,
                    "generation_id": self.release["manifest"]["generation_id"]
                    if self.release
                    else None,
                    "runtime": self.runtime.to_dict(),
                    **extra,
                },
            )
        except OSError as exc:
            print(f"Kernel status write failed: {exc}", file=sys.stderr, flush=True)

    async def start(self, release=None, *, recovery=False):
        release = release or await qualify(
            self.code_root, self.bridge_home, self.runtime
        )
        client = await ProcessClient.prepare(
            self.bootstrap(release, recovery=recovery, initial=self.release is None)
        )
        try:
            await client.peer.request(
                "activate",
                timeout=86400 if client.ready_metadata.get("interactive") else 360,
            )
            await client.peer.request("commit", timeout=120)
        except BaseException:
            await client.close()
            raise
        self.active, self.release = client, release
        self.publish("active")

    async def replace(self, release=None) -> bool:
        async with self._switch_lock:
            old, previous = self.active, self.release
            candidate, handoff = None, None
            retired = False
            try:
                release = release or await qualify(
                    self.code_root, self.bridge_home, self.runtime
                )
                candidate = await ProcessClient.prepare(self.bootstrap(release))
                self.publish("preparing_handoff")
                # The child closes intake and drains. A rejected drain leaves
                # old authority intact, so it can resume with no publication.
                handoff = await old.peer.request("quiesce", timeout=180)
                verify_artifact(Path(release["generation_root"]), release["manifest"])
                compare_runtime_fingerprints(
                    self.runtime, enforce_runtime_contract(self.code_root)
                )
                await old.close()
                retired = True
                await candidate.peer.request(
                    "activate", {"handoff": handoff}, timeout=360
                )
            except Exception as exc:
                if candidate is not None:
                    await candidate.close()
                if retired:
                    try:
                        restored = await ProcessClient.prepare(
                            self.bootstrap(previous, handoff, restore=True)
                        )
                        await restored.peer.request(
                            "activate", {"handoff": handoff or {}}, timeout=360
                        )
                        await restored.peer.request("commit", timeout=120)
                    except Exception as rollback_error:
                        if "restored" in locals():
                            await restored.close()
                        self.active = None
                        self.publish(
                            "failed", error=str(exc), rollback_error=str(rollback_error)
                        )
                        raise RuntimeError(
                            "Function replacement and rollback failed"
                        ) from rollback_error
                    self.active = restored
                elif old is not None and old.process.is_alive():
                    await old.peer.request("resume", timeout=45)
                self.release = previous
                self.publish("active", result="rolled_back", error=str(exc))
                return False
            # Publication is the commit point. A later notification/transport
            # fault cannot be reported as an all-or-none rollback.
            self.active, self.release = candidate, release
            try:
                await candidate.peer.request("commit", timeout=120)
            except Exception as exc:
                self.publish("active", result="committed", post_commit_error=str(exc))
            else:
                self.publish("active", result="committed")
            return True

    async def run(self):
        await self.start()
        recoveries = 0
        try:
            while not self.stop_event.is_set():
                for request_path in sorted(
                    (self.state_dir / "kernel-requests").glob("*.json")
                ):
                    try:
                        if request_path.stat().st_size > 4096:
                            raise ValueError("oversized process-control request")
                        request = json.loads(request_path.read_text(encoding="utf-8"))
                        request_id = request_path.stem
                        if (
                            request.get("id") != request_id
                            or len(request_id) != 32
                            or any(c not in "0123456789abcdef" for c in request_id)
                        ):
                            raise ValueError("invalid process-control request identity")
                    except (OSError, ValueError, AttributeError) as exc:
                        self.publish("active", request_error=str(exc))
                    else:
                        request_path.unlink()
                        ok = await self.replace()
                        try:
                            write_record(
                                self.state_dir
                                / ("replacement-" + request_id + ".json"),
                                {
                                    "ok": ok,
                                    "generation_id": self.release["manifest"][
                                        "generation_id"
                                    ],
                                },
                            )
                        except OSError as exc:
                            self.publish("active", receipt_error=str(exc))
                    finally:
                        request_path.unlink(missing_ok=True)
                if self.active and self.active.requested_exit:
                    break
                if not self.active or not self.active.process.is_alive():
                    if recoveries >= 3:
                        raise RuntimeError("Function process recovery exhausted")
                    recoveries += 1
                    if self.active:
                        await self.active.close()
                    # Recovery is pinned to the last committed artifact. It
                    # never adopts checkout edits or silently installs a release.
                    await self.start(self.release, recovery=True)
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=0.5)
                except TimeoutError:
                    pass
        finally:
            if self.active:
                await self.active.close()
            self.publish("stopped")
