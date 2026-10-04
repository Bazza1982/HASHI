"""Focused HTTP/PAO contracts for the October 4 HASHI1 repairs.

All servers, identities, credentials and durable sinks live under pytest temp
directories. No installed instance, external Telegram or model is contacted.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from orchestrator.function_worker_supervisor import FunctionWorkerSupervisor
from orchestrator.frontend_live_voice.protocol import LiveVoiceError
from orchestrator.telegram_ingress_diagnostics import TelegramIngressDiagnostics
from orchestrator.workbench_api import WorkbenchApiServer
from tests.test_live_voice_actions import (
    action, actual_admission_api, decision, phone, speak,
)

ADMIN_TOKEN = "isolated-workbench-admin-secret"
BOT_TOKEN = "123456789:AAisolatedTelegramCredentialNeverPublish"
PROVIDER_SECRET = "isolated-provider-secret-never-publish"


def _server(tmp_path: Path, *, profile="personal", token=ADMIN_TOKEN,
            config=None, global_values=None):
    raw = config or {"global": {}, "agents": [{"name": "lily"}]}
    config_path = tmp_path / "agents.json"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    global_config = SimpleNamespace(
        bridge_home=tmp_path, project_root=tmp_path, instance_id="HASHI1",
        authorized_id=7, workbench_port=18800, api_gateway_port=18801,
        deployment_profile=profile, organization_id="contract-org",
        **(global_values or {}),
    )
    runtime = SimpleNamespace(name="lily")
    return WorkbenchApiServer(
        config_path=config_path, global_config=global_config,
        runtimes=[runtime], secrets={"workbench_admin_token": token},
        reconcile_session_runs=False,
    )


def _diagnostics(server, tmp_path):
    """Use the production supervisor reader and its real durable JSONL sink."""
    worker = FunctionWorkerSupervisor.__new__(FunctionWorkerSupervisor)
    diagnostic = TelegramIngressDiagnostics(
        bridge_home=tmp_path, instance_id="HASHI1", agent="lily",
        generation_id="api-contract-generation", token=BOT_TOKEN,
    )
    worker._telegram_ingress_diagnostics = {"lily": diagnostic}
    server.orchestrator = SimpleNamespace(function_workers=worker, runtimes=server.runtimes)
    return diagnostic


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [
    {}, {"X-Workbench-Token": "wrong-token"},
    {"Authorization": "Bearer wrong-token"},
])
async def test_instance_identity_requires_real_admin_auth(tmp_path, headers):
    server = _server(tmp_path)
    async with TestClient(TestServer(server.app)) as client:
        response = await client.get("/api/v1/instance/identity", headers=headers)
        assert response.status == 401
        assert (await response.json())["error_code"] == "not_authenticated"


@pytest.mark.asyncio
async def test_instance_identity_fails_closed_without_configured_secret(tmp_path):
    server = _server(tmp_path, token="")
    # Other legacy personal endpoints allow no-token mode. Identity must not.
    async with TestClient(TestServer(server.app)) as client:
        response = await client.get("/api/v1/instance/identity")
        assert response.status == 401
        assert (await response.json())["error_code"] == "not_authenticated"


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["X-Workbench-Token", "Authorization"])
async def test_instance_identity_is_compact_authenticated_and_contains_no_secrets(tmp_path, header):
    server = _server(tmp_path)
    server.bound_port = 18833
    server.secrets.update(telegram_token=BOT_TOKEN, deepseek_key=PROVIDER_SECRET)
    value = ADMIN_TOKEN if header == "X-Workbench-Token" else f"Bearer {ADMIN_TOKEN}"
    async with TestClient(TestServer(server.app)) as client:
        response = await client.get("/api/v1/instance/identity", headers={header: value})
        body = await response.read()
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert len(body) <= 1024
        assert json.loads(body) == {
            "ok": True, "instance_id": "HASHI1", "workbench_port": 18833,
            "protocol": "hashi-instance-identity-v1",
        }
        for secret in (ADMIN_TOKEN, BOT_TOKEN, PROVIDER_SECRET):
            assert secret.encode() not in body


@pytest.mark.asyncio
async def test_telegram_diagnostics_http_reads_durable_redacted_facts_only(tmp_path):
    server = _server(tmp_path)
    old = _diagnostics(server, tmp_path)
    old.begin("get_updates", deadline=32)
    old.failure(RuntimeError(f"Bearer {PROVIDER_SECRET}; private chat text"),
                stage="get_updates", retry_seconds=2)
    path = tmp_path / old.relative_path
    assert path.is_file()
    disk = path.read_text(encoding="utf-8")
    assert "private chat text" not in disk
    assert PROVIDER_SECRET not in disk and BOT_TOKEN not in disk
    # A restart has no in-memory last_failure; HTTP still retrieves real facts.
    current = TelegramIngressDiagnostics(
        bridge_home=tmp_path, instance_id="HASHI1", agent="lily",
        generation_id="api-contract-restarted", token=BOT_TOKEN,
    )
    server.orchestrator.function_workers._telegram_ingress_diagnostics["lily"] = current
    assert current.snapshot()["last_failure"] is None
    # Even an appended raw/unknown extension must be sanitized at read boundary.
    record = {**old.identity, "event": "failure", "stage": "get_updates",
              "at": old.snapshot()["last_failure_at"],
              "error": {"type": "RuntimeError", "reason": "unknown",
                        "message": f"{BOT_TOKEN} injected private content"},
              "private": PROVIDER_SECRET}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")
        stream.write(json.dumps({**record, "agent": "foreign-agent"}) + "\n")
    async with TestClient(TestServer(server.app)) as client:
        denied = await client.get("/api/v1/agents/lily/telegram/diagnostics")
        assert denied.status == 401
        response = await client.get("/api/v1/agents/lily/telegram/diagnostics?limit=2",
                                    headers={"X-Workbench-Token": ADMIN_TOKEN})
        body = await response.read()
        payload = json.loads(body)
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert len(payload["records"]) == 2
        assert all(row["agent"] == "lily" for row in payload["records"])
        assert payload["records"][0]["generation_id"] == "api-contract-generation"
        assert payload["records"][0]["error"]["reason"] == "unknown"
        for secret in (ADMIN_TOKEN, BOT_TOKEN, PROVIDER_SECRET,
                       "private chat text", "injected private content", "foreign-agent"):
            assert secret.encode() not in body
        invalid = await client.get("/api/v1/agents/lily/telegram/diagnostics?limit=51",
                                   headers={"X-Workbench-Token": ADMIN_TOKEN})
        assert invalid.status == 400
        assert (await invalid.json())["error_code"] == "diagnostic_limit_invalid"


@pytest.mark.asyncio
async def test_telegram_diagnostics_no_token_personal_mode_cannot_read_private_sink(tmp_path):
    server = _server(tmp_path, token="")
    diagnostic = _diagnostics(server, tmp_path)
    diagnostic.begin("worker_delivery")
    diagnostic.failure(RuntimeError("private user update"), stage="worker_delivery", retry_seconds=2)
    async with TestClient(TestServer(server.app)) as client:
        response = await client.get("/api/v1/agents/lily/telegram/diagnostics")
        assert response.status == 401
        assert (await response.json())["error_code"] == "not_authenticated"


@pytest.mark.asyncio
async def test_governed_identity_and_diagnostics_require_authenticated_admin_not_just_owner(tmp_path):
    server = _server(tmp_path, profile="enterprise")
    _diagnostics(server, tmp_path)
    identity = server.identity_service
    admin = identity.bootstrap_org_admin(org_id="contract-org", org_name="Contract",
        email="admin@contract.invalid", display_name="Admin", password="fixture-password")
    member = identity.create_user(org_id="contract-org", email="member@contract.invalid",
        display_name="Member", password="fixture-password")
    admin_token = identity.create_session(user_id=admin.id).token
    member_token = identity.create_session(user_id=member.id).token
    async with TestClient(TestServer(server.app)) as client:
        anonymous = await client.get("/api/v1/agents/lily/telegram/diagnostics")
        assert anonymous.status == 401
        owner = await client.get("/api/v1/agents/lily/telegram/diagnostics",
                                 headers={"Authorization": f"Bearer {member_token}"})
        assert owner.status == 403
        assert (await owner.json())["error_code"] == "diagnostics_forbidden"
        member_identity = await client.get("/api/v1/instance/identity",
                                            headers={"Authorization": f"Bearer {member_token}"})
        assert member_identity.status == 401
        for route in ("/api/v1/agents/lily/telegram/diagnostics", "/api/v1/instance/identity"):
            response = await client.get(route, headers={"Authorization": f"Bearer {admin_token}"})
            assert response.status == 200
            body = await response.read()
            assert admin_token.encode() not in body and member_token.encode() not in body
        identity.revoke_session(admin_token)
        revoked = await client.get("/api/v1/instance/identity",
                                   headers={"Authorization": f"Bearer {admin_token}"})
        assert revoked.status == 401


def _template_server(tmp_path, *, secret_present):
    executable = tmp_path / "isolated-codex"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    providers = {"providers": {"deepseek": {"engine": "deepseek-api", "status": "stable",
                  "secret": "contract_deepseek_key", "models": ["deepseek-v4-pro"],
                  "default_model": "deepseek-v4-pro"}}}
    config = {"global": {"agent_creation": {"template_agent": "sunny", "grant_mode": "template"},
                         "codex_cmd": str(executable), "her_providers": providers},
              "agents": [{"name": "sunny", "active_backend": "codex-cli",
                          "allowed_backends": [
                              {"engine": "codex-cli", "model": "gpt-5.4", "models": ["gpt-5.4"],
                               "effort": "high", "model_efforts": {"gpt-5.4": ["high"]}},
                              {"engine": "her-v2", "her_v2": {"main": {
                                  "provider": "deepseek-api", "model": "deepseek-v4-pro"},
                                  "v3_provider_allowlist": ["deepseek-api"]}},
                          ]}]}
    if secret_present:
        (tmp_path / "secrets.json").write_text(json.dumps({"contract_deepseek_key": PROVIDER_SECRET}),
                                                encoding="utf-8")
    return _server(tmp_path, config=config, global_values={"her_providers": providers})


@pytest.mark.asyncio
@pytest.mark.parametrize("secret_present", [False, True])
async def test_backend_catalogue_http_effective_sunny_template_uses_real_availability(tmp_path, secret_present):
    server = _template_server(tmp_path, secret_present=secret_present)
    async with TestClient(TestServer(server.app)) as client:
        response = await client.get("/api/backends/catalogue")
        payload = await response.json()
        assert response.status == 200
        creation = payload["creation"]
        assert creation["ok"] is True
        assert (creation["source"], creation["template_agent"], creation["grant_mode"]) == (
            "instance_template", "sunny", "template")
        assert set(creation["backends"]) == {"codex-cli", "her-v3"}
        codex = creation["backends"]["codex-cli"]
        assert codex["available"] is True
        # Ordinary model selection retains the qualified compatibility
        # baseline plus template opt-ins; the template grants engines and
        # overrides named model efforts, not a whole-registry whitelist.
        assert "gpt-5.4" in codex["models"]
        assert codex["model_efforts"]["gpt-5.4"] == ["high"]
        her = creation["backends"]["her-v3"]
        provider = her["providers"]["deepseek-api"]
        assert provider["available"] is secret_present
        assert her["available"] is secret_present
        assert creation["allowed_backends"] == (["codex-cli", "her-v3"] if secret_present else ["codex-cli"])
        if not secret_present:
            assert provider["reason"] == "credential_missing"
            # Shared registry entry is not authority for a creation permission.
            assert payload["backends"]["her-v3"]["providers"]["deepseek-api"]["available"] is True
            denied = await client.post("/api/admin/add-agent",
                headers={"X-Workbench-Token": ADMIN_TOKEN}, json={"name": "unavailable-child",
                "backend": "her-v3", "provider": "deepseek-api", "model": "deepseek-v4-pro"})
            assert denied.status == 400
            assert (await denied.json())["error_code"] == "invalid_backend"
            assert not (tmp_path / "workspaces" / "unavailable-child").exists()
        body = json.dumps(payload)
        assert PROVIDER_SECRET not in body and "contract_deepseek_key" not in body


@pytest.mark.asyncio
async def test_backend_catalogue_http_missing_explicit_template_fails_closed(tmp_path):
    server = _server(tmp_path, config={"global": {"agent_creation": {
        "template_agent": "absent-sunny", "grant_mode": "template"}}, "agents": []})
    async with TestClient(TestServer(server.app)) as client:
        response = await client.get("/api/backends/catalogue")
        assert response.status == 200
        assert (await response.json())["creation"] == {
            "ok": False, "error_code": "creation_policy_invalid",
            "backends": {}, "allowed_backends": [],
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("restricted", [False, True])
async def test_creation_http_provider_and_restricted_intent_publish_real_scoped_config(tmp_path, restricted):
    server = _template_server(tmp_path, secret_present=True)
    async with TestClient(TestServer(server.app)) as client:
        response = await client.post("/api/admin/add-agent",
            headers={"X-Workbench-Token": ADMIN_TOKEN}, json={
                "name": "contract-child", "backend": "her-v3", "provider": "deepseek-api",
                "model": "deepseek-v4-pro", "effort": "high",
                "restricted": restricted, "is_active": False,
            })
        payload = await response.json()
        assert response.status == 201, payload
        assert payload["ok"] is True
        raw = json.loads(server.config_path.read_text(encoding="utf-8"))
        child = next(row for row in raw["agents"] if row["name"] == "contract-child")
        assert child["is_active"] is False
        assert child["active_backend"] == "her-v2"
        engines = {row["engine"] for row in child["allowed_backends"]}
        assert engines == ({"her-v2"} if restricted else {"codex-cli", "her-v2"})
        her = next(row for row in child["allowed_backends"] if row["engine"] == "her-v2")
        assert her["her_v2"]["main"] == {
            "provider": "deepseek-api", "model": "deepseek-v4-pro"}
        assert her["effort"] == "high"
        assert (tmp_path / child["workspace_dir"] / "agent.md").is_file()
        assert PROVIDER_SECRET not in json.dumps(payload)
        assert PROVIDER_SECRET not in json.dumps(child)
        # Being in the shared catalogue never grants a template-external engine.
        denied = await client.post("/api/admin/add-agent",
            headers={"X-Workbench-Token": ADMIN_TOKEN}, json={
                "name": "foreign-engine-child", "backend": "claude-cli", "is_active": False})
        assert denied.status == 400
        assert (await denied.json())["error_code"] == "invalid_backend"
        assert not (tmp_path / "workspaces" / "foreign-engine-child").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [{"restricted": "false"}, {"provider": ["deepseek-api"]}])
async def test_creation_http_new_intent_fields_are_typed_before_scaffold(tmp_path, invalid):
    server = _template_server(tmp_path, secret_present=True)
    async with TestClient(TestServer(server.app)) as client:
        response = await client.post("/api/admin/add-agent",
            headers={"X-Workbench-Token": ADMIN_TOKEN}, json={
                "name": "invalid-child", "backend": "her-v3", **invalid})
        assert response.status == 400
        assert (await response.json())["error_code"] == "invalid_request"
        assert not (tmp_path / "workspaces" / "invalid-child").exists()
        raw = json.loads(server.config_path.read_text(encoding="utf-8"))
        assert [row["name"] for row in raw["agents"]] == ["sunny"]


@pytest.mark.asyncio
@pytest.mark.parametrize("damage, expected_code", [
    ("missing", "phone_context_handoff_unavailable"),
    ("not_durable", "phone_context_handoff_unavailable"),
    ("wrong_digest", "phone_context_scope_changed"),
    ("wrong_version", "phone_context_scope_changed"),
    ("wrong_delegation", "phone_context_scope_changed"),
    ("wrong_call", "phone_context_scope_changed"),
    ("wrong_epoch", "phone_context_scope_changed"),
])
async def test_phone_api_admission_checks_frozen_snapshot_before_any_worker_call(phone, tmp_path, damage, expected_code):
    phone.judgments = [decision(action("query", "Read the scoped status"))]
    await speak(phone, "Read the scoped status", source="snapshot-contract")
    proposal = phone.admitted_proposals[-1]
    binding = phone.binding
    assert proposal.phone_context_handoff_id
    if damage == "missing": proposal = replace(proposal, phone_context_handoff_id="")
    elif damage == "not_durable": proposal = replace(proposal, phone_context_handoff_id="nonexistent-handoff")
    elif damage == "wrong_digest": proposal = replace(proposal, digest="wrong-frozen-digest")
    elif damage == "wrong_version": proposal = replace(proposal, version=proposal.version + 1)
    elif damage == "wrong_delegation": proposal = replace(proposal, delegation_id="wrong-delegation")
    elif damage == "wrong_call": binding = replace(binding, call_id="other-call")
    elif damage == "wrong_epoch": binding = replace(binding, call_epoch=binding.call_epoch + 1)
    server, runtime = actual_admission_api(phone, tmp_path)
    phone.store.bind_primary_session(owner_id=phone.owner_id, agent_id=phone.agent_id, session_id=phone.session_id)
    handle = server._runtime_map()[phone.agent_id]
    original_route = handle._route
    worker_calls = []
    async def count_route(_self, method, params):
        worker_calls.append(method)
        return await original_route(method, params)
    handle._route = MethodType(count_route, handle)
    before = len(phone.store.recent_session_runs(phone.session_id, owner_id=phone.owner_id))
    with pytest.raises(LiveVoiceError) as rejected:
        await server._admit_live_voice_run(binding, proposal, "snapshot-contract-invalid")
    assert rejected.value.code == expected_code
    assert rejected.value.status == 409
    assert worker_calls == []
    assert runtime.queue.empty()
    assert len(phone.store.recent_session_runs(phone.session_id, owner_id=phone.owner_id)) == before
