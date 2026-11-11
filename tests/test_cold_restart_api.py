from types import SimpleNamespace
import json
import pytest
from tests.test_cold_restart_observation import record, save, RID, complete
from tests.test_session_api import _server, _Request

@pytest.mark.asyncio
async def test_existing_authenticated_operation_api_reads_cold_receipts_with_exact_owner_and_agent(tmp_path):
    server,_=_server(tmp_path)
    server.global_config.instance_id="HASHI3"
    warm=dict(operation_id="b"*32,action="reboot",mode="max",source_agent="lily",
        created_at=9999999999,terminal=True,status="completed")
    server.orchestrator=SimpleNamespace(reboot_manager=SimpleNamespace(
        visible_operations=lambda **kwargs:[warm],operation=lambda *args,**kwargs:None))
    save(tmp_path,record())
    response=await server.handle_reboot_operations(_Request())
    assert response.status==200
    assert response.headers["Cache-Control"]=="no-store"
    operations=json.loads(response.text)["operations"]
    assert operations[0]["operation_id"]==RID, "active restart precedes recent completed reboot"
    request=_Request(query={"agent_id":"ying","owner_id":"user:8"},match_info={"operation_id":RID})
    exact=await server.handle_reboot_operation(request)
    assert exact.status==200
    assert json.loads(exact.text)["operation"]["source_agent"]=="ying"
    assert (await server.handle_reboot_operation(_Request(query={"agent_id":"other"},
        match_info={"operation_id":RID}))).status==404
    value=record();value["notify_origin"]["owner_id"]="user:8";save(tmp_path,value)
    assert (await server.handle_reboot_operation(request)).status==404
    assert [op["operation_id"] for op in json.loads((await server.handle_reboot_operations(_Request())).text)["operations"]]==[warm["operation_id"]]
# HASHI-Fingerprint: hfp_1380cd748a824a1d98e1aa31aabf14bc
