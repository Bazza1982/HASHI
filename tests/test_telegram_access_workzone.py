from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_session, runtime_workzone
from orchestrator.config_json import read_config_json, write_config_json
from orchestrator.telegram_actor_scope import _delegated_owner, runtime_snapshot
from tests.test_runtime_workzone import _runtime
from tests.test_telegram_actor_scope import instance


def setup_scoped_workzone(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    fixture, _observations, config_path = instance(config_dir)
    runtime = _runtime(tmp_path / "runtime")
    runtime.name = "lily"
    runtime.global_config.config_path = config_path
    runtime.global_config.secrets_path = fixture.global_config.secrets_path
    runtime.global_config.authorized_id = 42
    runtime.global_config.owner_scope_id = "user:42"
    store = runtime_session.ensure_store(runtime)
    store.set_agent_workzone_slot(owner_id="user:42", agent_id="lily",
                                  slot_id="main", path=str(runtime.zone), enabled=True)
    snapshot = runtime_snapshot(runtime)
    return runtime, config_path, _delegated_owner(snapshot, 100), _delegated_owner(snapshot, 200)


def test_global_and_agent_users_inherit_only_the_selected_agents_workzone(tmp_path):
    runtime, config_path, global_owner, local_owner = setup_scoped_workzone(tmp_path)
    store = runtime_session.ensure_store(runtime)
    for owner in (global_owner, local_owner):
        state = runtime_session.agent_workzone_state(runtime, owner=owner)
        assert state["slots"][0]["path"] == str(runtime.zone.resolve())
        session = store.create_session(owner_id=owner, agent_id="lily", title="Telegram")
        admitted = runtime_session.session_workzone_state(runtime, session_id=session["session_id"])
        assert admitted["slots"][0]["path"] == str(runtime.zone.resolve())
    assert runtime_session.agent_workzone_state(runtime, owner=global_owner + "x")["slots"] == []

    runtime.name = "momo"
    assert runtime_session.agent_workzone_state(runtime, owner=local_owner)["slots"] == []
    runtime.name = "lily"
    config = read_config_json(config_path)
    config["global"]["authorized_telegram_user_ids"] = []
    write_config_json(config_path, config)
    assert runtime_session.agent_workzone_state(runtime, owner=global_owner)["slots"] == []


@pytest.mark.asyncio
async def test_global_user_manages_the_same_agent_workzone(tmp_path, monkeypatch):
    runtime, _config_path, global_owner, _local_owner = setup_scoped_workzone(tmp_path)
    runtime._is_authorized_user = lambda user_id: user_id == 100
    monkeypatch.setattr(runtime_workzone, "_current_session", lambda *_: {"owner_id": global_owner})
    update = SimpleNamespace(effective_user=SimpleNamespace(id=100),
                             effective_chat=SimpleNamespace(id=100))

    await runtime_workzone.cmd_workzone(runtime, update, SimpleNamespace(args=["all", "off"]))
    state = runtime_session.ensure_store(runtime).get_agent_workzone_set(
        owner_id="user:42", agent_id="lily"
    )
    assert state["slots"][0]["enabled"] is False


@pytest.mark.asyncio
async def test_agent_only_user_cannot_modify_workzone_by_direct_command(tmp_path, monkeypatch):
    runtime, _config_path, _global_owner, local_owner = setup_scoped_workzone(tmp_path)
    runtime._is_authorized_user = lambda user_id: user_id == 200
    monkeypatch.setattr(runtime_workzone, "_current_session", lambda *_: {"owner_id": local_owner})
    update = SimpleNamespace(effective_user=SimpleNamespace(id=200),
                             effective_chat=SimpleNamespace(id=200))

    await runtime_workzone.cmd_workzone(runtime, update, SimpleNamespace(args=["all", "off"]))
    state = runtime_session.ensure_store(runtime).get_agent_workzone_set(
        owner_id="user:42", agent_id="lily"
    )
    assert state["slots"][0]["enabled"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("downgraded", [False, True])
async def test_global_user_path_reply_uses_current_grant_and_agent_workzone(
    tmp_path, monkeypatch, downgraded
):
    from orchestrator import frontend_command_admission
    from tests.test_runtime_workzone import _CallbackQuery, _callback_update

    runtime, config_path, global_owner, _local_owner = setup_scoped_workzone(tmp_path)
    runtime._is_authorized_user = lambda user_id: user_id == 100
    runtime._backend_busy = lambda: True
    monkeypatch.setattr(runtime_workzone, "_current_session", lambda *_: {"owner_id": global_owner})
    monkeypatch.setattr(
        frontend_command_admission, "reserve_telegram_command_invocation",
        lambda *args, **kwargs: SimpleNamespace(state="reserved", complete=lambda _result: None),
    )
    directory = runtime.global_config.project_root / "shared"
    directory.mkdir()
    query = _CallbackQuery("wz:p:1:1", chat_id=100)
    query.from_user.id = 100
    await runtime_workzone.callback_workzone(runtime, _callback_update(query), SimpleNamespace())

    if downgraded:
        config = read_config_json(config_path)
        config["global"]["authorized_telegram_user_ids"] = []
        config["agents"][0]["extra"]["scoped_telegram_user_ids"] = [100]
        write_config_json(config_path, config)

    message = SimpleNamespace(message_id=702, text="shared",
                              reply_to_message=SimpleNamespace(message_id=700))
    reply = SimpleNamespace(effective_user=SimpleNamespace(id=100),
                            effective_chat=SimpleNamespace(id=100),
                            effective_message=message, message=message)
    assert await runtime_workzone.handle_pending_path_reply(runtime, reply)
    state = runtime_session.ensure_store(runtime).get_agent_workzone_set(
        owner_id="user:42", agent_id="lily"
    )
    added = [slot for slot in state["slots"] if slot["slot_id"] == "1"]
    if downgraded:
        assert added == []
    else:
        assert added[0]["path"] == str(directory.resolve())
