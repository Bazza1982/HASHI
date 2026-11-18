from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import orchestrator.telegram_credential as telegram_credential_module
import orchestrator.workbench_api as workbench_api_module

from orchestrator.config_json import read_config_json, write_config_json
from orchestrator.connector_delivery_preferences import set_connector_preference
from orchestrator.telegram_credential import (
    TelegramCredentialError,
    TelegramCredentialService,
)
from orchestrator.workbench_api import WorkbenchApiServer


ADMIN_TOKEN = "test-admin-token"
OLD_TOKEN = "123456:abcdefghijklmnopqrstuvwxyz_ABCDEFGHI"
NEW_TOKEN = "654321:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghi"


class _Request:
    def __init__(self, *, agent_id="lily", payload=None, headers=None):
        self.match_info = {"agent_id": agent_id}
        self._payload = payload
        self.headers = headers or {}

    async def json(self):
        return self._payload


def _write_config(
    root: Path,
    *,
    authorized_id: int = 42,
    shared: bool = False,
    configured: bool = True,
) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    token_key = "shared.telegram" if shared else "lily.telegram"
    agents = [
        {
            "name": "lily",
            "workspace_dir": "workspaces/lily",
            "type": "flex",
            "telegram_token_key": token_key,
            "active_backend": "codex-cli",
            "allowed_backends": [{"engine": "codex-cli"}],
            "is_active": True,
        }
    ]
    if shared:
        agents.append(
            {
                "name": "momo",
                "workspace_dir": "workspaces/momo",
                "type": "flex",
                "telegram_token_key": token_key,
                "active_backend": "codex-cli",
                "allowed_backends": [{"engine": "codex-cli"}],
                "is_active": True,
            }
        )
    config_path = root / "agents.json"
    secrets_path = root / "secrets.json"
    write_config_json(
        config_path,
        {"global": {"authorized_id": authorized_id}, "agents": agents},
    )
    secrets = {
        "workbench_admin_token": ADMIN_TOKEN,
        "provider_api_key": "preserve-me",
    }
    if configured:
        secrets[token_key] = OLD_TOKEN
    write_config_json(secrets_path, secrets)
    return config_path, secrets_path


def _server(
    root: Path,
    *,
    verifier,
    configured: bool = True,
    shared: bool = False,
    authorized_id: int = 42,
    runtime_active: bool = False,
) -> WorkbenchApiServer:
    config_path, secrets_path = _write_config(
        root,
        configured=configured,
        shared=shared,
        authorized_id=authorized_id,
    )
    global_config = SimpleNamespace(
        authorized_id=authorized_id,
        deployment_profile="personal",
        bridge_home=root,
        project_root=root,
        workbench_port=18800,
        secrets_path=secrets_path,
        instance_id="TEST",
        her_providers={},
    )
    orchestrator = (
        SimpleNamespace(runtimes=[SimpleNamespace(name="lily")])
        if runtime_active
        else None
    )
    return WorkbenchApiServer(
        config_path=config_path,
        global_config=global_config,
        secrets={"workbench_admin_token": ADMIN_TOKEN},
        orchestrator=orchestrator,
        reconcile_session_runs=False,
        telegram_bot_verifier=verifier,
    )


def test_credential_status_ignores_unrelated_management_capacity(tmp_path):
    config_path, secrets_path = _write_config(tmp_path)
    config = read_config_json(config_path)
    config["global"]["telegram_users_per_scope_max"] = 0
    write_config_json(config_path, config)
    service = TelegramCredentialService(config_path=config_path, secrets_path=secrets_path)

    assert service.context("lily").authorized_user_configured

    config["global"]["authorized_id"] = "invalid"
    write_config_json(config_path, config)
    with pytest.raises(TelegramCredentialError):
        service.context("lily")


