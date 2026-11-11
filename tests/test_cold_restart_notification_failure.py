"""A notification exception must not reverse a durable successful restart."""
import json
import threading
from remote.api import server
from remote.restart_receipts import RestartReceiptReader
from remote.security.shared_token import build_auth_headers
from tests.test_remote_rescue_control import _client

def test_notification_exception_keeps_success_and_only_records_warning(tmp_path,monkeypatch):
    (tmp_path/"secrets.json").write_text('{"hashi_remote_shared_token":"test-secret"}',encoding="utf-8")
    monkeypatch.setattr(server,"_hashi_control_status",lambda:{"ok":True,"pid":10,
        "workbench_health":{"ok":True,"instance_id":"HASHI_TEST"}})
    monkeypatch.setattr(server,"_qualified_restart_runtime",lambda:None)
    monkeypatch.setattr(server,"_restart_hashi_process",lambda:{"pid":30,"command":["fixture"],"log_path":"fixture"})
    monkeypatch.setattr(server,"_restart_evidence",lambda *args,**kwargs:dict(
        old_pid_exited=True,new_pid_alive=True,new_pid_differs=True,backend_health_ok=True,
        instance_matches=True,runtime_version_verified=True,generation_verified=True,
        old_pid=10,new_pid=20,actual_instance="HASHI_TEST",runtime_version={"core_api":"4"},generation_id="g2"))
    notice_seen=threading.Event()
    def notice(_):notice_seen.set();raise OSError("private notification storage detail")
    monkeypatch.setattr(server,"_retry_restart_notification",notice)
    origin=dict(instance_id="HASHI_TEST",agent_id="ying",surface="workbench",
        owner_id="user:7",session_id="ses_test",context_generation=1,notice_id="restart:fixture")
    body=json.dumps(dict(detached=True,target_instance="HASHI_TEST",requester_agent="ying",notify_origin=origin)).encode()
    headers=build_auth_headers(shared_token="test-secret",method="POST",path="/control/hashi/restart",
        from_instance="HASHI_TEST",body_bytes=body);headers["Content-Type"]="application/json"
    with _client(tmp_path) as client:
        response=client.post("/control/hashi/restart",content=body,headers=headers)
        assert response.status_code==202,response.text
        rid=response.json()["restart_id"]
        assert notice_seen.wait(3)
        for _ in range(100):
            receipt=server._read_restart_record(rid)
            if receipt.get("notification",{}).get("state")=="failed" or receipt["state"]=="failed":break
            threading.Event().wait(.01)
        assert receipt["state"]=="completed" and receipt["ok"] is True
        assert receipt["notification"]["state"]=="failed"
        projected=RestartReceiptReader(tmp_path,instance_id="HASHI_TEST").operation(rid,owner_id="user:7")
        assert projected["status"]=="succeeded"
        assert projected["warnings"]==["restart_notification_failed"]
        assert "private notification" not in json.dumps(projected)
# HASHI-Fingerprint: hfp_72ab35536750429794d42257edba57b8
