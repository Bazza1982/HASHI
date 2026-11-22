from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from orchestrator.telegram_access import access_snapshot
from orchestrator.telegram_actor_scope import _delegated_owner
from orchestrator.telegram_delivery_state import telegram_bot_fingerprint
from orchestrator.telegram_status_projection import TelegramStatusProjection


class _Bot:
    def __init__(self, bot_id):
        self.id = bot_id

    async def get_me(self):
        raise AssertionError("a status menu must not probe Telegram")


def _fixture(tmp_path):
    config_path = tmp_path / "agents.json"
    secrets_path = tmp_path / "secrets.json"
    config = {
        "global": {"authorized_id": 42},
        "agents": [
            {"name": "alpha", "scoped_telegram_user_ids": [99]},
            {"name": "beta"},
            {"name": "gamma", "telegram_mirror_policy": "always_off"},
        ],
    }
    secrets = {"alpha": "token-a", "beta": "token-b", "gamma": "token-c"}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    secrets_path.write_text(json.dumps(secrets), encoding="utf-8")
    projection = TelegramStatusProjection(config_path, secrets_path)
    assert projection.refresh_configuration()
    now = datetime.now(timezone.utc).isoformat()
    for name, token, bot_id in (("alpha", "token-a", 100), ("beta", "token-b", 100), ("gamma", "token-c", 200)):
        fingerprint = telegram_bot_fingerprint(token)
        ingress = SimpleNamespace(token=token, _initialized=True, bot=_Bot(bot_id),
                                  is_running=True, connected=True,
                                  diagnostics=SimpleNamespace(summary={"last_success_at": now}))
        handle = SimpleNamespace(
            client=SimpleNamespace(process=SimpleNamespace(is_alive=lambda: True)),
            metadata={"worker_accepting": True, "telegram_outbound_connected": True,
                      "telegram_bot_fingerprint": fingerprint},
        )
        projection.update_handle(name, handle)
        projection.update_ingress(name, ingress)
    return projection, config, secrets


def test_summary_deduplicates_verified_bots_and_isolates_authority(tmp_path):
    projection, config, secrets = _fixture(tmp_path)
    owner = projection.snapshot("user:42", actor_id=42, mirror_enabled=True)
    assert owner["configured_verified"] == 2
    assert owner["ready"] == 2
    assert owner["mirror_active"] == 1
    assert owner["not_ready"] == 0
    assert owner["unverified_bindings"] == 0

    delegated = _delegated_owner(access_snapshot(config, secrets, "alpha"), 99)
    scoped = projection.snapshot(delegated, actor_id=99, mirror_enabled=True)
    assert scoped["configured_verified"] == 1
    assert scoped["ready"] == 1
    assert projection.snapshot(delegated, actor_id=42, mirror_enabled=True)["configured_verified"] == 0
    assert projection.snapshot("user:42", actor_id=99, mirror_enabled=True)["configured_verified"] == 0
    assert projection.snapshot("unknown", actor_id=None, mirror_enabled=True)["configured_verified"] == 0
    assert all("token" not in json.dumps(projection.snapshot("user:42", actor_id=42, mirror_enabled=True))
               for _ in range(5))


def test_summary_tracks_disconnect_recovery_and_owner_mirror_without_probes(tmp_path):
    projection, _config, _secrets = _fixture(tmp_path)
    alpha, _ = projection._ingress["alpha"]
    beta, _ = projection._ingress["beta"]
    alpha.connected = False
    beta.connected = False
    alpha.diagnostics.summary.update(consecutive_failures=1, last_failure={
        "error": {"reason": "bot_identity_mismatch", "message": "private details"},
    })
    failed = projection.snapshot("user:42", actor_id=42, mirror_enabled=True)
    assert failed["ready"] == 1 and failed["not_ready"] == 1
    assert failed["failure_reasons"] == ["bot_identity_mismatch"]
    assert "private details" not in json.dumps(failed)
    alpha.connected = True
    alpha.diagnostics.summary["consecutive_failures"] = 0
    recovered = projection.snapshot("user:42", actor_id=42, mirror_enabled=False)
    assert recovered["ready"] == 2 and recovered["mirror_active"] == 0
    assert recovered["failure_reasons"] == []


async def _summary_command_fixture(tmp_path):
    from orchestrator.function_worker_host import WorkerKernelFacade
    from orchestrator.function_worker_supervisor import FunctionWorkerSupervisor

    projection, _config, _secrets = _fixture(tmp_path)
    handle = projection._handles["alpha"]
    client = SimpleNamespace(agent_name="alpha", process=handle.client.process)
    handle.client = client
    supervisor = object.__new__(FunctionWorkerSupervisor)
    supervisor.kernel = SimpleNamespace(
        paths=SimpleNamespace(bridge_home=tmp_path),
        global_cfg=SimpleNamespace(instance_id="NP-TEST"),
        _runtime_map=lambda: {"alpha": handle},
    )
    supervisor._telegram_summary_projection = projection
    calls = []

    class Peer:
        async def request(self, method, params, *, timeout):
            calls.append((method, dict(params)))
            return await supervisor.handle_worker_request(client, method, params)

    facade = object.__new__(WorkerKernelFacade)
    facade.peer = Peer()
    runtime = SimpleNamespace(
        name="alpha", orchestrator=facade, workspace_dir=tmp_path,
        global_config=SimpleNamespace(authorized_id=42, bridge_home=tmp_path, ui_language="en"),
        _is_authorized_user=lambda actor: actor == 42,
        _is_command_allowed=lambda name: name == "telegram",
    )
    return runtime, calls, supervisor, client