@pytest.mark.asyncio
async def test_status_and_replace_never_return_existing_or_new_token(tmp_path):
    verified = []

    async def verify(token):
        verified.append(token)
        return {"id": 123, "username": "safe_bot", "first_name": "Safe"}

    server = _server(tmp_path, verifier=verify, runtime_active=True)
    headers = {"X-Workbench-Token": ADMIN_TOKEN}

    status_response = await server.handle_admin_agent_telegram_credential(
        _Request(headers=headers)
    )
    assert status_response.status == 200
    assert status_response.headers["Cache-Control"] == "no-store"
    status = json.loads(status_response.text)
    assert status["configured"] is True
    assert status["runtime_active"] is True
    assert status["activation_required"] == "agent_restart"
    assert status["bidirectional_ready"] is False
    assert OLD_TOKEN not in status_response.text
    assert "token_key" not in status_response.text
    assert status["credential_revision"] != read_config_json(
        tmp_path / "secrets.json"
    ).revision

    replace_response = await server.handle_admin_agent_telegram_credential_replace(
        _Request(
            headers=headers,
            payload={
                "token": NEW_TOKEN,
                "expected_revision": status["credential_revision"],
                "replace_existing": True,
            },
        )
    )
    assert replace_response.status == 200
    assert replace_response.headers["Cache-Control"] == "no-store"
    result = json.loads(replace_response.text)
    assert result["configured"] is True
    assert result["replaced"] is True
    assert result["verified_bot"] == {
        "id": 123,
        "username": "safe_bot",
        "first_name": "Safe",
    }
    assert result["activation_required"] == "agent_restart"
    assert NEW_TOKEN not in replace_response.text
    assert "token_key" not in replace_response.text
    assert verified == [NEW_TOKEN]

    stored = read_config_json(tmp_path / "secrets.json")
    assert stored["lily.telegram"] == NEW_TOKEN
    assert stored["provider_api_key"] == "preserve-me"
    assert stored["workbench_admin_token"] == ADMIN_TOKEN
    assert result["credential_revision"] != stored.revision


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_save_automatically_requests_only_target_agent_activation(
    tmp_path, monkeypatch, configured
):
    async def verify(token):
        return {"id": 123, "username": "safe_bot", "first_name": "Safe"}

    server = _server(
        tmp_path, verifier=verify, runtime_active=True, configured=configured
    )
    runtime = server.orchestrator.runtimes[0]
    runtime.is_function_worker_proxy = True
    calls = []

    async def execute(target, command, **kwargs):
        assert read_config_json(tmp_path / "secrets.json")["lily.telegram"] == NEW_TOKEN
        calls.append((target, command, kwargs))
        return {"ok": True, "result": {"accepted": True, "record": {
            "id": "reboot-lily", "status": "accepted", "targets": ["lily"],
        }}}

    monkeypatch.setattr(workbench_api_module, "try_execute_slash_command_text", execute)
    result = await server.handle_admin_agent_telegram_credential_replace(_Request(
        headers={"X-Workbench-Token": ADMIN_TOKEN},
        payload={"token": NEW_TOKEN, "replace_existing": configured,
                 "expected_revision": server.telegram_credential_service.status("lily")["credential_revision"]},
    ))
    payload = json.loads(result.text)
    assert result.status == 200
    assert payload["activation_status"] == "activating"
    assert payload["activation_operation_id"] == "reboot-lily"
    assert payload["activation_required"] is None
    assert payload["bidirectional_ready"] is False
    assert len(calls) == 1 and calls[0][0] is runtime
    assert calls[0][1] == "/reboot min"
    assert NEW_TOKEN not in repr(calls)
    assert NEW_TOKEN not in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["busy", "exception", "ack_unknown"])
async def test_saved_credential_is_not_reported_connected_when_activation_fails(
    tmp_path, monkeypatch, caplog, failure
):
    async def verify(token):
        return {"id": 123, "username": "safe_bot", "first_name": "Safe"}

    server = _server(tmp_path, verifier=verify, runtime_active=True)
    server.orchestrator.runtimes[0].is_function_worker_proxy = True

    async def busy(*args, **kwargs):
        if failure == "exception":
            raise RuntimeError(NEW_TOKEN)  # Transport exceptions must never echo credentials.
        return {"ok": True, "result": {"accepted": False, "reason": failure}}

    monkeypatch.setattr(workbench_api_module, "try_execute_slash_command_text", busy)
    response = await server.handle_admin_agent_telegram_credential_replace(_Request(
        headers={"X-Workbench-Token": ADMIN_TOKEN},
        payload={"token": NEW_TOKEN, "replace_existing": True,
                 "expected_revision": server.telegram_credential_service.status("lily")["credential_revision"]},
    ))
    payload = json.loads(response.text)
    assert response.status == 200  # Saving committed; do not prompt a credential replay.
    assert read_config_json(tmp_path / "secrets.json")["lily.telegram"] == NEW_TOKEN
    assert payload["activation_status"] == "pending"
    assert payload["activation_error_code"] == (
        "AGENT_BUSY" if failure == "busy" else "telegram_activation_unavailable"
    )
    assert payload["bidirectional_ready"] is False
    assert NEW_TOKEN not in response.text
    assert NEW_TOKEN not in caplog.text

    saved_bytes = (tmp_path / "secrets.json").read_bytes()
    refreshed = await server.handle_admin_agent_telegram_credential(
        _Request(headers={"X-Workbench-Token": ADMIN_TOKEN})
    )
    refreshed_payload = json.loads(refreshed.text)
    assert refreshed_payload["activation_status"] == "pending"
    assert refreshed_payload["activation_required"] == "agent_restart"
    assert refreshed_payload["bidirectional_ready"] is False
    assert (tmp_path / "secrets.json").read_bytes() == saved_bytes
    assert NEW_TOKEN not in refreshed.text


