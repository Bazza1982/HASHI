"""Real private-channel process admission, lifecycle and durable recovery."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from orchestrator.kernel_artifact import manifest_digest
from orchestrator.runtime_process import launch_function_process, launch_function_process_async
from orchestrator.runtime_contract import RuntimeContractError, enforce_runtime_contract
from orchestrator.function_worker_protocol import JsonConnectionPeer

ROOT = Path(__file__).resolve().parents[1]
PEER = '''
import asyncio, os, sys
from orchestrator.function_worker_protocol import JsonConnectionPeer
async def serve(connection, bootstrap):
    from pathlib import Path
    home = Path(bootstrap["bridge_home"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "entered-product.txt").write_text(str(os.getpid()))
    stop = asyncio.Event()
    async def request(method, params):
        if method == "crash": os._exit(17)
        if method == "stop": stop.set()
        if method == "write":
            from pathlib import Path
            state = Path(bootstrap["bridge_home"]) / "durable.txt"
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text(params["value"])
        return {"pid": os.getpid(), "prefix": sys.prefix, "executable": sys.executable, "value": params}
    peer = JsonConnectionPeer(connection, label="native-fixture", request_handler=request)
    peer.start()
    await peer.emit("fixture.started", {"pid": os.getpid()})
    while not stop.is_set() and not peer.is_closed: await asyncio.sleep(.01)
    await peer.close()
'''

def bootstrap(tmp_path):
    artifact = tmp_path / "artifact"
    source = artifact / "orchestrator/runtime_process_fixture.py"
    source.parent.mkdir(parents=True)
    source.write_text(PEER, encoding="utf-8")
    entries = [{"module": "orchestrator.runtime_process_fixture",
                "relative_path": "orchestrator/runtime_process_fixture.py",
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]
    return {"code_root": str(ROOT), "bridge_home": str(tmp_path / "instance"),
            "generation_root": str(artifact),
            "manifest": {"generation_id": manifest_digest(entries), "entries": entries,
                         "assets": [], "schema_version": 2},
            "runtime": enforce_runtime_contract(ROOT).to_dict(),
            "entrypoint": "orchestrator.runtime_process_fixture:serve"}

@pytest.mark.asyncio
async def test_real_private_bootstrap_roundtrip_events_crash_and_persistence(tmp_path):
    data = bootstrap(tmp_path)
    process, connection = await launch_function_process_async(data)
    events = []
    async def event(name, payload):
        events.append((name, payload))
    peer = JsonConnectionPeer(connection, label="test-parent", event_handler=event)
    peer.start()
    try:
        observed = await peer.request("write", {"value": "once"})
        assert observed["pid"] == process.pid
        assert Path(observed["prefix"]).resolve() == Path(data["runtime"]["environment_prefix"]).resolve()
        assert Path(observed["executable"]).resolve() == Path(data["runtime"]["executable"]).resolve()
        assert (tmp_path / "instance/durable.txt").read_text() == "once"
        assert events == [("fixture.started", {"pid": process.pid})]
        with pytest.raises(Exception):
            await peer.request("crash", timeout=3)
        await asyncio.to_thread(process.join, 5)
        assert process.exitcode == 17
        assert not process.is_alive()
        assert (tmp_path / "instance/durable.txt").read_text() == "once"
    finally:
        await peer.close()
        process.close()

@pytest.mark.asyncio
async def test_parallel_start_has_no_crossed_channels(tmp_path):
    payloads = [bootstrap(tmp_path / str(i)) for i in range(4)]
    attempts = await asyncio.gather(
        *(launch_function_process_async(p) for p in payloads), return_exceptions=True)
    children = [result for result in attempts if not isinstance(result, BaseException)]
    async def exercise(index, child):
        process, connection = child
        peer = JsonConnectionPeer(connection, label=str(index))
        peer.start()
        try:
            reply = await peer.request("echo", {"index": index})
            assert reply["pid"] == process.pid
            assert reply["value"] == {"index": index}
        finally:
            await peer.close()
            await asyncio.to_thread(process.join, 5)
    try:
        for result in attempts:
            if isinstance(result, BaseException):
                raise result
        results = await asyncio.gather(
            *(exercise(i, child) for i, child in enumerate(children)), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
    finally:
        # A failed launch/assertion still owns every successful sibling.
        for process, connection in children:
            connection.close()
            process.close()
    assert len({p.pid for p, _ in children}) == 4
    assert all(not p.is_alive() for p, _ in children)


@pytest.mark.parametrize("defect", ["runtime", "artifact", "untrusted-entry"])
def test_rejects_identity_or_generation_before_any_product_effect(tmp_path, defect):
    data = bootstrap(tmp_path)
    if defect == "runtime":
        data["runtime"]["executable"] += ".forged"
    elif defect == "artifact":
        (Path(data["generation_root"]) / "orchestrator/runtime_process_fixture.py").write_text("raise AssertionError('tamper')")
    else:
        data["entrypoint"] = "os:system"
    with pytest.raises((RuntimeContractError, ValueError)):
        launch_function_process(data)
    assert not (tmp_path / "instance").exists()

def test_child_cannot_choose_an_executable_with_bootstrap_fields(tmp_path):
    data = bootstrap(tmp_path)
    data["executable"] = "unapproved-python"
    with pytest.raises(RuntimeContractError, match="override"):
        launch_function_process(data)
    assert not (tmp_path / "instance").exists()
# HASHI-Fingerprint: hfp_1d248faea4a141c08075bdac425290a5


@pytest.mark.platform
@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows Job creation boundary")
def test_venv_child_cannot_start_before_job_ownership(tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes
    import time
    from orchestrator import runtime_process

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD), ("usage", wintypes.DWORD),
            ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t),
            ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
            ("parent", wintypes.DWORD), ("priority", wintypes.LONG),
            ("flags", wintypes.DWORD), ("image", wintypes.WCHAR * 260),
        ]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    api.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    api.Process32FirstW.restype = wintypes.BOOL
    api.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    api.Process32NextW.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    early_children = []

    def children(parent_pid):
        handle = api.CreateToolhelp32Snapshot(2, 0)
        assert handle and handle != ctypes.c_void_p(-1).value
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        found = []
        try:
            okay = api.Process32FirstW(handle, ctypes.byref(entry))
            while okay:
                if entry.parent == parent_pid:
                    found.append(int(entry.pid))
                okay = api.Process32NextW(handle, ctypes.byref(entry))
        finally:
            api.CloseHandle(handle)
        return found

    owner = runtime_process.WindowsProcessJob

    parent = __import__("os").getpid()
    existing = set(children(parent))

    def delayed_owner(pid=None):
        # Force the original redirector/child race. Atomic creation must not
        # start even the launcher until its owned Job has been created.
        deadline = time.monotonic() + .3
        while time.monotonic() < deadline and not early_children:
            observed = children(pid) if pid is not None else (
                set(children(parent)) - existing)
            early_children.extend(observed)
            time.sleep(.01)
        return owner(pid)

    monkeypatch.setattr(runtime_process, "WindowsProcessJob", delayed_owner)
    process, connection = launch_function_process(bootstrap(tmp_path))
    try:
        assert not early_children, "venv child executed before its owned Job existed"
        assert process.job is not None
    finally:
        connection.close()
        process.close()


@pytest.mark.platform
@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows Job parent-crash qualification")
@pytest.mark.parametrize("phase", ["created", "grandchild"])
def test_parent_crash_retires_owned_interpreter_and_descendants(tmp_path, phase):
    import ctypes
    from ctypes import wintypes
    import json
    import multiprocessing
    import os
    import sys
    from orchestrator import runtime_process

    data = bootstrap(tmp_path)
    marker = tmp_path / "crash-observation.json"
    if phase == "grandchild":
        source = Path(data["generation_root"]) / data["manifest"]["entries"][0]["relative_path"]
        source.write_text("""
