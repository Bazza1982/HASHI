from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.config_json import read_config_json, write_config_json
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
    assert status["activation_required"] is None
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
async def test_shared_reference_and_missing_owner_fail_closed_before_verification(tmp_path):
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

    owner_server = _server(
        tmp_path / "owner",
        verifier=verify,
        configured=False,
        authorized_id=0,
    )
    owner_status = owner_server.telegram_credential_service.status("lily")
    assert owner_status["authorized_user_configured"] is False
    with pytest.raises(TelegramCredentialError) as owner_error:
        await owner_server.telegram_credential_service.replace(
            "lily",
            token=NEW_TOKEN,
            expected_revision=owner_status["credential_revision"],
            replace_existing=False,
        )
    assert owner_error.value.error_code == "telegram_owner_not_configured"
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
