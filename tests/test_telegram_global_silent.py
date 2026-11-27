"""Instance-wide Telegram safety silence: /telegram silent global."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from orchestrator import telegram_silent, ui_language
from orchestrator.commands import telegram as telegram_command_module
from orchestrator.config_json import write_config_json
from orchestrator.connector_delivery_preferences import (
    instance_telegram_mirror_enabled,
    instance_telegram_silenced,
    state_path,
)
from orchestrator.frontend_delivery import (
    RUN_DELIVERY_ROUTE_METADATA_KEY,
    RUN_DELIVERY_ROUTE_TYPE,
    RUN_DELIVERY_ROUTE_VERSION,
    telegram_delivery_for_admission,
    telegram_mirror_allowed_for_run,
)
from orchestrator import runtime_delivery
from orchestrator import telegram_update_ack


def _write_instance(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "agents.json"
    write_config_json(
        config_path,
        {
            "global": {"authorized_id": 7},
            "agents": [
                {
                    "name": "alpha",
                    "workspace_dir": "workspaces/alpha",
                    "type": "flex",
                    "telegram_mirror_policy": "always_on",
                    "is_active": True,
                },
                {
                    "name": "beta",
                    "workspace_dir": "workspaces/beta",
                    "type": "flex",
                    "is_active": True,
                },
            ],
        },
    )
    return config_path


def _runtime(root: Path, config_path: Path, sent: list[str]) -> NS:
    async def reply(update, text, *, parse_mode=None, reply_markup=None):
        sent.append(text)

    return NS(
        name="alpha",
        global_config=NS(
            authorized_id=7,
            bridge_home=root,
            config_path=str(config_path),
            instance_id="SILENT_TEST",
        ),
        _is_authorized_user=lambda actor: actor == 7,
        _reply_text=reply,
    )


def _update():
    return NS(
        update_id=11,
        effective_user=NS(id=7),
        effective_chat=NS(id=7),
        _hashi_session_owner_id="user:123",
    )


async def _run(runtime: NS, args: list[str]) -> None:
    with ui_language.language_scope(None, locale="en"):
        await telegram_command_module.telegram_command(
            runtime, _update(), NS(args=args)
        )


@pytest.fixture(autouse=True)
def _clear_counters():
    telegram_silent.reset_counters()
    yield
    telegram_silent.reset_counters()


@pytest.mark.asyncio
async def test_silent_drops_every_inbound_update_and_still_acknowledges(
    tmp_path, monkeypatch
):
    config_path = _write_instance(tmp_path)
    runtime = _runtime(tmp_path, config_path, [])
    handled: list[object] = []

    async def fake_process(runtime_, update):
        handled.append(update)

    monkeypatch.setattr(
        "orchestrator.telegram_actor_scope.scoped_access_enabled", lambda runtime_: True
    )
    monkeypatch.setattr(
        "orchestrator.telegram_actor_scope.process_telegram_update", fake_process
    )

    await telegram_update_ack.dispatch_update(runtime, _update())
    assert len(handled) == 1
    handled.clear()

    await _run(runtime, ["silent", "global"])
    assert instance_telegram_silenced(tmp_path) is True

    # Dropping is explicit: returning normally acknowledges the update so the
    # Bot offset advances instead of building a backlog.
    await telegram_update_ack.dispatch_update(runtime, _update())
    await telegram_update_ack.dispatch_update(runtime, _update())
    assert handled == []
    assert telegram_silent.dropped_since_start(runtime) == 2
    assert "2" in (await _status(runtime))

    await _run(runtime, ["silent", "off", "global"])
    assert instance_telegram_silenced(tmp_path) is False
    await telegram_update_ack.dispatch_update(runtime, _update())
    assert len(handled) == 1


async def _status(runtime: NS) -> str:
    with ui_language.language_scope(None, locale="en"):
        return telegram_command_module._global_status_text(runtime)


@pytest.mark.asyncio
async def test_silent_suppresses_mirror_even_for_always_on_agents(tmp_path):
    config_path = _write_instance(tmp_path)
    runtime = _runtime(tmp_path, config_path, [])
    admission = dict(
        request_metadata={"owner_id": "user:123"},
        state_root=tmp_path,
        agent_id="alpha",
        agent_config_path=config_path,
    )
    assert telegram_delivery_for_admission(source="workbench", **admission) is True

    await _run(runtime, ["silent", "global"])

    assert telegram_delivery_for_admission(source="workbench", **admission) is False
    # Telegram-origin traffic is a primary route, never a mirror; the silence
    # itself removes it at the ingest point, not here.
    assert telegram_delivery_for_admission(source="telegram", **admission) is True
    assert instance_telegram_mirror_enabled(tmp_path) is True


@pytest.mark.asyncio
async def test_silent_stops_a_pending_mirror_of_a_frozen_run(tmp_path):
    config_path = _write_instance(tmp_path)
    runtime = _runtime(tmp_path, config_path, [])
    route = {
        "type": RUN_DELIVERY_ROUTE_TYPE,
        "version": RUN_DELIVERY_ROUTE_VERSION,
        "scope": "run",
        "primary": {"surface": "workbench", "channel_key": "user:123"},
        "mirrors": [{"surface": "telegram", "channel_key": "user:123"}],
        "automatic": True,
    }
    runtime._request_meta_by_id = {
        "req-silent": {
            "owner_id": "user:123",
            "request_metadata": {RUN_DELIVERY_ROUTE_METADATA_KEY: route},
        }
    }
    assert telegram_mirror_allowed_for_run(runtime, "req-silent") is True

    await _run(runtime, ["silent", "global"])

    assert telegram_mirror_allowed_for_run(runtime, "req-silent") is False


@pytest.mark.asyncio
async def test_silent_suppresses_outbound_delivery_at_send_time(tmp_path):
    config_path = _write_instance(tmp_path)
    runtime = _runtime(tmp_path, config_path, [])

    await _run(runtime, ["silent", "global"])

    duration, chunks = await runtime_delivery.send_long_message(
        runtime, chat_id=7, text="reply that must not leave", request_id="req-1"
    )
    assert (duration, chunks) == (0.0, 0)

    # The receipt announcing the change is the single exempt message.
    with telegram_silent.last_notice():
        assert telegram_silent.suppressed(runtime) is False
    assert telegram_silent.suppressed(runtime) is True


@pytest.mark.asyncio
async def test_on_global_clears_both_switches_and_off_global_keeps_silence(
    tmp_path,
):
    config_path = _write_instance(tmp_path)
    sent: list[str] = []
    runtime = _runtime(tmp_path, config_path, sent)

    await _run(runtime, ["off", "global"])
    await _run(runtime, ["silent", "global"])
    assert instance_telegram_silenced(tmp_path) is True

    # "off global" is the mirror switch only: it must not lift a safety cut.
    await _run(runtime, ["off", "global"])
    assert instance_telegram_silenced(tmp_path) is True
    assert instance_telegram_mirror_enabled(tmp_path) is False

    await _run(runtime, ["on", "global"])
    assert instance_telegram_silenced(tmp_path) is False
    assert instance_telegram_mirror_enabled(tmp_path) is True
    assert sent[-1].count("Safety silence") == 1


@pytest.mark.asyncio
async def test_silent_is_instance_wide_only(tmp_path):
    config_path = _write_instance(tmp_path)
    sent: list[str] = []
    runtime = _runtime(tmp_path, config_path, sent)

    await _run(runtime, ["silent"])
    assert instance_telegram_silenced(tmp_path) is False
    assert "silent global" in sent[-1]


@pytest.mark.asyncio
async def test_silence_is_scoped_to_one_instance(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_config = _write_instance(first)
    second_config = _write_instance(second)
    sent: list[str] = []
    first_runtime = _runtime(first, first_config, sent)
    second_runtime = _runtime(second, second_config, sent)

    await _run(first_runtime, ["silent", "global"])

    assert instance_telegram_silenced(first) is True
    assert instance_telegram_silenced(second) is False
    admission = dict(
        request_metadata={"owner_id": "user:123"},
        state_root=second,
        agent_id="alpha",
        agent_config_path=second_config,
    )
    assert telegram_delivery_for_admission(source="workbench", **admission) is True


@pytest.mark.asyncio
async def test_silence_survives_a_restart(tmp_path):
    config_path = _write_instance(tmp_path)
    await _run(_runtime(tmp_path, config_path, []), ["silent", "global"])

    # A new runtime over the same instance root reads the persisted switch.
    restarted = _runtime(tmp_path, config_path, [])
    assert telegram_silent.silenced(restarted) is True
    assert instance_telegram_silenced(tmp_path) is True


@pytest.mark.asyncio
async def test_damaged_preference_document_does_not_silence_the_instance(
    tmp_path,
):
    config_path = _write_instance(tmp_path)
    sent: list[str] = []
    runtime = _runtime(tmp_path, config_path, sent)
    target = state_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")

    # Reads fail open: a damaged store must not silence the instance by itself.
    assert telegram_silent.silenced(runtime) is False

    await _run(runtime, ["silent", "global"])
    assert instance_telegram_silenced(tmp_path) is False
    assert "could not be saved" in sent[-1]
    assert target.read_text(encoding="utf-8") == "{not json"


@pytest.mark.asyncio
async def test_silent_status_reports_state_and_scope(tmp_path):
    config_path = _write_instance(tmp_path)
    sent: list[str] = []
    runtime = _runtime(tmp_path, config_path, sent)

    await _run(runtime, ["silent", "global"])
    await _run(runtime, ["global"])

    status = sent[-1]
    assert "Safety silence: on (nothing in or out)" in status
    assert "every Telegram account" in status
    assert "Instance-wide mirror" in status
    stored = json.loads(state_path(tmp_path).read_text(encoding="utf-8"))
    assert stored["owners"]["__instance__"]["connectors"]["telegram"]["silent"] is True
# HASHI-Fingerprint: hfp_b24bfb63aa6e4792987fcf2e2d12df65