@pytest.mark.asyncio
@pytest.mark.parametrize("outbound,running,inbound,current_bot,current_receiver,ready", [
    (True, False, False, True, True, False),
    (True, True, False, True, True, False),
    (False, True, True, True, True, False),
    (True, True, True, False, True, False),
    (True, True, True, True, False, False),
    (True, True, True, True, True, True),
])
async def test_status_requires_current_bot_and_both_live_directions(
    tmp_path, outbound, running, inbound, current_bot, current_receiver, ready
):
    from orchestrator.telegram_delivery_state import telegram_bot_fingerprint

    server = _server(tmp_path, verifier=None, runtime_active=True)
    server.orchestrator.runtimes[0].get_runtime_metadata = lambda: {
        "online": True,
        "telegram_bot_fingerprint": telegram_bot_fingerprint(OLD_TOKEN if current_bot else NEW_TOKEN),
        "telegram_outbound_connected": outbound,
        "telegram_ingress": {"running": running, "connected": inbound,
            "bot_fingerprint": telegram_bot_fingerprint(OLD_TOKEN if current_receiver else NEW_TOKEN)},
    }
    response = await server.handle_admin_agent_telegram_credential(
        _Request(headers={"X-Workbench-Token": ADMIN_TOKEN})
    )
    payload = json.loads(response.text)
    assert payload["bidirectional_ready"] is ready
    assert (payload["activation_status"] == "connected") is ready
    assert payload["inbound_connected"] is bool(current_bot and current_receiver and running and inbound)
    assert payload["outbound_connected"] is bool(current_bot and outbound)
    assert OLD_TOKEN not in response.text


@pytest.mark.asyncio
async def test_live_transport_requires_an_authorized_user_before_reporting_ready(tmp_path):
    from orchestrator.telegram_delivery_state import telegram_bot_fingerprint

    server = _server(tmp_path, verifier=None, runtime_active=True, authorized_id=0)
    fingerprint = telegram_bot_fingerprint(OLD_TOKEN)
    server.orchestrator.runtimes[0].get_runtime_metadata = lambda: {
        "online": True,
        "telegram_bot_fingerprint": fingerprint,
        "telegram_outbound_connected": True,
        "telegram_ingress": {"running": True, "connected": True, "bot_fingerprint": fingerprint},
    }
    response = await server.handle_admin_agent_telegram_credential(
        _Request(headers={"X-Workbench-Token": ADMIN_TOKEN})
    )
    payload = json.loads(response.text)
    assert payload["inbound_connected"] is True
    assert payload["outbound_connected"] is True
    assert payload["authorized_user_configured"] is False
    assert payload["bidirectional_ready"] is False
    assert payload["activation_status"] == "user_required"
    assert payload["activation_required"] is None


