"""Cold restart admission, durable owner isolation, and proof-based completion."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import pytest
from remote.restart_receipts import RestartReceiptReader, project_restart
from remote.api import server as remote_server
from remote.security.shared_token import build_auth_headers
from tests.test_remote_rescue_control import _client

RID = "rst_20261111T060000Z_abcdef123456"
def record():
    return {"restart_id": RID, "state": "accepted", "ok": False,
        "target_instance": "HASHI3", "controlled_instance": "HASHI3", "requester_agent": "ying",
        "notify_origin": {"owner_id": "user:7", "agent_id": "ying", "instance_id": "HASHI3"},
        "started_at": "2026-11-11T06:00:00Z", "phase": "target_validation",
        "requester": "HASHI3", "reason": "PRIVATE", "status_before": {"secret": "PRIVATE"}}
def complete(value):
    value.update(state="completed", ok=True, completed_at=datetime.now(timezone.utc).isoformat(),
        evidence=dict(old_pid_exited=True,new_pid_alive=True,new_pid_differs=True,
            backend_health_ok=True,instance_matches=True,runtime_version_verified=True,
            generation_verified=True,old_pid=10,new_pid=20,actual_instance="HASHI3",
            runtime_version={"core_api":"4"}, generation_id="generation-2"))
    return value

def save(tmp_path, value):
    folder=tmp_path/"state"/"restarts";folder.mkdir(parents=True,exist_ok=True)
    (folder/(RID+".json")).write_text(json.dumps(value),encoding="utf-8")
    return RestartReceiptReader(tmp_path, instance_id="HASHI3")

def test_owner_agent_instance_isolation_and_bounded_public_projection(tmp_path):
    reader=save(tmp_path,record())
    op=reader.operation(RID,owner_id="user:7",agent_id="ying")
    assert op["status"]=="running" and op["terminal"] is False
    assert "PRIVATE" not in json.dumps(op)
    assert reader.operation(RID,owner_id="user:8",agent_id="ying") is None
    assert reader.operation(RID,owner_id="user:7",agent_id="other") is None
    assert RestartReceiptReader(tmp_path,instance_id="HASHI4").operation(RID,owner_id="user:7") is None
    assert reader.visible_operations(owner_id="user:7")==[op]
    with pytest.raises(ValueError):
        reader.operation("../secrets",owner_id="user:7")

@pytest.mark.parametrize("mutation",["missing-proof","wrong-instance","same-pid","wrong-generation-type","invalid-json"])
def test_completed_without_proof_never_becomes_success(tmp_path,mutation):
    value=complete(record())
    if mutation=="missing-proof":value["evidence"]["old_pid_exited"]=False
    if mutation=="wrong-instance":value["evidence"]["actual_instance"]="HASHI4"
    if mutation=="same-pid":value["evidence"]["new_pid"]=10
    if mutation=="wrong-generation-type":value["evidence"]["runtime_version"]=None
    reader=save(tmp_path,value)
    if mutation=="invalid-json":
        (reader.directory/(RID+".json")).write_text('{"restart_id":NaN}',encoding="utf-8")
        assert reader.operation(RID,owner_id="user:7") is None
    else:
        op=reader.operation(RID,owner_id="user:7")
        assert op["terminal"] and op["status"]=="failed"

def test_verified_completion_retained_for_exact_read_after_discovery_window(tmp_path):
    value=complete(record());value["completed_at"]="2020-01-01T00:00:00Z"
    reader=save(tmp_path,value)
    assert reader.visible_operations(owner_id="user:7")==[]
    op=reader.operation(RID,owner_id="user:7",agent_id="ying")
    assert op["status"]=="succeeded" and op["kernel_pid"]==20
    assert op["shared_generation_id"]=="generation-2"

def test_detached_remote_admits_before_qualification_and_blocks_overlap(tmp_path,monkeypatch):
    import threading
    started=threading.Event();release=threading.Event()
    (tmp_path/"secrets.json").write_text('{"hashi_remote_shared_token":"test-secret"}',encoding="utf-8")
    status={"ok":True,"state":"running","pid":10,"workbench_health":{"ok":True,"instance_id":"HASHI_TEST"}}
    monkeypatch.setattr(remote_server,"_hashi_control_status",lambda:status)
    def qualification():
        started.set()
        assert release.wait(10)
        raise ValueError("qualification fixture failure")
    monkeypatch.setattr(remote_server,"_qualified_restart_runtime",qualification)
    monkeypatch.setattr(remote_server,"_restart_hashi_process",lambda:pytest.fail("never launch"))
    def post(client):
        origin={"instance_id":"HASHI_TEST","agent_id":"ying","surface":"workbench",
            "owner_id":"user:7","session_id":"ses_test","context_generation":1,"notice_id":"restart:fixture"}
        body=json.dumps({"target_instance":"HASHI_TEST","requester_agent":"ying",
            "notify_origin":origin,"detached":True}).encode()
        headers=build_auth_headers(shared_token="test-secret",method="POST",path="/control/hashi/restart",
            from_instance="HASHI_TEST",body_bytes=body)
        headers["Content-Type"]="application/json"
        return client.post("/control/hashi/restart",content=body,headers=headers)
    try:
        with _client(tmp_path) as client:
            response=post(client)
            assert response.status_code==202,response.text
            rid=response.json()["restart_id"]
            assert started.wait(2)
            persisted=remote_server._read_restart_record(rid)
            assert persisted["state"]=="accepted"
            assert post(client).status_code==409
            assert len(list((tmp_path/"state"/"restarts").glob("*.json")))==1
            release.set()
            # Bounded polling only observes this isolated fixture.
            for _ in range(100):
                state=client.get("/control/hashi/restarts/"+rid).json()["state"]
                if state=="failed":break
                threading.Event().wait(.01)
            assert state=="failed"
    finally:release.set()
# HASHI-Fingerprint: hfp_9693e4335cb04e129e26839db89e47ba
