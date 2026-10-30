"""The existing shared command projection submits private session decisions."""
from types import SimpleNamespace

import pytest

from orchestrator.command_interaction_bridge import dispatch_command_interaction
from orchestrator.permissions.frontend_control import (
    PermissionCommandProjection, PermissionIngress, bind_permission_ingress,
)
from tests.test_permission_decision_commands import control as control


@pytest.mark.asyncio
async def test_real_shared_projection_approves_without_forwarding_credentials_to_worker(control, tmp_path):
    store, _, authority, context, scope, request, subject, session, _, controller = control
    path = tmp_path / "agents.json"
    path.write_text('{"global":{},"agents":[{"name":"agent","permission_settings":{"requested_level":"L1"}}]}',
                    encoding="utf-8")
    calls = []

    async def unexpected_worker(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("permission credential must never enter Worker RPC")

    worker = SimpleNamespace(
        name="agent", global_config=SimpleNamespace(
            config_path=path, bridge_home=tmp_path, project_root=tmp_path,
            authorized_id=7, instance_id="INSTANCE", deployment_profile="personal",
        ), is_function_worker_proxy=True, execute_slash_command=unexpected_worker,
    )
    projection = PermissionCommandProjection(worker, session_store=store, views=authority)
    metadata = {
        "actor_id": 7, "instance_id": context.instance_id, "session_id": context.session_id,
        "context_generation": int(context.context_generation), "owner_id": context.owner_id,
        "session_surface": "workbench", "session_channel_key": "default",
        "connection_binding": "bindingabcdefghijklmnop", "source_channel": "session_api_command",
    }
    operation = {
        "version": 1, "op": "open", "command": f"/safety approve {request.request_id} once --revision 1 --for 2m",
        "ui_locale": "zh-CN", "client_id": "clientabcdefghijklmnop", "request_id": "requestabcdefghijklmnop",
    }
    # Public metadata and an API token cannot manufacture a private ingress.
    denied = await dispatch_command_interaction(projection, operation, {
        **metadata, "session_token": session.token, "_permission_authenticated": True,
    })
    assert not denied["ok"] and authority.decision_count(request.request_id) == 0
    assert not calls
    operation["request_id"] = "requestvalidabcdefghijk"
    ingress = PermissionIngress(subject, controller, session.token, metadata["connection_binding"])
    assert session.token not in repr(ingress)
    with bind_permission_ingress(ingress):
        result = await dispatch_command_interaction(projection, operation, metadata)
    assert result["ok"], result
    assert "已批准" in result["messages"][-1]["text"]
    assert authority.evaluate(context, [scope], operation_id="op")
    assert authority.decision_count(request.request_id) == 1
    assert not calls
    assert session.token not in repr(result)
    assert session.token.encode() not in authority.path.read_bytes()
    rows = store.messages(context.session_id, owner_id=context.owner_id)
    command_rows = [row for row in rows if row["role"] == "assistant"]
    assert command_rows and all(not row["history_eligible"] for row in command_rows)
    assert session.token not in repr(rows)
