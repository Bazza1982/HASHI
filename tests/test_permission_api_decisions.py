"""Original Backend API session headers, rather than public metadata, authorize."""
import base64
import json
from types import SimpleNamespace

import pytest

from orchestrator.workbench_api import WorkbenchApiServer
from tests.test_session_api import _Request

from tests.test_permission_decision_commands import control as control


def api_fixture(control, tmp_path):
    store, _, authority, context, _, _, _, _, _, controller = control
    path = tmp_path / "agents.json"
    path.write_text('{"global":{},"agents":[{"name":"agent","permission_settings":{"requested_level":"L1"}}]}',
                    encoding="utf-8")
    config = SimpleNamespace(config_path=path, bridge_home=tmp_path, project_root=tmp_path,
                             authorized_id=7, instance_id=context.instance_id, deployment_profile="personal")
    calls = []

    async def worker_rpc(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("trusted permission command entered generic Worker RPC")

    runtime = SimpleNamespace(name="agent", global_config=config, is_function_worker_proxy=True,
                              execute_slash_command=worker_rpc)
    server = WorkbenchApiServer.__new__(WorkbenchApiServer)
    server.global_config, server.config_path, server.session_store = config, path, store
    server._runtime_map = lambda: {"agent": runtime}
    server._v1_owner_id = lambda request: context.owner_id
    server.permission_control = SimpleNamespace(
        get_session_request=authority.get_session_request,
        list_session_requests=authority.list_session_requests,
        withdraw_session_request=authority.withdraw_session_request,
        submit_session_decision=controller.submit_session_decision,
        issue_session_actions=controller.issue_session_actions,
        submit_session_action=controller.submit_session_action,
    )
    return server, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [True, False])
async def test_session_command_path_accepts_only_original_user_session_header(control, tmp_path, valid):
    _, _, authority, context, _, request, _, session, _, _ = control
    server, calls = api_fixture(control, tmp_path)
    payload = {
        "command": "safety",
        "arguments": ["approve", request.request_id, "once", "--revision", "1", "--for", "2m"],
        "client_id": "frontendabcdefghijkl", "request_id": "approvalabcdefghijkl",
        "context_generation": int(context.context_generation), "ui_locale": "zh-CN",
    }
    response = await server.handle_v1_session_command_invocation(_Request(
        payload, match_info={"session_id": context.session_id},
        headers={"Authorization": "Bearer " + (session.token if valid else "shared-admin-token")},
    ))
    data = json.loads(response.text)
    assert data["ok"] is valid, data
    assert authority.decision_count(request.request_id) == int(valid)
    assert not calls
    assert session.token not in response.text
    # Existing durable FC command retry returns the persisted rendition.
    replay = await server.handle_v1_session_command_invocation(_Request(
        payload, match_info={"session_id": context.session_id},
        headers={"Authorization": "Bearer " + session.token},
    ))
    replay_data = json.loads(replay.text)
    assert replay_data["replayed"]
    assert authority.decision_count(request.request_id) == int(valid)

def encoded(operation, context):
    from orchestrator.command_interaction_transport import TRANSPORT_PREFIX
    payload = {**operation, "session_id": context.session_id,
               "context_generation": int(context.context_generation),
               "connection_binding": "client-supplied-is-not-human-authority"}
    return TRANSPORT_PREFIX + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


@pytest.mark.asyncio
async def test_existing_opaque_menu_action_is_private_and_cannot_be_replayed_cross_connection(control, tmp_path):
    _, _, authority, context, _, request, _, session, _, _ = control
    server, calls = api_fixture(control, tmp_path)
    operation = {
        "version": 1, "op": "open", "command": f"/safety request {request.request_id}",
        "ui_locale": "zh-CN", "client_id": "frontendabcdefghijkl",
        "request_id": "displayabcdefghijklmnop",
    }
    headers = {"Authorization": "Bearer " + session.token}
    response = await server.handle_agent_command(_Request(
        {"command": encoded(operation, context)}, match_info={"name": "agent"}, headers=headers,
    ))
    data = json.loads(response.text)
    assert data["ok"], data
    ui = data["messages"][-1]["command_ui"]
    assert authority.decision_count(request.request_id) == 0
    assert "4 小时" in data["messages"][-1]["text"]
    action = {
        "version": 1, "op": "act", "client_id": operation["client_id"],
        "request_id": "clickabcdefghijklmnop", "menu_id": ui["menu_id"],
        "revision": ui["revision"], "button_id": ui["rows"][0][0]["button_id"],
        "ui_locale": "zh-CN",
    }
    assert action["button_id"].startswith("act_")
    # This is still a shared admin connection, not the original human session.
    denied = await server.handle_agent_command(_Request(
        {"command": encoded({**action, "client_id": "anotherclientabcdefgh"}, context)},
        match_info={"name": "agent"}, headers=headers,
    ))
    assert not json.loads(denied.text)["ok"]
    assert authority.decision_count(request.request_id) == 0
    approved = await server.handle_agent_command(_Request(
        {"command": encoded(action, context)}, match_info={"name": "agent"}, headers=headers,
    ))
    result = json.loads(approved.text)
    assert result["ok"], result
    assert authority.decision_count(request.request_id) == 1
    assert "已批准" in result["messages"][-1]["text"]
    assert not calls
    assert session.token not in response.text + denied.text + approved.text
