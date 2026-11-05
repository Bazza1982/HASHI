"""Selected identity and observable process/port completion at the CLI boundary."""
from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import hashi_instance_cli as cli
from tools.instance_registry import InstanceRegistry


@pytest.fixture
def target(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    record = {
        "record_id": "fixture", "name": "selected", "instance_id": "SELECTED",
        "code_root": str(tmp_path / "program"), "bridge_home": str(home),
        "managed": False, "source_kind": "git", "api_port": 18800,
        "gateway_port": 18801, "adopted_program_version": None,
    }
    registry = InstanceRegistry(program_root=tmp_path / "program", program_version="test",
        registry_root=tmp_path / "registry", data_root=tmp_path / "data", environment_id="test-os")
    return registry, record


def test_selected_instance_is_the_identity_received_by_the_actual_child(target, monkeypatch):
    _, record = target
    monkeypatch.setenv("HASHI_INSTANCE_ID", "DIFFERENT_PARENT")
    child = subprocess.run(
        [sys.executable, "-c", "import os,json; print(json.dumps([os.environ['HASHI_INSTANCE_ID'],os.environ['BRIDGE_HOME']]))"],
        env=cli._launch_environment(record, Path(record["code_root"])),
        check=True, capture_output=True, text=True,
    )
    assert json.loads(child.stdout) == [record["instance_id"], str(Path(record["bridge_home"]).resolve())]


@pytest.mark.parametrize("remaining", ["process", "api", "gateway", "lock", "released"])
def test_stop_cannot_report_success_with_an_owned_process_or_port_left(target, monkeypatch, remaining):
    registry, record = target
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    live_port = listener.getsockname()[1]
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    closed_port = closed.getsockname()[1]
    closed.close()
    snapshot = {
        "instance_id": "SELECTED",
        "lock_held": True, "healthy": True, "running": True,
        "activity_verified": True, "busy": False, "busy_agents": [],
        "api_port": live_port if remaining == "api" else closed_port,
        "gateway_port": live_port if remaining == "gateway" else closed_port,
        "pid": child.pid, "agents": [],
    }
    calls = []

    def shutdown(*args, **kwargs):
        calls.append(args)
        if remaining != "process":
            child.terminate()
            child.wait(timeout=5)
        return 200, {"ok": True}

    monkeypatch.setattr(cli, "inspect_instance", lambda *_: snapshot)
    monkeypatch.setattr(cli, "_lock_is_held", lambda _: remaining == "lock")
    monkeypatch.setattr(cli, "_admin_token", lambda *_: "fixture-token")
    monkeypatch.setattr(cli, "_http_json", shutdown)
    if remaining == "released":
        listener.close()
    try:
        expected = 0 if remaining == "released" else cli.EXIT_BUSY
        assert cli.stop_instance(registry, record, timeout=.02) == expected
        assert calls == [("POST", snapshot["api_port"], "/api/admin/shutdown")]
        if remaining == "process":
            assert child.poll() is None, "completion verification must not manufacture success by force killing"
    finally:
        listener.close()
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)


@pytest.mark.parametrize("phase", ["existing", "after_launch"])
@pytest.mark.parametrize("observed_identity", ["DEFAULT", None])
def test_start_refuses_a_receipt_for_a_different_selected_instance(target, monkeypatch, phase, observed_identity):
    registry, record = target
    matching = {"instance_id":"SELECTED", "running":False, "lock_held":False,
                "healthy":False, "foreign_endpoint":False}
    wrong = {**matching, "instance_id":observed_identity, "running":True, "ready":True,
             "pid":123, "api_port":18800}
    snapshots = iter([wrong] if phase == "existing" else [matching, wrong])
    monkeypatch.setattr(cli, "inspect_instance", lambda *_: next(snapshots))
    monkeypatch.setattr(cli, "_is_provisioned", lambda *_: True)
    monkeypatch.setattr(cli, "_select_runtime", lambda *_args, **_kwargs: [sys.executable])
    starts = []
    def start(*args, **kwargs):
        starts.append(kwargs)
        return type("Child", (), {"poll":lambda self:None})()
    monkeypatch.setattr(cli.subprocess, "Popen", start)
    assert cli.start_instance(registry, record, timeout=.01) == cli.EXIT_BUSY
    assert len(starts) == (0 if phase == "existing" else 1)


def test_stop_waits_for_the_original_descendant_after_parent_exit(target, monkeypatch):
    import psutil
    registry, record = target
    program = ("import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        "print(child.pid,flush=True); time.sleep(30)")
    parent = subprocess.Popen([sys.executable,"-c",program],stdout=subprocess.PIPE,text=True)
    descendant = psutil.Process(int(parent.stdout.readline()))
    descendant.create_time()
    snapshot = {"instance_id":"SELECTED","lock_held":True,"healthy":True,"running":True,"activity_verified":True,
        "busy":False,"busy_agents":[],"api_port":0,"gateway_port":0,"pid":parent.pid,"agents":[]}
    monkeypatch.setattr(cli,"inspect_instance",lambda *_:snapshot)
    monkeypatch.setattr(cli,"_lock_is_held",lambda _:False)
    monkeypatch.setattr(cli,"_admin_token",lambda *_:"fixture")
    def shutdown(*_args,**_kwargs):
        parent.terminate()
        parent.wait(timeout=5)
        return 200,{"ok":True}
    monkeypatch.setattr(cli,"_http_json",shutdown)
    try:
        assert cli.stop_instance(registry,record,timeout=.02)==cli.EXIT_BUSY
        assert parent.poll() is not None and descendant.is_running()
    finally:
        if parent.poll() is None:
            parent.terminate()
        parent.wait(timeout=5)
        parent.stdout.close()
        if descendant.is_running():
            descendant.terminate()
        try:
            descendant.wait(timeout=5)
        except psutil.NoSuchProcess:
            pass
