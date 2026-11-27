"""Instance-wide Telegram mirror switch: /telegram on|off global."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from orchestrator import ui_language
from orchestrator.commands import telegram as telegram_command_module
from orchestrator.config_json import read_config_json, write_config_json
from orchestrator.connector_delivery_preferences import (
    INSTANCE_SCOPE_OWNER,
    get_connector_preference,
    instance_telegram_mirror_enabled,
)
from orchestrator.frontend_delivery import (
    RUN_DELIVERY_ROUTE_METADATA_KEY,
    RUN_DELIVERY_ROUTE_TYPE,
    RUN_DELIVERY_ROUTE_VERSION,
    telegram_delivery_for_admission,
    telegram_mirror_allowed_for_run,
)


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
            instance_id="GLOBAL_MIRROR_TEST",
        ),
        _is_authorized_user=lambda actor: actor == 7,
        _reply_text=reply,
    )


def _update():
    return NS(effective_user=NS(id=7), _hashi_session_owner_id="user:123")


async def _run(runtime: NS, args: list[str]) -> None:
    with ui_language.language_scope(None, locale="en"):
        await telegram_command_module.telegram_command(
            runtime, _update(), NS(args=args)
        )


@pytest.mark.asyncio
async def test_global_off_overrides_always_on_and_restores_per_agent_settings(
    tmp_path,
):
    config_path = _write_instance(tmp_path)
    sent: list[str] = []
    runtime = _runtime(tmp_path, config_path, sent)

    admission = dict(
        request_metadata={"owner_id": "user:123"},
        state_root=tmp_path,
        agent_id="alpha",
        agent_config_path=config_path,
    )
    assert telegram_delivery_for_admission(source="workbench", **admission) is True

    await _run(runtime, ["off", "global"])

    assert instance_telegram_mirror_enabled(tmp_path) is False
    assert "disabled for every Agent" in sent[-1]
    assert "always on" in sent[-1]
    assert "2 Agents" in sent[-1]
    assert "1 set to always on" in sent[-1]
    # Only the mirror changes: origin-platform traffic keeps its own replies.
    assert telegram_delivery_for_admission(source="telegram", **admission) is True
    # An Agent pinned to always on no longer mirrors while the switch is off.
    assert telegram_delivery_for_admission(source="workbench", **admission) is False
    # The per-Agent settings themselves are untouched.
    policies = {
        row["name"]: row.get("telegram_mirror_policy")
        for row in read_config_json(config_path)["agents"]
    }
    assert policies == {"alpha": "always_on", "beta": None}
    # The switch survives a new runtime (restart) because it is persisted.
    assert instance_telegram_mirror_enabled(tmp_path) is False

    await _run(runtime, ["on", "global"])
    assert instance_telegram_mirror_enabled(tmp_path) is True
    assert "is enabled again" in sent[-1]
    assert telegram_delivery_for_admission(source="workbench", **admission) is True


@pytest.mark.asyncio
async def test_global_status_readback_reports_state_and_scope(tmp_path):
    config_path = _write_instance(tmp_path)
    sent: list[str] = []
    runtime = _runtime(tmp_path, config_path, sent)

    await _run(runtime, ["off", "global"])
    await _run(runtime, ["global"])
    assert "Instance-wide mirror: off" in sent[-1]
    assert "2 Agents" in sent[-1]
    assert "/telegram on|off [global]" in sent[-1]

    await _run(runtime, ["on", "global"])
    await _run(runtime, ["global"])
    assert "Instance-wide mirror: on" in sent[-1]


@pytest.mark.asyncio
async def test_global_switch_is_scoped_to_one_instance(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_config = _write_instance(first)
    second_config = _write_instance(second)
    sent: list[str] = []
    first_runtime = _runtime(first, first_config, sent)
    second_runtime = _runtime(second, second_config, sent)

    await _run(first_runtime, ["off", "global"])

    assert instance_telegram_mirror_enabled(first) is False
    assert instance_telegram_mirror_enabled(second) is True
    admission = dict(
        request_metadata={"owner_id": "user:123"},
        state_root=second,
        agent_id="alpha",
        agent_config_path=second_config,
    )
    assert telegram_delivery_for_admission(source="workbench", **admission) is True
    # The reserved instance key never masquerades as an owner preference.
    stored = json.loads(
        (first / "state" / "frontend_delivery_preferences.json").read_text(
            encoding="utf-8"
        )
    )
    assert INSTANCE_SCOPE_OWNER in stored["owners"]
    assert get_connector_preference(
        first, "user:123", "telegram", "mirror", default=True
    ) is True
    assert second_runtime.global_config.bridge_home == second


@pytest.mark.asyncio
async def test_global_off_stops_a_pending_mirror_of_a_frozen_run(tmp_path):
    config_path = _write_instance(tmp_path)
    sent: list[str] = []
    runtime = _runtime(tmp_path, config_path, sent)
    route = {
        "type": RUN_DELIVERY_ROUTE_TYPE,
        "version": RUN_DELIVERY_ROUTE_VERSION,
        "scope": "run",
        "primary": {"surface": "workbench", "channel_key": "user:123"},
        "mirrors": [{"surface": "telegram", "channel_key": "user:123"}],
        "automatic": True,
    }
    runtime._request_meta_by_id = {
        "req-global": {
            "owner_id": "user:123",
            "request_metadata": {RUN_DELIVERY_ROUTE_METADATA_KEY: route},
        }
    }
    assert telegram_mirror_allowed_for_run(runtime, "req-global") is True

    await _run(runtime, ["off", "global"])

    # The Run was admitted while mirroring was allowed; the not-yet-sent
    # mirror is suppressed immediately without re-admission.
    assert telegram_mirror_allowed_for_run(runtime, "req-global") is False

    await _run(runtime, ["on", "global"])
    assert telegram_mirror_allowed_for_run(runtime, "req-global") is True


@pytest.mark.asyncio
async def test_global_switch_requires_authorization(tmp_path):
    config_path = _write_instance(tmp_path)
    sent: list[str] = []
    runtime = _runtime(tmp_path, config_path, sent)
    runtime._is_authorized_user = lambda actor: False

    await _run(runtime, ["off", "global"])

    assert sent == []
    assert instance_telegram_mirror_enabled(tmp_path) is True
# HASHI-Fingerprint: hfp_f28b69420aee4262a951448b634f3ea4
