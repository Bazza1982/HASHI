"""Review/CAS, quiesce, revocation, adoption and session-revocation boundaries."""
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from orchestrator.config_admin import ConfigAdmin
from orchestrator.config_json import new_config_json, write_config_json
from orchestrator.permissions.ipc_rpc import PermissionPipeClient
from orchestrator.permissions.level_transition import FrontendLevelTransitions, RemoteLevelTransition
from orchestrator.permissions.management import PermissionManagement
from orchestrator.permissions.models import PermissionSettings
from orchestrator.permissions.wire import PermissionServiceRouter, decode, request_frame

from tests.test_permission_service_application import environment as environment


@pytest.mark.parametrize("revoke_session", [False, True])
@pytest.mark.asyncio
async def test_level_change_reviews_then_quiesces_before_cas_and_never_writes_after_session_revocation(
    environment, tmp_path, monkeypatch, revoke_session,
):
    service, authority, identity, open_arguments, subject = environment
    binding = await service.open_execution(**open_arguments)
    session = identity.create_session(user_id="human")
    config_path = tmp_path / "agents.json"
    config_doc = new_config_json(config_path, {"agents": [
        {"name": subject.agent_id, "permission_settings": {"requested_level": "L1"}},
    ]})
    write_config_json(config_path, config_doc)
    global_config = SimpleNamespace(config_path=config_path)
    admin = ConfigAdmin(global_config)
    original = config_path.read_bytes()
    runtimes = {subject.agent_id: SimpleNamespace(worker_pid=111)}
    calls = []

    class Lifecycle:
        async def stop_agent(self, agent_id, *, reason):
            assert agent_id == subject.agent_id and reason == "permission-level-change"
            assert config_path.read_bytes() == original
            calls.append("stop")
            runtimes.clear()
            if revoke_session:
                identity.revoke_session(session.token)
            return True, "stopped"

        async def start_agent(self, agent_id):
            assert calls == ["stop"]
            calls.append("start")
            snapshot = admin.read_agent_permission_settings(agent_id)
            runtimes[agent_id] = SimpleNamespace(
                worker_pid=222, agent_config=SimpleNamespace(permission_settings=asdict(snapshot.settings)),
            )
            return True, "ready"

    private = tmp_path / "fc-private"
    private.mkdir()
    monkeypatch.setattr("orchestrator.permissions.level_transition.require_private_path", lambda *a, **k: True)
    fc = FrontendLevelTransitions(
        SimpleNamespace(global_config=global_config, session_store=service.store, orchestrator=Lifecycle(),
                        _runtime_map=lambda: runtimes),
        private_root=private, trusted_sid="test-only",
    )
    router = PermissionServiceRouter(fc, methods=fc.METHODS)

    class Loopback(PermissionPipeClient):
        def __init__(self):
            pass

        async def call_async(self, method, arguments):
            # Real closed codec/role dispatch, no native identity assertion.
            response = await router.dispatch_async(request_frame(method, arguments, request_id=method))
            return decode(response["result"])

    transition = RemoteLevelTransition(
        Loopback(), run_service=service.runs, generation="service-generation", levels=("L1",),
        policies=SimpleNamespace(preview=lambda *args: {"baseline_scopes": [], "requestable_scopes": [],
                                                       "installation_revision": "base-1", "native_tools": []}),
    )
    manager = PermissionManagement(
        authority, identity, owner_bindings=service.owners, config_admin=admin,
        run_service=service.runs, level_transition=transition,
    )
    review = await manager.manage(subject, ["set-level", "L0"], session_token=session.token, invocation_id="review")
    assert review["kind"] == "level_review" and not calls
    assert config_path.read_bytes() == original
    with pytest.raises(ValueError):
        await manager.manage(subject, ["set-level", "L0", "--revision", "0" * 64],
                             session_token=session.token, invocation_id="stale")
    assert not calls
    command = ["set-level", "L0", "--revision", review["revision"]]
    if revoke_session:
        with pytest.raises(ValueError):
            await manager.manage(subject, command, session_token=session.token, invocation_id="confirm")
        assert calls == ["stop"] and config_path.read_bytes() == original
        assert not runtimes
    else:
        result = await manager.manage(subject, command, session_token=session.token, invocation_id="confirm")
        assert result["kind"] == "level_changed" and result["adopted"] is True
        assert calls == ["stop", "start"]
        assert admin.read_agent_permission_settings(subject.agent_id).settings == PermissionSettings()
        assert authority.execution_status(binding.context).binding_state == "fenced"
