"""L1 personal UI clicks need no second login; exact server actions still own scope."""
import json
from dataclasses import replace
from types import MethodType, SimpleNamespace

import pytest

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.models import PermissionSettings, PermissionLevel, normalize_scope
from orchestrator.workbench_api import WorkbenchApiServer
from tests.test_permission_service_application import environment as environment
from tests.test_permission_api_decisions import encoded
from tests.test_session_api import _Request


def api(service, subject, tmp_path):
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({"global": {}, "agents": [{
        "name": subject.agent_id, "permission_settings": {"requested_level": "L1"},
    }]}), encoding="utf-8")
    config = SimpleNamespace(config_path=path, bridge_home=tmp_path, project_root=tmp_path,
                             authorized_id=7, instance_id=subject.instance_id, deployment_profile="personal")
    runtime = SimpleNamespace(name=subject.agent_id, global_config=config)
    server = WorkbenchApiServer.__new__(WorkbenchApiServer)
    server.global_config, server.config_path, server.session_store = config, path, service.store
    server.permission_control = service
    server._runtime_map = lambda: {subject.agent_id: runtime}
    server._v1_owner_id = lambda request: subject.owner_id
    # Transport authentication is exercised separately by the existing API tests.
    server._strict_authenticated_owner = lambda request: subject.owner_id
    return server


@pytest.mark.asyncio
async def test_l1_personal_button_without_login_commits_exactly_once(environment, tmp_path):
    service, authority, identity, arguments, subject = environment
    binding = await service.open_execution(**arguments)
    scope = normalize_scope("file", str(tmp_path / "one.txt"), ("write",), cwd=tmp_path)
    request = authority.request_access(binding.context, [scope], purpose="Write one file",
                                       operation_id="one-operation", arguments_digest="a" * 64)
    server = api(service, subject, tmp_path)
    server.admin_token = "application-transport-only"
    server._strict_authenticated_owner = MethodType(WorkbenchApiServer._strict_authenticated_owner, server)
    headers = {"X-Workbench-Token": server.admin_token}
    opening = {"version": 1, "op": "open", "command": f"/safety prompt {request.request_id}",
               "ui_locale": "zh-CN", "client_id": "frontendabcdefghijkl",
               "request_id": "displayabcdefghijklmnop"}
    response = await server.handle_agent_command(_Request(
        {"command": encoded(opening, binding.context)}, match_info={"name": subject.agent_id}, headers=headers))
    view = json.loads(response.text)
    assert view["ok"], view
    menu = view["messages"][-1].get("command_ui")
    assert menu and menu["rows"], "A pending request must have real approval buttons"
    assert authority.decision_count(request.request_id) == 0
    once = next(button for row in menu["rows"] for button in row if "一次" in button["text"])
    action = {"version": 1, "op": "act", "client_id": opening["client_id"],
              "request_id": "clickabcdefghijklmnop", "menu_id": menu["menu_id"],
              "revision": menu["revision"], "button_id": once["button_id"], "ui_locale": "zh-CN"}
    for _ in range(2):
        clicked = await server.handle_agent_command(_Request(
            {"command": encoded(action, binding.context)}, match_info={"name": subject.agent_id}, headers=headers))
        assert json.loads(clicked.text)["ok"], clicked.text
    assert authority.decision_count(request.request_id) == 1
    assert authority.evaluate(binding.context, [scope], operation_id="one-operation")
    assert not authority.evaluate(binding.context, [scope], operation_id="another-operation")
    assert "hs_sess_" not in response.text + clicked.text


@pytest.mark.asyncio
async def test_personal_short_session_requires_l1_and_current_exact_owner(environment):
    service, _, identity, arguments, subject = environment
    await service.open_execution(**arguments)
    login = service.personal_l1_login(subject=subject)
    assert identity.get_session_user(login["session_token"]).id == "human"
    service.logout(session_token=login["session_token"])
    assert identity.get_session_user(login["session_token"]) is None
    for bad in (replace(subject, owner_id="other"), replace(subject, agent_id="other"),
                replace(subject, context_generation="2"), replace(subject, instance_id="FOREIGN")):
        with pytest.raises((ValueError, PermissionDenied)):
            service.personal_l1_login(subject=bad)
    service.levels = ("L1", "L2")
    service.personal_l1_login(subject=subject)
    resolver = service._policy_resolver
    service._policy_resolver = lambda *args: replace(resolver(*args),
        settings=PermissionSettings(PermissionLevel.L2))
    with pytest.raises(PermissionDenied):
        service.personal_l1_login(subject=subject)
    service._policy_resolver = resolver
    service.levels = ("L2",)
    with pytest.raises(PermissionDenied):
        service.personal_l1_login(subject=subject)


@pytest.mark.asyncio
async def test_personal_fallback_never_accepts_governed_or_unauthenticated_click(environment, tmp_path):
    service, authority, _, arguments, subject = environment
    binding = await service.open_execution(**arguments)
    scope = normalize_scope("file", str(tmp_path / "one.txt"), ("write",), cwd=tmp_path)
    request = authority.request_access(binding.context, [scope], purpose="Write")
    server = api(service, subject, tmp_path)
    command = f"/safety prompt {request.request_id}"
    for governed, authenticated in ((True, True), (False, False)):
        server._is_governed_profile = lambda: governed
        server._strict_authenticated_owner = lambda req: subject.owner_id if authenticated else None
        opening = {"version": 1, "op": "open", "command": command, "ui_locale": "zh-CN",
                   "client_id": "frontendabcdefghijkl", "request_id": f"display{int(governed)}abcdefghijklmnop"}
        response = await server.handle_agent_command(_Request(
            {"command": encoded(opening, binding.context)}, match_info={"name": subject.agent_id}))
        data = json.loads(response.text)
        assert not any(m.get("command_ui", {}).get("rows") for m in data.get("messages", []))
        assert authority.decision_count(request.request_id) == 0