import asyncio,json,os,subprocess,sys,time
from pathlib import Path
async def serve(connection,bootstrap):
    home=Path(bootstrap["bridge_home"])
    home.mkdir(parents=True,exist_ok=True)
    actual=home/"grandchild.json"
    script="import json,os,sys,time;from pathlib import Path;Path(sys.argv[1]).write_text(json.dumps(os.getpid()));time.sleep(300)"
    grandchild=subprocess.Popen([sys.executable,"-I","-B","-c",script,str(actual)],
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    deadline=time.monotonic()+10
    while not actual.exists():
        if time.monotonic()>deadline: raise RuntimeError("grandchild failed to start")
        await asyncio.sleep(.01)
    pending=home/"tree.pending"
    pending.write_text(json.dumps({
        "worker":os.getpid(),"redirector":grandchild.pid,"grandchild":json.loads(actual.read_text())}))
    pending.replace(home/"tree.json")
    while True: await asyncio.sleep(1)
""", encoding="utf-8")
        data["manifest"]["entries"][0]["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        data["manifest"]["generation_id"] = manifest_digest(data["manifest"]["entries"])
    request = tmp_path / "request.json"
    request.write_text(json.dumps(data), encoding="utf-8")
    controller = tmp_path / "controller.py"
    controller.write_text("""
import json,os,sys,time
from pathlib import Path
root,request,marker,phase,release=map(str,sys.argv[1:])
sys.path.insert(0,root)
from orchestrator import runtime_process
payload=json.loads(Path(request).read_text())
def finish(record):
    pending=Path(marker).with_suffix(".pending")
    pending.write_text(json.dumps(record))
    pending.replace(Path(marker))
    deadline=time.monotonic()+20
    while not Path(release).exists():
        if time.monotonic()>deadline: raise RuntimeError("test did not release crash")
        time.sleep(.01)
    os._exit(41)
if phase=="created":
    create=runtime_process._start_windows_process
    def crash(*args,**kwargs):
        process=create(*args,**kwargs)
        finish({"launcher":process.pid})
    runtime_process._start_windows_process=crash
owned = runtime_process.launch_function_process(payload)
tree=Path(payload["bridge_home"])/"tree.json"
deadline=time.monotonic()+15
while not tree.exists():
    if time.monotonic()>deadline: raise RuntimeError("worker tree failed to start")
    time.sleep(.01)
finish(json.loads(tree.read_text()))
""", encoding="utf-8")
    parent, inherited = multiprocessing.get_context("spawn").Pipe(duplex=True)
    owner = runtime_process.WindowsProcessJob()
    process = None
    try:
        with runtime_process._START_LOCK:
            os.set_handle_inheritable(inherited.fileno(), True)
            try:
                native = runtime_process._start_windows_process(
                    [sys.executable, "-I", "-B", str(controller), str(ROOT),
                     str(request), str(marker), phase, str(tmp_path / "release-crash")],
                    environment=dict(os.environ), cwd=ROOT,
                    channel_handle=inherited.fileno(), job=owner)
                process = runtime_process.RuntimeProcess(native)
                process.job = owner
            finally:
                os.set_handle_inheritable(inherited.fileno(), False)
        inherited.close()
        import time
        deadline = time.monotonic() + 20
        while not marker.exists():
            assert process.is_alive(), "controller failed before publishing its owned children"
            assert time.monotonic() < deadline, "controller did not publish its children"
            time.sleep(.01)
        # The creator's existing Job remains an ancestor of every new Job.
        # Child assignment must never escape pre-existing host restrictions.
        recorded = json.loads(marker.read_text())
        assert len(recorded) == (1 if phase == "created" else 3)
        for pid in recorded.values():
            owner.include(pid)
        (tmp_path / "release-crash").write_text("release", encoding="utf-8")
        assert process._process.wait(40) == 41
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        for pid in recorded.values():
            handle = api.OpenProcess(0x00101000, False, pid)
            if not handle:
                assert ctypes.get_last_error() == 87, "cannot verify recorded child retirement"
                continue
            try:
                assert api.WaitForSingleObject(handle, 5000) == 0, (
                    f"owned {phase} descendant {pid} survived its parent")
            finally:
                api.CloseHandle(handle)
        if phase == "created":
            assert not (tmp_path / "instance/entered-product.txt").exists()
    finally:
        parent.close()
        inherited.close()
        if process is not None:
            process.close()
        owner.close()


def _assert_completed_launches_release_handles(tmp_path):
    import ctypes
    from ctypes import wintypes
    import gc
    from orchestrator.function_worker_protocol import decode_envelope

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetCurrentProcess.restype = wintypes.HANDLE
    api.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    api.GetProcessHandleCount.restype = wintypes.BOOL

    def count():
        value = wintypes.DWORD()
        assert api.GetProcessHandleCount(api.GetCurrentProcess(), ctypes.byref(value))
        return value.value

    def cycle(index):
        process, connection = launch_function_process(bootstrap(tmp_path / str(index)))
        try:
            assert connection.poll(5)
            event = decode_envelope(connection.recv_bytes())
            assert event["kind"] == "event" and event["event"] == "fixture.started"
            assert event["payload"]["pid"] == process.pid
        finally:
            connection.close()
            process.join(5)
            process.close()

    # Warm one-time standard-library/native initialization before comparing
    # actual OS handle counts. No tolerance hides an accumulating leaked handle.
    cycle("warmup")
    gc.collect()
    baseline = count()
    for index in range(8):
        cycle(index)
        gc.collect()
        assert count() == baseline, "completed Function launch leaked a parent HANDLE"


@pytest.mark.platform
@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows native HANDLE retirement")
def test_completed_launches_release_all_parent_handles(tmp_path):
    import subprocess
    import sys

    # The suite process also owns unrelated Windows/RPC handles. Main reproduced
    # a -2 delta from ALPC Port cleanup while the Function-owned handles retired.
    # Measure the same real launches in a fresh process, retaining exact equality
    # after every cycle rather than tolerating growth or retrying an assertion.
    probe = (
        "import runpy,sys;from pathlib import Path;"
        "sys.path.insert(0,sys.argv[1]);"
        "module=runpy.run_path(sys.argv[2]);"
        "module['_assert_completed_launches_release_handles'](Path(sys.argv[3]))"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", probe, str(ROOT), __file__, str(tmp_path)],
        cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=90,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
