from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator import runtime_session
from orchestrator.config_json import read_config_json, write_config_json
from orchestrator.telegram_actor_scope import process_telegram_update


def instance(tmp_path):
    config_path = tmp_path / "agents.json"
    secrets_path = tmp_path / "secrets.json"
    write_config_json(config_path, {
        "global": {"telegram_owner_id": 42, "owner_scope_id": "user:42",
                   "authorized_telegram_user_ids": [100]},
        "agents": [
            {"name": "lily", "extra": {"scoped_telegram_user_ids": [200]}},
            {"name": "momo", "extra": {}},
        ],
    })
    write_config_json(secrets_path, {})
    observations = []

    class App:
        async def process_update(self, update):
            await asyncio.sleep(0)
            observations.append((update.effective_user.id, runtime_session.owner_id(runtime)))
            await asyncio.sleep(0)

    runtime = SimpleNamespace(
        name="lily",
        global_config=SimpleNamespace(
            config_path=config_path, secrets_path=secrets_path,
            owner_scope_id="user:42", authorized_id=42,
        ),
        app=App(),
    )
    return runtime, observations, config_path


def update(user_id, chat_id=None, chat_type="private"):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id if chat_id is None else chat_id, type=chat_type),
    )


@pytest.mark.asyncio
async def test_owner_and_other_users_have_distinct_scopes_even_concurrently(tmp_path):
    runtime, observations, _ = instance(tmp_path)
    await asyncio.gather(
        process_telegram_update(runtime, update(42)),
        process_telegram_update(runtime, update(100)),
        process_telegram_update(runtime, update(200)),
    )
    scopes = dict(observations)
    assert scopes[42] == "user:42"
    assert len(set(scopes.values())) == 3
    assert runtime_session.owner_id(runtime) == "user:42"


@pytest.mark.asyncio
async def test_grants_are_current_and_local_grants_do_not_apply_to_other_agent(tmp_path):
    runtime, observations, config_path = instance(tmp_path)
    assert await process_telegram_update(runtime, update(100))
    config = read_config_json(config_path)
    config["global"]["authorized_telegram_user_ids"] = []
    write_config_json(config_path, config)
    assert not await process_telegram_update(runtime, update(100))
    runtime.name = "momo"
    assert not await process_telegram_update(runtime, update(200))
    assert [row[0] for row in observations] == [100]


@pytest.mark.asyncio
async def test_non_owner_requires_a_direct_private_chat(tmp_path):
    runtime, observations, _ = instance(tmp_path)
    assert not await process_telegram_update(runtime, update(100, chat_id=-100, chat_type="group"))
    assert not await process_telegram_update(runtime, update(200, chat_id=42))
    assert await process_telegram_update(runtime, update(42, chat_id=-100, chat_type="group"))
    assert [row[0] for row in observations] == [42]


@pytest.mark.asyncio
async def test_bad_configuration_fails_closed(tmp_path):
    runtime, observations, config_path = instance(tmp_path)
    config_path.write_text("{broken", encoding="utf-8")
    assert not await process_telegram_update(runtime, update(100))
    assert observations == []

@pytest.mark.asyncio
async def test_real_session_binding_keeps_different_users_separate(tmp_path):
    from orchestrator.session_store import SessionStore

    runtime, _observations, _ = instance(tmp_path)
    runtime.session_store = SessionStore(tmp_path / "sessions.sqlite", instance_id="test")
    selected = {}

    async def admit(update):
        actor = update.effective_user.id
        metadata = {"owner_id": runtime_session.owner_id(runtime)}
        session, owner, surface, channel = runtime_session.resolve_request_session(
            runtime, source="telegram", chat_id=actor, metadata=metadata,
        )
        selected[actor] = (session["session_id"], owner, surface, channel)

    runtime.app.process_update = admit
    for actor in (42, 100, 200, 100):
        assert await process_telegram_update(runtime, update(actor))
    assert len({selected[actor][0] for actor in (42, 100, 200)}) == 3
    assert selected[42][1] == "user:42"
    assert selected[100][2:] == ("telegram", "100")
    assert runtime_session.owner_id(runtime) == "user:42"

def test_delivery_follows_current_grants_and_private_endpoint(tmp_path):
    from orchestrator.telegram_actor_scope import delivery_allowed, runtime_snapshot, _delegated_owner

    runtime, _observations, config_path = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(runtime), 100)
    assert delivery_allowed(runtime, owner_id=owner, chat_id=100)
    assert not delivery_allowed(runtime, owner_id=owner, chat_id=200)
    config = read_config_json(config_path)
    config["global"]["authorized_telegram_user_ids"] = []
    write_config_json(config_path, config)
    assert not delivery_allowed(runtime, owner_id=owner, chat_id=100)
    assert not delivery_allowed(runtime, owner_id="telegram-user:malformed", chat_id=100)
    assert delivery_allowed(runtime, owner_id="user:42", chat_id=-100)


@pytest.mark.asyncio
async def test_unavailable_secrets_never_fall_back_to_stale_credentials(tmp_path):
    runtime, observations, _ = instance(tmp_path)
    runtime.secrets = {"authorized_telegram_id": 42}
    runtime.global_config.secrets_path.unlink()
    assert not await process_telegram_update(runtime, update(100))
    assert observations == []

@pytest.mark.asyncio
async def test_claimed_response_is_suppressed_after_revocation(tmp_path):
    from orchestrator import runtime_delivery
    from orchestrator.telegram_actor_scope import runtime_snapshot, _delegated_owner

    runtime, _observations, config_path = instance(tmp_path)
    owner = _delegated_owner(runtime_snapshot(runtime), 100)
    config = read_config_json(config_path)
    config["global"]["authorized_telegram_user_ids"] = []
    write_config_json(config_path, config)
    completions = []
    store = SimpleNamespace(complete_delivery_outbox=lambda **entry: completions.append(entry))
    result = await runtime_delivery.dispatch_claimed_telegram_event(
        runtime, chat_id=100, store=store,
        claim={"outbox_id": "ob", "lease_token": "lease"},
        frontend_owner_id=owner, request_id="r", purpose="response",
    )
    assert result == (0.0, 0)
    assert completions == [{
        "outbox_id": "ob", "lease_token": "lease",
        "status": "suppressed", "error_code": "telegram_access_revoked",
    }]


@pytest.mark.asyncio
async def test_legacy_grant_retains_original_session_and_group_access(tmp_path):
    runtime, observations, config_path = instance(tmp_path)
    config = read_config_json(config_path)
    config['agents'][0]['extra'] = {'authorized_telegram_ids': [200]}
    write_config_json(config_path, config)
    assert await process_telegram_update(runtime, update(200, chat_id=-100, chat_type='group'))
    assert observations == [(200, 'user:42')]
    assert not await process_telegram_update(runtime, update(200), expected_owner_id='other-owner')
    config = read_config_json(config_path)
    config['agents'][0]['extra']['authorized_telegram_ids'] = []
    write_config_json(config_path, config)
    assert not await process_telegram_update(runtime, update(200))
    assert len(observations) == 1