@pytest.mark.asyncio
async def test_request_discovery_is_scoped_read_only_and_survives_reentry(environment, tmp_path):
    from orchestrator.permissions.frontend_requests import read
    service, authority, _, arguments, subject = environment
    binding = await service.open_execution(**arguments)
    scope = normalize_scope("file", str(tmp_path / "one.txt"), ("write",), cwd=tmp_path)
    request = authority.request_access(binding.context, [scope], purpose="Write")
    server = api(service, subject, tmp_path)
    for _ in range(2):
        response = await read(server, _Request({}, match_info={"session_id": subject.session_id},
            query={"agent_id": subject.agent_id, "context_generation": "1"}))
        result = json.loads(response.text)
        assert result["requests"][0]["request_id"] == request.request_id
        assert result["requests"][0]["revision"] == request.revision
        assert authority.decision_count(request.request_id) == 0
    changed = await read(server, _Request({}, match_info={"session_id": subject.session_id},
        query={"agent_id": subject.agent_id, "context_generation": "2"}))
    assert changed.status == 409
    server._strict_authenticated_owner = lambda req: None
    assert (await read(server, _Request({}, match_info={"session_id": subject.session_id}))).status == 401


@pytest.mark.asyncio
async def test_saved_service_without_loaded_frontend_is_unavailable_not_an_empty_authority(environment, tmp_path):
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from orchestrator.config_json import read_config_json, write_config_json
    from orchestrator.permissions.clients import service_clients
    from orchestrator.permissions.frontend_requests import read

    service, authority, _, arguments, subject = environment
    binding = await service.open_execution(**arguments)
    request = authority.request_access(
        binding.context, [normalize_scope("file", str(tmp_path / "pending.txt"), ("write",), cwd=tmp_path)],
        purpose="Write the pending test file",
    )
    server = api(service, subject, tmp_path)
    server.permission_control = None  # The shared frontend booted before installation.
    descriptor = {
        "version": 1, "instance_id": subject.instance_id,
        "pao_endpoint": r"\\.\pipe\LOCAL\HASHI.Permissions.TestsPao",
        "control_endpoint": r"\\.\pipe\LOCAL\HASHI.Permissions.TestsControl",
        "public_key": base64.b64encode(
            Ed25519PrivateKey.generate().public_key().public_bytes_raw(),
        ).decode(),
    }
    # This is a qualified public descriptor; construction does not connect or
    # start an installed service. Only the persisted configuration changes.
    assert len(service_clients(SimpleNamespace(
        instance_id=subject.instance_id, permission_service=descriptor,
    ))) == 2
    document = read_config_json(server.config_path)
    document["global"]["permission_service"] = descriptor
    write_config_json(server.config_path, document)
    before = server.config_path.read_bytes()
    response = await read(server, _Request({}, match_info={"session_id": subject.session_id},
        query={"agent_id": subject.agent_id, "context_generation": "1"}))
    assert response.status == 503
    assert json.loads(response.text) == {"ok": False, "error_code": "permission_requests_unavailable"}
    assert server.permission_control is None
    assert server.config_path.read_bytes() == before
    assert authority.get_request(binding.context, request.request_id).state == "pending"
    assert authority.decision_count(request.request_id) == 0


@pytest.mark.asyncio
async def test_unconfigured_frontend_stays_a_read_only_empty_view(environment, tmp_path):
    from orchestrator.permissions.frontend_requests import read

    service, _, _, _, subject = environment
    server = api(service, subject, tmp_path)
    server.permission_control = None
    before = server.config_path.read_bytes()
    response = await read(server, _Request({}, match_info={"session_id": subject.session_id},
        query={"agent_id": subject.agent_id, "context_generation": "1"}))
    assert response.status == 200
    assert json.loads(response.text) == {"ok": True, "requests": [], "next_before": None}
    assert server.config_path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["{invalid", '{"global":[]}'])
async def test_unknown_configuration_cannot_be_presented_as_no_pending_requests(environment, tmp_path, raw):
    from orchestrator.permissions.frontend_requests import read

    service, _, _, _, subject = environment
    server = api(service, subject, tmp_path)
    server.permission_control = None
    server.config_path.write_text(raw, encoding="utf-8")
    response = await read(server, _Request({}, match_info={"session_id": subject.session_id},
        query={"agent_id": subject.agent_id, "context_generation": "1"}))
    assert response.status == 503
    assert server.config_path.read_text(encoding="utf-8") == raw


@pytest.mark.asyncio
async def test_unloaded_frontend_checks_authentication_before_reading_installation(environment, tmp_path):
    from orchestrator.permissions.frontend_requests import read

    service, _, _, _, subject = environment
    server = api(service, subject, tmp_path)
    server.permission_control = None
    server._load_raw_agent_config = lambda: pytest.fail("configuration read before session authorization")
    server._strict_authenticated_owner = lambda request: None
    request = _Request({}, match_info={"session_id": subject.session_id},
        query={"agent_id": subject.agent_id, "context_generation": "1"})
    assert (await read(server, request)).status == 401
    server._strict_authenticated_owner = lambda request: subject.owner_id
    request.query["context_generation"] = "2"
    assert (await read(server, request)).status == 409
