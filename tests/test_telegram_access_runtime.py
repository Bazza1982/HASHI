from __future__ import annotations

from orchestrator.config_json import read_config_json, write_config_json
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from tests.test_telegram_actor_scope import instance


def test_runtime_uses_additive_live_scope_only_when_enabled(tmp_path):
    runtime, _observations, config_path = instance(tmp_path)
    runtime._authorized_telegram_ids = (42, 75)

    assert not FlexibleAgentRuntime._is_authorized_user(runtime, 100)
    assert FlexibleAgentRuntime._is_authorized_user(runtime, 75)

    runtime._telegram_scoped_access_enabled = True
    assert FlexibleAgentRuntime._is_authorized_user(runtime, 42)
    assert FlexibleAgentRuntime._is_authorized_user(runtime, 100)
    assert FlexibleAgentRuntime._is_authorized_user(runtime, 200)
    assert not FlexibleAgentRuntime._is_authorized_user(runtime, 75)

    runtime.name = "momo"
    assert FlexibleAgentRuntime._is_authorized_user(runtime, 100)
    assert not FlexibleAgentRuntime._is_authorized_user(runtime, 200)
    config = read_config_json(config_path)
    config["global"]["authorized_telegram_user_ids"] = []
    write_config_json(config_path, config)
    assert not FlexibleAgentRuntime._is_authorized_user(runtime, 100)
    assert FlexibleAgentRuntime._is_authorized_user(runtime, 42)


def test_runtime_fails_closed_when_live_configuration_breaks(tmp_path):
    runtime, _observations, config_path = instance(tmp_path)
    runtime._authorized_telegram_ids = (42, 100)
    runtime._telegram_scoped_access_enabled = True
    config_path.write_text("{broken", encoding="utf-8")
    assert not FlexibleAgentRuntime._is_authorized_user(runtime, 42)
    assert not FlexibleAgentRuntime._is_authorized_user(runtime, 100)


import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from orchestrator import runtime_session
from orchestrator.function_worker_host import FunctionWorkerHost
from tests.test_telegram_actor_scope import update


@pytest.mark.asyncio
async def test_worker_scope_is_guarded_until_enabled(tmp_path, monkeypatch):
    from telegram import Update

    runtime, _observations, _ = instance(tmp_path)
    runtime.app.bot = object()
    observed = []
    async def process(update):
        observed.append((update.effective_user.id, runtime_session.owner_id(runtime)))
    runtime.app.process_update = process
    host = FunctionWorkerHost.__new__(FunctionWorkerHost)
    host.runtime = runtime
    host.phase = "ACTIVE"
    host.accepting = True
    host.emit_metadata = AsyncMock()
    selected = [update(100)]
    monkeypatch.setattr(Update, "de_json", lambda payload, bot: selected[0])

    assert await host.handle_request("runtime.telegram_update", {"update": {}})
    assert observed == [(100, "user:42")]

    runtime._telegram_scoped_access_enabled = True
    selected[0] = update(999)
    assert await host.handle_request("runtime.telegram_update", {"update": {}})
    assert len(observed) == 1
    selected[0] = update(100)
    assert await host.handle_request("runtime.telegram_update", {"update": {}})
    assert observed[-1][0] == 100
    assert observed[-1][1].startswith("telegram-user:100:")
    assert runtime_session.owner_id(runtime) == "user:42"

@pytest.mark.asyncio
async def test_replayed_telegram_input_keeps_actor_scope_and_checks_persisted_owner(tmp_path, monkeypatch):
    from telegram import Update
    from orchestrator import telegram_freshness_gate
    from orchestrator.telegram_actor_scope import _delegated_owner, runtime_snapshot

    runtime, _observations, _ = instance(tmp_path)
    runtime._telegram_scoped_access_enabled = True
    runtime.app.bot = object()
    seen = []
    async def process(replayed):
        seen.append(runtime_session.owner_id(runtime))
    runtime.app.process_update = process
    monkeypatch.setattr(Update, "de_json", lambda payload, bot: update(100))
    payload = {"raw_update": {"update_id": 1}}
    record = {
        "payload": payload, "payload_digest": telegram_freshness_gate._payload_digest(payload),
        "confirmation_id": "confirm", "source_message_id": "message",
        "idempotency_key": "key",
        "owner_id": _delegated_owner(runtime_snapshot(runtime), 100),
    }
    await telegram_freshness_gate._replay_persisted_update(runtime, record)
    assert seen == [record["owner_id"]]
    record["owner_id"] = "user:42"
    with pytest.raises(telegram_freshness_gate.ConfirmationConflictError):
        await telegram_freshness_gate._replay_persisted_update(runtime, record)
    assert len(seen) == 1

@pytest.mark.asyncio
async def test_agent_scoped_user_can_chat_but_cannot_manage_other_agents(tmp_path):
    from tests.test_telegram_actor_scope import update
    from orchestrator.telegram_actor_scope import process_telegram_update

    runtime, observations, _ = instance(tmp_path)
    runtime._reply_text = AsyncMock()
    local = update(200)
    local.message = SimpleNamespace(text="/transfer momo")
    local.effective_message = local.message
    assert await process_telegram_update(runtime, local)
    assert observations == []
    assert runtime._reply_text.await_count == 1

    local.message.text = "Hello"
    assert await process_telegram_update(runtime, local)
    assert [actor for actor, _ in observations] == [200]

    global_user = update(100)
    global_user.message = SimpleNamespace(text="/transfer momo")
    global_user.effective_message = global_user.message
    assert await process_telegram_update(runtime, global_user)
    assert [actor for actor, _ in observations] == [200, 100]


@pytest.mark.asyncio
async def test_agent_scoped_conversation_buttons_cannot_target_another_chat(tmp_path):
    from tests.test_telegram_actor_scope import update
    from orchestrator.telegram_actor_scope import process_telegram_update

    runtime, observations, _ = instance(tmp_path)
    runtime._pending_voice = {
        "200": {"chat_id": 200},
        "100": {"chat_id": 100},
    }
    local = update(200)
    local.callback_query = SimpleNamespace(data="safevoice:yes:100", answer=AsyncMock())
    assert await process_telegram_update(runtime, local)
    assert observations == []
    assert local.callback_query.answer.await_count == 1

    local.callback_query.data = "safevoice:yes:200"
    assert await process_telegram_update(runtime, local)
    assert len(observations) == 1
    local.callback_query.data = "rq:que_" + "a" * 32 + ":0"
    assert await process_telegram_update(runtime, local)
    assert len(observations) == 2

    global_user = update(100)
    global_user.callback_query = SimpleNamespace(data="safevoice:yes:200", answer=AsyncMock())
    assert await process_telegram_update(runtime, global_user)
    assert len(observations) == 2
    assert global_user.callback_query.answer.await_count == 1


@pytest.mark.asyncio
async def test_scoped_start_welcomes_user_without_starting_other_agents(tmp_path):
    from tests.test_telegram_actor_scope import update
    from orchestrator.telegram_actor_scope import process_telegram_update

    runtime, observations, _ = instance(tmp_path)
    runtime._reply_text = AsyncMock()
    runtime.global_config.ui_language = "zh-CN"
    local = update(200)
    local.message = SimpleNamespace(text="/start")
    local.effective_message = local.message
    assert await process_telegram_update(runtime, local)
    assert observations == []
    assert runtime._reply_text.await_count == 1
    assert runtime._reply_text.await_args.args[0] is local
    assert "发送消息" in runtime._reply_text.await_args.args[1]
