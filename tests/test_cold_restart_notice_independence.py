"""A terminal receipt must release lifecycle admission while notice delivery is slow."""
import json
import threading
from tests.test_remote_rescue_control import _client
from remote.api import server
from remote.security.shared_token import build_auth_headers

def test_slow_telegram_notice_does_not_block_next_cold_restart(tmp_path, monkeypatch):
    (tmp_path/"secrets.json").write_text('{"hashi_remote_shared_token":"test-secret"}',encoding="utf-8")
    begun=threading.Event();release=threading.Event()
    monkeypatch.setattr(server,"_hashi_control_status",lambda:{"ok":True,"pid":10,
        "workbench_health":{"ok":True,"instance_id":"HASHI_TEST"}})
    monkeypatch.setattr(server,"_qualified_restart_runtime",lambda:None)
    monkeypatch.setattr(server,"_restart_hashi_process",lambda:{"pid":30,"command":["fixture"],"log_path":"fixture"})
    monkeypatch.setattr(server,"_restart_evidence",lambda *args,**kwargs:{
        "old_pid_exited":True,"new_pid_alive":True,"new_pid_differs":True,"backend_health_ok":True,
        "instance_matches":True,"runtime_version_verified":True,"generation_verified":True,"new_pid":20})
    def notify(record):
        assert record["state"]=="completed"
        begun.set();assert release.wait(10)
        return record
    monkeypatch.setattr(server,"_retry_restart_notification",notify)
    def post(client,index):
        body=json.dumps({"detached":True,"target_instance":"HASHI_TEST","requester_agent":"ying",
            "notify_origin":{"instance_id":"HASHI_TEST","agent_id":"ying","surface":"workbench",
                "owner_id":"user:7","session_id":"ses_test","context_generation":1,"notice_id":"restart:"+str(index)}}).encode()
        headers=build_auth_headers(shared_token="test-secret",method="POST",path="/control/hashi/restart",
            from_instance="HASHI_TEST",body_bytes=body)
        headers["Content-Type"]="application/json"
        return client.post("/control/hashi/restart",content=body,headers=headers)
    try:
        with _client(tmp_path) as client:
            first=post(client,1);assert first.status_code==202,first.text
            assert begun.wait(3)
            assert server._read_restart_record(first.json()["restart_id"])["state"]=="completed"
            second=post(client,2)
            assert second.status_code==202,second.text
            assert second.json()["restart_id"]!=first.json()["restart_id"]
            release.set()
    finally:release.set()
# HASHI-Fingerprint: hfp_4ce08834baba4baa8a9920210945d1b2