import pytest


@pytest.mark.asyncio
async def test_command_reads_one_scoped_summary_without_bot_or_worker_probes(tmp_path):
    from unittest.mock import AsyncMock
    from orchestrator.admin_local_testing import _execute_connector_mirror_command
    from orchestrator.commands.telegram import telegram_command
    from orchestrator import ui_language

    runtime, calls, supervisor, client = await _summary_command_fixture(tmp_path)
    config_bytes = (tmp_path / "agents.json").read_bytes()
    secret_bytes = (tmp_path / "secrets.json").read_bytes()
    with ui_language.language_scope(runtime, locale="en"):
        first = await _execute_connector_mirror_command(runtime, "telegram", [],
            session_metadata={"_hashi_owner_id": "user:42"})
        second = await _execute_connector_mirror_command(runtime, "telegram", [],
            session_metadata={"_hashi_owner_id": "user:42"})
    text = first["messages"][0]["text"]
    assert "configured 2, ready 2, mirror-ready 1, not ready 0" in text
    assert "does not prove message delivery" in text
    assert second["messages"][0]["text"]
    assert len(calls) == 2 and all(method == "core.telegram.summary" for method, _ in calls)
    assert (tmp_path / "agents.json").read_bytes() == config_bytes
    assert (tmp_path / "secrets.json").read_bytes() == secret_bytes
    assert "token-a" not in text and "token-c" not in text

    update = SimpleNamespace(effective_user=SimpleNamespace(id=42),
                             _hashi_session_owner_id="user:42", message=SimpleNamespace())
    runtime._reply_text = AsyncMock()
    with ui_language.language_scope(runtime, locale="en"):
        await telegram_command(runtime, update, SimpleNamespace(args=[]))
    assert "configured 2, ready 2" in runtime._reply_text.await_args.args[1]
    assert calls[-1][1]["actor_id"] == 42

    # A different actor cannot inherit the primary owner's aggregate.
    denied = await supervisor.handle_worker_request(client, "core.telegram.summary",
        {"owner_id": "user:42", "actor_id": 99})
    assert denied["configured_verified"] == 0


def test_summary_render_reports_safe_failure_and_rejects_invalid_counts(tmp_path):
    from orchestrator import ui_language
    from orchestrator.telegram_status_presentation import summary_facts

    projection, _config, _secrets = _fixture(tmp_path)
    alpha, _ = projection._ingress["alpha"]
    alpha.connected = False
    alpha.diagnostics.summary.update(consecutive_failures=1, last_failure={
        "error": {"reason": "bot_identity_mismatch", "message": "secret token"},
    })
    payload = projection.snapshot("user:42", actor_id=42, mirror_enabled=True)
    payload["instance"] = "<TEST>"
    with ui_language.language_scope(None, locale="zh-CN"):
        text = "\\n".join(summary_facts(payload))
        invalid = "\\n".join(summary_facts({**payload, "ready": 99}))
    assert "收信端与代理使用的机器人不一致" in text
    assert "&lt;TEST&gt;" in text and "secret token" not in text
    assert "快照版本" in text and "未核实最近送达回执" in text
    assert "状态暂不可用" in invalid


def test_summary_is_truthful_for_stale_identity_and_changed_config(tmp_path):
    projection, config, secrets = _fixture(tmp_path)
    alpha, _ = projection._ingress["alpha"]
    beta, _ = projection._ingress["beta"]
    alpha.diagnostics.summary["last_success_at"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    beta.connected = False
    result = projection.snapshot("user:42", actor_id=42, mirror_enabled=True)
    assert result["ready"] == 1
    assert result["not_ready"] == 1
    assert result["stale_bindings"] == 1

    gamma, _ = projection._ingress["gamma"]
    gamma._initialized = False
    projection._known_ids.clear()
    result = projection.snapshot("user:42", actor_id=42, mirror_enabled=True)
    assert result["configured_verified"] == 1
    assert result["unverified_bindings"] == 1

    delegated = _delegated_owner(access_snapshot(config, secrets, "alpha"), 99)
    config["agents"][0]["scoped_telegram_user_ids"] = []
    projection.config_path.write_text(json.dumps(config), encoding="utf-8")
    assert projection.snapshot(delegated, actor_id=99, mirror_enabled=True)["ok"] is False
    assert projection.refresh_configuration()
    assert projection.snapshot(delegated, actor_id=99, mirror_enabled=True)["configured_verified"] == 0
