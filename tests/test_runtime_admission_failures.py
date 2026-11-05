"""Fault injection retains real child creation and proves rejection has no effects."""
from __future__ import annotations

import asyncio
import os
import threading

import pytest

from orchestrator import runtime_process
from orchestrator.runtime_contract import RuntimeContractError
from tests.test_runtime_process import bootstrap


@pytest.mark.parametrize("defect", ["nonce", "pid", "generation", "executable", "packages", "schema",
    pytest.param("launcher-pid", marks=pytest.mark.skipif(os.name != "nt", reason="Windows venv launcher identity"))])
def test_forged_private_admission_never_runs_product(tmp_path, monkeypatch, defect):
    payload = bootstrap(tmp_path)
    decode = runtime_process.decode_envelope
    children = []
    popen = runtime_process.subprocess.Popen
    def observe(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child
    def forged(frame):
        reply = decode(frame)
        if reply.get("kind") == "response" and reply.get("ok"):
            value = reply["result"]
            if defect == "nonce":
                value["nonce"] = "f" * 64
            elif defect == "pid":
                value["pid"] = os.getpid()
            elif defect == "launcher-pid":
                value["pid"] = children[0].pid
            elif defect == "generation":
                value["generation_id"] = "sha256:" + "0" * 64
            elif defect == "executable":
                value["runtime"]["executable"] += ".forged"
            elif defect == "packages":
                value["runtime"]["dependency_digest"] = "sha256:" + "0" * 64
            elif defect == "schema":
                value["runtime"]["worker_protocol"] = True
        return reply
    monkeypatch.setattr(runtime_process.subprocess, "Popen", observe)
    monkeypatch.setattr(runtime_process, "decode_envelope", forged)
    spawned = None
    try:
        with pytest.raises(RuntimeContractError):
            spawned = runtime_process.launch_function_process(payload)
    finally:
        if spawned is not None:
            spawned[1].close()
            spawned[0].close()
    assert len(children) == 1
    assert children[0].poll() is not None
    assert not (tmp_path / "instance").exists()


@pytest.mark.asyncio
async def test_cancelling_native_launch_reclaims_child_before_return(tmp_path, monkeypatch):
    payload = bootstrap(tmp_path)
    reached = threading.Event()
    release = threading.Event()
    validate = runtime_process._validate_admission
    children = []
    popen = runtime_process.subprocess.Popen
    def observe(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child
    def pause(*args):
        reached.set()
        assert release.wait(10), "test control failed to release admission"
        return validate(*args)
    monkeypatch.setattr(runtime_process.subprocess, "Popen", observe)
    monkeypatch.setattr(runtime_process, "_validate_admission", pause)
    task = asyncio.create_task(runtime_process.launch_function_process_async(payload))
    try:
        assert await asyncio.to_thread(reached.wait, 10)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(children) == 1
    assert children[0].poll() is not None
    assert not (tmp_path / "instance").exists()
# HASHI-Fingerprint: hfp_69c56a52b1504127927a56e29bce0195
