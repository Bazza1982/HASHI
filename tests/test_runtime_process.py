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
    children = await asyncio.gather(*(launch_function_process_async(p) for p in payloads))
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
            process.close()
    await asyncio.gather(*(exercise(i, child) for i, child in enumerate(children)))
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