@pytest.mark.asyncio
async def test_missing_user_does_not_attempt_agent_activation(tmp_path, monkeypatch):
    from orchestrator import workbench_api

    server = _server(tmp_path, verifier=None, runtime_active=True, authorized_id=0)
    calls = []
    async def activation(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("activation should not be attempted without an authorized user")
    monkeypatch.setattr(workbench_api, "try_execute_slash_command_text", activation)
    revision = server.telegram_credential_service.status("lily")["credential_revision"]
    result = {"agent_id": "lily", "credential_revision": revision}
    await server._activate_saved_telegram_credential(
        _Request(headers={"X-Workbench-Token": ADMIN_TOKEN}), result
    )
    assert result["activation_status"] == "user_required"
    assert result["bidirectional_ready"] is False
    assert calls == []


@pytest.mark.asyncio
async def test_replace_preserves_explicit_runtime_principals_on_secret_write(
    tmp_path,
    monkeypatch,
):
    config_path, secrets_path = _write_config(tmp_path)
    remote_sid = "S-1-5-19"
    captured_sids = []
    real_write_config_json = telegram_credential_module.write_config_json

    def capture_secret_write(path, document, **kwargs):
        if Path(path).resolve() == secrets_path.resolve():
            captured_sids.append(
                tuple(kwargs.get("private_full_control_sids") or ())
            )
            # Keep this contract test portable: applying a Windows SID is only
            # meaningful on Windows, while the captured call is the invariant.
            kwargs["private_full_control_sids"] = ()
        return real_write_config_json(path, document, **kwargs)

    monkeypatch.setattr(
        telegram_credential_module,
        "write_config_json",
        capture_secret_write,
    )

    async def verify(_token):
        return {"id": 123, "username": "safe_bot"}

    service = TelegramCredentialService(
        config_path=config_path,
        secrets_path=secrets_path,
        verify_bot=verify,
        private_full_control_sids=(remote_sid,),
    )
    before = service.status("lily")

    await service.replace(
        "lily",
        token=NEW_TOKEN,
        expected_revision=before["credential_revision"],
        replace_existing=True,
    )

    assert captured_sids == [(remote_sid,)]


@pytest.mark.asyncio
async def test_server_resolves_remote_runtime_principal_only_at_credential_write(tmp_path, monkeypatch):
    config_path, secrets_path = _write_config(tmp_path)
    remote_sid = "S-1-5-19"
    stored = read_config_json(secrets_path)
    stored["hashi_remote_shared_token"] = "test-remote-shared-token"
    write_config_json(secrets_path, stored)
    resolved_roots = []

    def resolve_runtime_sids(root):
        resolved_roots.append(Path(root).resolve())
        return (remote_sid,)

    monkeypatch.setattr(
        workbench_api_module,
        "resolve_remote_runtime_sids",
        resolve_runtime_sids,
        raising=False,
    )
    monkeypatch.setattr(
        workbench_api_module, "_windows_remote_runtime_sids",
        lambda _root: pytest.fail("startup must not query the Remote principal"), raising=False,
    )
    captured_sids = []
    real_write = telegram_credential_module.write_config_json
    def capture(path, document, **kwargs):
        captured_sids.append(tuple(kwargs.get("private_full_control_sids") or ()))
        kwargs["private_full_control_sids"] = ()
        return real_write(path, document, **kwargs)
    monkeypatch.setattr(telegram_credential_module, "write_config_json", capture)
    async def verifier(_token):
        return {"id": 123}
    global_config = SimpleNamespace(
        authorized_id=42,
        deployment_profile="personal",
        bridge_home=tmp_path,
        project_root=tmp_path,
        workbench_port=18800,
        secrets_path=secrets_path,
        instance_id="TEST",
        her_providers={},
    )

    server = WorkbenchApiServer(
        config_path=config_path,
        global_config=global_config,
        secrets=dict(stored),
        reconcile_session_runs=False,
        telegram_bot_verifier=verifier,
    )

    assert resolved_roots == []
    status = server.telegram_credential_service.status("lily")
    assert resolved_roots == []
    await server.telegram_credential_service.replace(
        "lily", token=NEW_TOKEN, expected_revision=status["credential_revision"], replace_existing=True,
    )
    assert resolved_roots == [tmp_path.resolve()]
    assert captured_sids == [(remote_sid,)]


@pytest.mark.asyncio
async def test_remote_principal_failure_does_not_block_startup_or_write_an_unsafe_secret(tmp_path, monkeypatch):
    async def verify(_token):
        return {"id": 123}

    server = _server(tmp_path, verifier=verify)
    secrets = read_config_json(tmp_path / "secrets.json")
    secrets["hashi_remote_shared_token"] = "test-remote-shared-token"
    write_config_json(tmp_path / "secrets.json", secrets)
    def fail(_root):
        raise RuntimeError("principal lookup unavailable")
    monkeypatch.setattr(workbench_api_module, "resolve_remote_runtime_sids", fail, raising=False)
    status = server.telegram_credential_service.status("lily")
    before = (tmp_path / "secrets.json").read_bytes()
    with pytest.raises(TelegramCredentialError) as caught:
        await server.telegram_credential_service.replace(
            "lily", token=NEW_TOKEN, expected_revision=status["credential_revision"], replace_existing=True,
        )
    assert caught.value.error_code == "telegram_credential_save_failed"
    assert (tmp_path / "secrets.json").read_bytes() == before


@pytest.mark.skipif(os.name != "nt", reason="Remote principal lookup is Windows-specific")
@pytest.mark.asyncio
async def test_disabled_remote_does_not_require_a_windows_principal_for_credential_write(tmp_path, monkeypatch):
    from orchestrator import workbench_auth

    async def verify(_token):
        return {"id": 123}

    server = _server(tmp_path, verifier=verify)
    secrets = read_config_json(tmp_path / "secrets.json")
    secrets["hashi_remote_shared_token"] = "test-remote-shared-token"
    write_config_json(tmp_path / "secrets.json", secrets)
    remote_config = tmp_path / "remote" / "config.yaml"
    remote_config.parent.mkdir()
    remote_config.write_text("lifecycle:\n  remote_enabled: false\n  remote_supervised: false\n", encoding="utf-8")
    monkeypatch.setattr(
        workbench_auth, "_windows_remote_runtime_sids",
        lambda _root: pytest.fail("disabled Remote must not query Task Scheduler"),
    )
    status = server.telegram_credential_service.status("lily")
    result = await server.telegram_credential_service.replace(
        "lily", token=NEW_TOKEN, expected_revision=status["credential_revision"], replace_existing=True,
    )
    assert result["configured"] is True
    assert read_config_json(tmp_path / "secrets.json")["lily.telegram"] == NEW_TOKEN


def test_explicit_telegram_owner_supersedes_stale_secret_fallback(tmp_path):
    config_path, secrets_path = _write_config(tmp_path, authorized_id=8872)
    config = read_config_json(config_path)
    config["global"]["telegram_owner_id"] = 7666
    write_config_json(config_path, config)
    secrets = read_config_json(secrets_path)
    secrets["authorized_telegram_id"] = 8872
    write_config_json(secrets_path, secrets)

    service = TelegramCredentialService(
        config_path=config_path,
        secrets_path=secrets_path,
    )
    context = service.context("lily")

    assert context.authorized_owner_id == 7666
    assert service.status("lily")["authorized_user_configured"] is True


def test_status_uses_stable_pao_scope_for_instance_mirror_preference(tmp_path):
    config_path, secrets_path = _write_config(tmp_path, authorized_id=7666)
    config = read_config_json(config_path)
    config["global"].update(
        telegram_owner_id=7666,
        owner_scope_id="user:8872",
    )
    write_config_json(config_path, config)
    secrets = read_config_json(secrets_path)
    secrets["authorized_telegram_id"] = 8872
    write_config_json(secrets_path, secrets)
    set_connector_preference(
        tmp_path,
        "user:8872",
        "telegram",
        "mirror",
        False,
    )

    status = TelegramCredentialService(
        config_path=config_path,
        secrets_path=secrets_path,
    ).status("lily")

    assert status["instance_mirror_enabled"] is False
    assert status["resolved_mirror_enabled"] is False


@pytest.mark.asyncio
async def test_replace_rejects_stale_secret_revision_after_verification(tmp_path):
    config_path, secrets_path = _write_config(tmp_path)

    async def verify(_token):
        concurrent = read_config_json(secrets_path)
        concurrent["unrelated"] = "newer-value"
        write_config_json(secrets_path, concurrent)
        return {"id": 123, "username": "safe_bot"}

    service = TelegramCredentialService(
        config_path=config_path,
        secrets_path=secrets_path,
        verify_bot=verify,
    )
    before = service.status("lily")

    with pytest.raises(TelegramCredentialError) as captured:
        await service.replace(
            "lily",
            token=NEW_TOKEN,
            expected_revision=before["credential_revision"],
            replace_existing=True,
        )

    assert captured.value.error_code == "credential_state_changed"
    stored = read_config_json(secrets_path)
    assert stored["lily.telegram"] == OLD_TOKEN
    assert stored["unrelated"] == "newer-value"


@pytest.mark.asyncio
@pytest.mark.parametrize("token_first", [True, False])
async def test_token_and_user_can_be_saved_independently_in_either_order(
    tmp_path, monkeypatch, token_first,
):
    from orchestrator.telegram_access import TelegramAccessService

    async def verify(_token):
        return {"id": 123, "username": "safe_bot"}

    server = _server(tmp_path, verifier=verify, configured=False, authorized_id=0,
                     runtime_active=True)
    calls = []
    async def activate(runtime, command, **kwargs):
        calls.append((runtime.name, command))
        return {"ok": True, "result": {"accepted": True,
                "record": {"id": "activation-test", "status": "accepted"}}}
    monkeypatch.setattr(workbench_api_module, "try_execute_slash_command_text", activate)
    server.orchestrator.runtimes = [SimpleNamespace(
        name="lily", is_function_worker_proxy=True,
        get_runtime_metadata=lambda: {"telegram_scoped_access_protocol": 3,
                                     "worker_phase": "ACTIVE", "worker_accepting": True},
    )]
    users = TelegramAccessService(config_path=tmp_path / "agents.json",
                                  secrets_path=tmp_path / "secrets.json")
    headers = {"X-Workbench-Token": ADMIN_TOKEN}

    async def add_user():
        status = server.telegram_access_service.status("lily")
        response = await server.handle_admin_agent_telegram_access_change(_Request(
            headers=headers, payload={"expected_revision": status["revision"],
                                      "scope": "global", "action": "add", "user_id": "42"},
        ))
        assert response.status == 200

    if not token_first:
        await add_user()
        assert server.telegram_credential_service.status("lily")["configured"] is False
    status = server.telegram_credential_service.status("lily")
    response = await server.handle_admin_agent_telegram_credential_replace(_Request(
        headers=headers, payload={"token": NEW_TOKEN,
                                  "expected_revision": status["credential_revision"],
                                  "replace_existing": False},
    ))
    assert response.status == 200
    saved = json.loads(response.text)
    assert saved["configured"] is True
    assert saved["bidirectional_ready"] is False
    assert NEW_TOKEN not in response.text
    if token_first:
        assert users.status("lily")["primary_user_id"] is None
        assert saved["activation_status"] == "user_required"
        assert saved["activation_required"] is None
        assert calls == []
        await add_user()
    assert calls == [("lily", "/reboot min")]
    assert users.status("lily")["primary_user_id"] is None
    assert read_config_json(tmp_path / "secrets.json")["lily.telegram"] == NEW_TOKEN
    assert server.telegram_credential_service.status("lily")["authorized_user_configured"] is True


@pytest.mark.asyncio
async def test_shared_reference_fails_closed_before_verification(tmp_path):
    calls = []

    async def verify(token):
        calls.append(token)
        return {"id": 123}

    shared_server = _server(tmp_path / "shared", verifier=verify, shared=True)
    shared_status = shared_server.telegram_credential_service.status("lily")
    assert shared_status["manageable"] is False
    assert shared_status["shared_reference"] is True
    with pytest.raises(TelegramCredentialError) as shared_error:
        await shared_server.telegram_credential_service.replace(
            "lily",
            token=NEW_TOKEN,
            expected_revision=shared_status["credential_revision"],
            replace_existing=True,
        )
    assert shared_error.value.error_code == "telegram_credential_shared"

    assert calls == []


@pytest.mark.asyncio
async def test_endpoint_requires_admin_auth_and_rejects_unknown_fields(tmp_path):
    async def verify(_token):
        raise AssertionError("invalid requests must not verify a token")

    server = _server(tmp_path, verifier=verify)
    denied = await server.handle_admin_agent_telegram_credential(_Request())
    assert denied.status == 403
    assert json.loads(denied.text)["error_code"] == "admin_auth_failed"

    invalid = await server.handle_admin_agent_telegram_credential_replace(
        _Request(
            headers={"X-Workbench-Token": ADMIN_TOKEN},
            payload={
                "token": NEW_TOKEN,
                "expected_revision": "absent",
                "replace_existing": True,
                "unsafe": "field",
            },
        )
    )
    assert invalid.status == 400
    assert json.loads(invalid.text)["error_code"] == "invalid_request"
    assert NEW_TOKEN not in invalid.text

    revision = server.telegram_credential_service.status("lily")[
        "credential_revision"
    ]
    invalid_token = await server.handle_admin_agent_telegram_credential_replace(
        _Request(
            headers={"X-Workbench-Token": ADMIN_TOKEN},
            payload={
                "token": "not-a-telegram-token",
                "expected_revision": revision,
                "replace_existing": True,
            },
        )
    )
    assert invalid_token.status == 400
    assert json.loads(invalid_token.text)["error_code"] == "telegram_token_invalid"

    unconfirmed_replace = (
        await server.handle_admin_agent_telegram_credential_replace(
            _Request(
                headers={"X-Workbench-Token": ADMIN_TOKEN},
                payload={
                    "token": NEW_TOKEN,
                    "expected_revision": revision,
                    "replace_existing": False,
                },
            )
        )
    )
    assert unconfirmed_replace.status == 409
    assert (
        json.loads(unconfirmed_replace.text)["error_code"]
        == "telegram_replace_confirmation_required"
    )


@pytest.mark.asyncio
async def test_agent_mirror_policy_is_revision_checked_and_applies_without_restart(tmp_path):
    async def verify(_token):
        raise AssertionError("mirror policy updates must not inspect the Bot token")

    server = _server(tmp_path, verifier=verify, runtime_active=True)
    headers = {"X-Workbench-Token": ADMIN_TOKEN}
    initial = server.telegram_credential_service.status(
        "lily", runtime_active=True
    )
    assert initial["mirror_policy"] == "inherit"
    assert initial["instance_mirror_enabled"] is True
    assert initial["resolved_mirror_enabled"] is True
    assert initial["mirror_delivery_ready"] is True
    assert len(initial["mirror_policy_revision"]) == 64

    disabled_response = await server.handle_admin_agent_telegram_mirror_policy_update(
        _Request(
            headers=headers,
            payload={
                "policy": "always_off",
                "expected_revision": initial["mirror_policy_revision"],
            },
        )
    )
    assert disabled_response.status == 200
    disabled = json.loads(disabled_response.text)
    assert disabled["mirror_policy"] == "always_off"
    assert disabled["resolved_mirror_enabled"] is False
    assert disabled["activation_required"] is None
    assert disabled["runtime_active"] is True
    stored = read_config_json(tmp_path / "agents.json")
    assert stored["agents"][0]["telegram_mirror_policy"] == "always_off"

    stale = await server.handle_admin_agent_telegram_mirror_policy_update(
        _Request(
            headers=headers,
            payload={
                "policy": "always_on",
                "expected_revision": initial["mirror_policy_revision"],
            },
        )
    )
    assert stale.status == 409
    assert json.loads(stale.text)["error_code"] == "telegram_mirror_policy_changed"

    set_connector_preference(tmp_path, "user:42", "telegram", "mirror", False)
    enabled_response = await server.handle_admin_agent_telegram_mirror_policy_update(
        _Request(
            headers=headers,
            payload={
                "policy": "always_on",
                "expected_revision": disabled["mirror_policy_revision"],
            },
        )
    )
    enabled = json.loads(enabled_response.text)
    assert enabled_response.status == 200
    assert enabled["instance_mirror_enabled"] is False
    assert enabled["mirror_policy"] == "always_on"
    assert enabled["resolved_mirror_enabled"] is True


@pytest.mark.asyncio
async def test_always_on_still_reports_missing_telegram_delivery_prerequisites(tmp_path):
    async def verify(_token):
        raise AssertionError("mirror policy updates must not verify a token")

    server = _server(
        tmp_path,
        verifier=verify,
        configured=False,
        authorized_id=0,
    )
    status = server.telegram_credential_service.status("lily")
    result = server.telegram_credential_service.update_mirror_policy(
        "lily",
        policy="always_on",
        expected_revision=status["mirror_policy_revision"],
    )
    assert result["mirror_policy"] == "always_on"
    assert result["resolved_mirror_enabled"] is True
    assert result["mirror_delivery_ready"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_unverified_telegram_draft_survives_restart_and_verifies(
    tmp_path, monkeypatch, caplog, configured
):
    online = False
    calls = []

    async def verify(token):
        calls.append(token)
        if not online:
            raise TelegramCredentialError(
                "Offline", error_code="telegram_verification_unavailable", status_code=502
            )
        return {"id": 654321, "username": "fake_bot"}

    server = _server(tmp_path, verifier=verify, configured=configured, runtime_active=True)
    activations = []

    async def activate(_request, result):
        activations.append(result["agent_id"])

    monkeypatch.setattr(server, "_activate_saved_telegram_credential", activate)
    headers = {"X-Workbench-Token": ADMIN_TOKEN}
    revision = server.telegram_credential_service.status("lily")["credential_revision"]
    saved = await server.handle_admin_agent_telegram_credential_replace(_Request(
        headers=headers,
        payload={"token": NEW_TOKEN, "replace_existing": configured,
                 "expected_revision": revision, "save_unverified": True},
    ))
    assert saved.status == 200
    payload = json.loads(saved.text)
    assert payload["pending_verification"] is True
    assert payload["configured"] is configured
    assert not calls and not activations
    assert NEW_TOKEN not in saved.text and NEW_TOKEN not in caplog.text
    stored = read_config_json(tmp_path / "secrets.json")
    assert stored.get("lily.telegram") == (OLD_TOKEN if configured else None)
    assert stored["provider_api_key"] == "preserve-me"

    # Reconstruct the service, as happens on a server restart.
    server.telegram_credential_service = TelegramCredentialService(
        config_path=tmp_path / "agents.json",
        secrets_path=tmp_path / "secrets.json",
        state_root=tmp_path,
        verify_bot=verify,
    )
    status = server.telegram_credential_service.status("lily", runtime_active=True)
    assert status["pending_verification"] is True
    assert status["bidirectional_ready"] is False
    assert NEW_TOKEN not in repr(status)

    failed = await server.handle_admin_agent_telegram_credential_replace(_Request(
        headers=headers,
        payload={"expected_revision": status["credential_revision"], "verify_pending": True},
    ))
    assert failed.status == 502
    assert json.loads(failed.text)["error_code"] == "telegram_verification_unavailable"
    assert not activations
    assert server.telegram_credential_service.status("lily")["pending_verification"] is True
    assert read_config_json(tmp_path / "secrets.json").get("lily.telegram") == (
        OLD_TOKEN if configured else None
    )

    online = True
    verified = await server.handle_admin_agent_telegram_credential_replace(_Request(
        headers=headers,
        payload={"expected_revision": status["credential_revision"], "verify_pending": True},
    ))
    assert verified.status == 200
    result = json.loads(verified.text)
    assert result["pending_verification"] is False
    assert result["verified_bot"]["id"] == 654321
    assert result["bidirectional_ready"] is False
    assert activations == ["lily"]
    assert read_config_json(tmp_path / "secrets.json")["lily.telegram"] == NEW_TOKEN
    assert read_config_json(tmp_path / "secrets.json")["provider_api_key"] == "preserve-me"
    assert NEW_TOKEN not in verified.text and NEW_TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_pending_telegram_draft_does_not_bypass_revision_or_shared_reference(tmp_path):
    async def verify_with_concurrent_edit(token):
        secrets_path = tmp_path / "secrets.json"
        latest = read_config_json(secrets_path)
        latest["provider_api_key"] = "newer-value"
        write_config_json(secrets_path, latest)
        return {"id": 654321, "username": "fake_bot"}

    server = _server(tmp_path, verifier=verify_with_concurrent_edit)
    headers = {"X-Workbench-Token": ADMIN_TOKEN}
    revision = server.telegram_credential_service.status("lily")["credential_revision"]
    saved = await server.handle_admin_agent_telegram_credential_replace(_Request(
        headers=headers,
        payload={"token": NEW_TOKEN, "replace_existing": True,
                 "expected_revision": revision, "save_unverified": True},
    ))
    assert saved.status == 200
    revision = json.loads(saved.text)["credential_revision"]
    raced = await server.handle_admin_agent_telegram_credential_replace(_Request(
        headers=headers,
        payload={"expected_revision": revision, "verify_pending": True},
    ))
    assert raced.status == 409
    assert json.loads(raced.text)["error_code"] == "credential_state_changed"
    stored = read_config_json(tmp_path / "secrets.json")
    assert stored["lily.telegram"] == OLD_TOKEN
    assert stored["provider_api_key"] == "newer-value"
    assert server.telegram_credential_service.status("lily")["pending_verification"] is True

    shared_server = _server(tmp_path / "shared", verifier=verify_with_concurrent_edit, shared=True)
    shared_revision = shared_server.telegram_credential_service.status("lily")["credential_revision"]
    shared = await shared_server.handle_admin_agent_telegram_credential_replace(_Request(
        headers=headers,
        payload={"token": NEW_TOKEN, "replace_existing": True,
                 "expected_revision": shared_revision, "save_unverified": True},
    ))
    assert shared.status == 409
    assert json.loads(shared.text)["error_code"] == "telegram_credential_shared"


@pytest.mark.asyncio
async def test_default_verifier_uses_agent_api_origin_and_configured_proxy_route(
    tmp_path, monkeypatch
):
    config_path, secrets_path = _write_config(tmp_path, configured=False)
    config = read_config_json(config_path)
    config["agents"][0]["extra"] = {"telegram_api_url": "http://127.0.0.1:8989"}
    write_config_json(config_path, config)
    observed = {}

    class Response:
        status = 200
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return False
        async def json(self):
            return {"ok": True, "result": {"id": 654321, "is_bot": True}}

    class Client:
        def __init__(self, **kwargs):
            observed["trust_env"] = kwargs.get("trust_env")
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            return False
        def get(self, url, **kwargs):
            observed["url"] = url
            observed["redirects"] = kwargs.get("allow_redirects")
            return Response()

    monkeypatch.setattr(telegram_credential_module.aiohttp, "ClientSession", Client)
    service = TelegramCredentialService(config_path=config_path, secrets_path=secrets_path)
    result = await service.replace(
        "lily", token=NEW_TOKEN, expected_revision=service.status("lily")["credential_revision"],
        replace_existing=False,
    )
    assert observed["trust_env"] is True
    assert observed["url"] == f"http://127.0.0.1:8989/bot{NEW_TOKEN}/getMe"
    assert observed["redirects"] is False
    assert result["verified_bot"]["id"] == 654321
    assert NEW_TOKEN not in repr(result)


@pytest.mark.asyncio
async def test_draft_save_preserves_live_existing_bot_status(tmp_path, monkeypatch):
    from orchestrator.telegram_delivery_state import telegram_bot_fingerprint

    async def never_verify(_token):
        raise AssertionError("saving a draft must not contact Telegram")

    server = _server(tmp_path, verifier=never_verify, runtime_active=True)
    fingerprint = telegram_bot_fingerprint(OLD_TOKEN)
    server.orchestrator.runtimes[0].get_runtime_metadata = lambda: {
        "online": True,
        "telegram_bot_fingerprint": fingerprint,
        "telegram_outbound_connected": True,
        "telegram_ingress": {
            "bot_fingerprint": fingerprint, "running": True, "connected": True,
        },
    }
    calls = []
    async def activate(*_args):
        calls.append(True)
    monkeypatch.setattr(server, "_activate_saved_telegram_credential", activate)
    status = server.telegram_credential_service.status("lily")
    response = await server.handle_admin_agent_telegram_credential_replace(_Request(
        headers={"X-Workbench-Token": ADMIN_TOKEN},
        payload={"token": NEW_TOKEN, "replace_existing": True, "save_unverified": True,
                 "expected_revision": status["credential_revision"]},
    ))
    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["pending_verification"] is True
    assert payload["activation_status"] == "connected"
    assert payload["bidirectional_ready"] is True  # This is the existing verified bot.
    assert read_config_json(tmp_path / "secrets.json")["lily.telegram"] == OLD_TOKEN
    assert not calls
    assert NEW_TOKEN not in response.text


@pytest.mark.asyncio
async def test_draft_is_bound_to_agent_incarnation_not_reused_name(tmp_path):
    calls = []
    async def verify(token):
        calls.append(token)
        return {"id": 654321}

    server = _server(tmp_path, verifier=verify)
    headers = {"X-Workbench-Token": ADMIN_TOKEN}
    saved = await server.handle_admin_agent_telegram_credential_replace(_Request(
        headers=headers,
        payload={"token": NEW_TOKEN, "replace_existing": True, "save_unverified": True,
                 "expected_revision": server.telegram_credential_service.status("lily")["credential_revision"]},
    ))
    assert saved.status == 200
    config = read_config_json(tmp_path / "agents.json")
    original_lifecycle = config["agents"][0]["agent_lifecycle_id"]
    assert len(original_lifecycle) == 32
    config["agents"][0]["agent_lifecycle_id"] = (
        "a" * 32 if original_lifecycle != "a" * 32 else "b" * 32
    )
    write_config_json(tmp_path / "agents.json", config)
    current_revision = server.telegram_credential_service.status("lily")["credential_revision"]
    attempted = await server.handle_admin_agent_telegram_credential_replace(_Request(
        headers=headers, payload={"expected_revision": current_revision, "verify_pending": True},
    ))
    assert attempted.status == 409
    assert json.loads(attempted.text)["error_code"] == "credential_state_changed"
    assert calls == []
    assert read_config_json(tmp_path / "secrets.json")["lily.telegram"] == OLD_TOKEN
    assert NEW_TOKEN not in attempted.text
