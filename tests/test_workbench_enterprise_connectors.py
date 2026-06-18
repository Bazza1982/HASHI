from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.enterprise import ConnectorHealth, ConnectorResult, EnterpriseRole, PolicyEvaluator
from orchestrator.workbench_api import WorkbenchApiServer


class _FakeRequest:
    def __init__(
        self,
        *,
        headers: dict | None = None,
        path: str = "",
        query: dict | None = None,
        body: dict | None = None,
        match_info: dict | None = None,
    ):
        self.headers = headers or {}
        self.path = path
        self.query = query or {}
        self._body = body or {}
        self.match_info = match_info or {}

    async def json(self):
        return self._body


class _FakeConnector:
    connector_type = "github"

    def health_check(self):
        return ConnectorHealth(ok=True, status="healthy", message="ready", data={"mode": "test"})

    def execute(self, action):
        raise AssertionError("not used")


class _RecordingConnector:
    connector_type = "github"

    def __init__(self):
        self.actions = []

    def health_check(self):
        return ConnectorHealth(ok=True, status="healthy")

    def execute(self, action):
        self.actions.append(action)
        return ConnectorResult(ok=True, status="success", message="done", data={"resource": action.resource})


def _server(tmp_path: Path, *, connectors: list | None = None, secrets: dict | None = None) -> WorkbenchApiServer:
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {"deployment_profile": "enterprise", "organization_id": "ORG-001"},
                "agents": [],
            }
        ),
        encoding="utf-8",
    )
    global_config = SimpleNamespace(
        deployment_profile="enterprise",
        organization_id="ORG-001",
        bridge_home=tmp_path,
        workbench_port=18800,
        project_root=tmp_path,
    )
    return WorkbenchApiServer(config_path=config_path, global_config=global_config, connectors=connectors, secrets=secrets)


def _admin_headers(server: WorkbenchApiServer) -> dict[str, str]:
    admin = server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
        user_id="usr-admin",
    )
    session = server.identity_service.create_session(user_id=admin.id)
    return {"Authorization": f"Bearer {session.token}"}


def _create_github_credential(server: WorkbenchApiServer):
    return server.connector_credentials.create_credential(
        org_id="ORG-001",
        connector_type="github",
        display_name="GitHub App",
        secret_ref="vault://github/app",
        scopes=["repo:read"],
        credential_id="cred-github",
    )


def test_workbench_connector_ui_includes_channel_presets():
    app_source = (Path(__file__).resolve().parents[1] / "workbench" / "src" / "App.jsx").read_text()

    assert "teams: {" in app_source
    assert "displayName: 'Teams Webhook'" in app_source
    assert "secretRef: 'env://TEAMS_WEBHOOK_URL'" in app_source
    assert '<option value="teams">Teams</option>' in app_source

    assert "feishu: {" in app_source
    assert "displayName: 'Feishu Webhook'" in app_source
    assert "secretRef: 'env://FEISHU_WEBHOOK_URL'" in app_source
    assert '<option value="feishu">Feishu</option>' in app_source
    assert "api('/api/enterprise/connectors/action-schemas')" in app_source
    assert "connector-schema-panel" in app_source
    assert "validateConnectorParameters" in app_source
    assert "Connector parameters failed schema validation." in app_source


@pytest.mark.asyncio
async def test_enterprise_admin_can_read_connector_health(tmp_path):
    server = _server(tmp_path, connectors=[_FakeConnector()])
    headers = _admin_headers(server)

    response = await server.handle_enterprise_connector_health(
        _FakeRequest(headers=headers, path="/api/enterprise/connectors/health")
    )

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["ok"] is True
    assert payload["healthy"] is True
    assert payload["count"] == 1
    assert payload["connectors"][0] == {
        "connector_type": "github",
        "ok": True,
        "status": "healthy",
        "message": "ready",
        "data": {"mode": "test"},
    }
    events = server.audit_ledger.query(event_type="connector")
    assert len(events) == 1
    assert events[0].action == "github.health_check"


@pytest.mark.asyncio
async def test_enterprise_connector_health_requires_admin(tmp_path):
    server = _server(tmp_path, connectors=[_FakeConnector()])
    server.identity_service.bootstrap_org_admin(
        org_id="ORG-001",
        org_name="Acme",
        email="admin@example.com",
        display_name="Admin",
        password="secret-password",
    )
    user = server.identity_service.create_user(
        org_id="ORG-001",
        email="user@example.com",
        display_name="User",
        password="secret-password",
        user_id="usr-user",
    )
    server.identity_service.assign_project_role(
        user_id=user.id,
        project_id="ORG-001-default",
        role=EnterpriseRole.INDIVIDUAL_USER,
    )
    session = server.identity_service.create_session(user_id=user.id)

    response = await server.handle_enterprise_connector_health(
        _FakeRequest(
            headers={"Authorization": f"Bearer {session.token}"},
            path="/api/enterprise/connectors/health",
        )
    )

    assert response.status == 403
    assert json.loads(response.text)["ok"] is False


@pytest.mark.asyncio
async def test_enterprise_admin_can_read_connector_action_schemas(tmp_path):
    server = _server(tmp_path)
    headers = _admin_headers(server)

    response = await server.handle_enterprise_connector_schemas(
        _FakeRequest(headers=headers, path="/api/enterprise/connectors/action-schemas")
    )

    payload = json.loads(response.text)
    schemas = {(item["connector_type"], item["action"]): item for item in payload["schemas"]}
    assert response.status == 200
    assert payload["ok"] is True
    assert payload["count"] == len(payload["schemas"])
    assert ("feishu", "message.send") in schemas
    assert ("github", "pr.merge") in schemas
    pull_number = [
        item for item in schemas[("github", "pr.merge")]["parameters"] if item["name"] == "pull_number"
    ][0]
    assert pull_number["required"] is True
    assert pull_number["type"] == "integer"


@pytest.mark.asyncio
async def test_enterprise_connector_action_schemas_require_admin(tmp_path):
    server = _server(tmp_path)

    response = await server.handle_enterprise_connector_schemas(
        _FakeRequest(headers={}, path="/api/enterprise/connectors/action-schemas")
    )

    assert response.status == 403
    assert json.loads(response.text)["ok"] is False


@pytest.mark.asyncio
async def test_enterprise_admin_can_create_list_and_revoke_connector_credentials(tmp_path):
    server = _server(tmp_path)
    headers = _admin_headers(server)

    create_response = await server.handle_enterprise_connector_credentials_create(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/credentials",
            body={
                "connector_type": "github",
                "display_name": "GitHub App",
                "secret_ref": "vault://github/app",
                "scopes": ["repo:read"],
                "credential_id": "cred-github",
            },
        )
    )
    create_payload = json.loads(create_response.text)
    assert create_response.status == 201
    assert create_payload["ok"] is True
    assert create_payload["credential"]["status"] == "active"
    assert create_payload["credential"]["secret_ref"] == "vault://github/app"

    list_response = await server.handle_enterprise_connector_credentials(
        _FakeRequest(headers=headers, path="/api/enterprise/connectors/credentials")
    )
    list_payload = json.loads(list_response.text)
    assert list_response.status == 200
    assert list_payload["count"] == 1
    assert list_payload["credentials"][0]["id"] == "cred-github"

    revoke_response = await server.handle_enterprise_connector_credential_revoke(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/credentials/cred-github/revoke",
            match_info={"credential_id": "cred-github"},
        )
    )
    revoke_payload = json.loads(revoke_response.text)
    assert revoke_response.status == 200
    assert revoke_payload["credential"]["status"] == "revoked"

    active_response = await server.handle_enterprise_connector_credentials(
        _FakeRequest(headers=headers, path="/api/enterprise/connectors/credentials")
    )
    assert json.loads(active_response.text)["count"] == 0

    all_response = await server.handle_enterprise_connector_credentials(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/credentials",
            query={"include_revoked": "true"},
        )
    )
    assert json.loads(all_response.text)["count"] == 1


@pytest.mark.asyncio
async def test_enterprise_connector_credential_create_rejects_unsupported_type(tmp_path):
    server = _server(tmp_path)
    headers = _admin_headers(server)

    response = await server.handle_enterprise_connector_credentials_create(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/credentials",
            body={
                "connector_type": "jira",
                "display_name": "Jira",
                "secret_ref": "env://JIRA_TOKEN",
                "scopes": ["issue.read"],
            },
        )
    )

    payload = json.loads(response.text)
    assert response.status == 400
    assert payload["ok"] is False
    assert "unsupported connector_type" in payload["error"]
    assert server.connector_credentials.list_credentials(org_id="ORG-001") == []


@pytest.mark.asyncio
async def test_enterprise_connector_credential_create_validates_scope_and_secret_ref(tmp_path):
    server = _server(tmp_path)
    headers = _admin_headers(server)

    missing_scope = await server.handle_enterprise_connector_credentials_create(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/credentials",
            body={
                "connector_type": "slack",
                "display_name": "Slack Webhook",
                "secret_ref": "env://SLACK_WEBHOOK_URL",
                "scopes": ["chat:write"],
            },
        )
    )
    bad_secret_ref = await server.handle_enterprise_connector_credentials_create(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/credentials",
            body={
                "connector_type": "slack",
                "display_name": "Slack Webhook",
                "secret_ref": "plain-secret-name",
                "scopes": ["message.send"],
            },
        )
    )

    missing_scope_payload = json.loads(missing_scope.text)
    bad_secret_ref_payload = json.loads(bad_secret_ref.text)
    assert missing_scope.status == 400
    assert "slack credentials require scope message.send" == missing_scope_payload["error"]
    assert bad_secret_ref.status == 400
    assert "secret_ref must use env://" in bad_secret_ref_payload["error"]
    assert server.connector_credentials.list_credentials(org_id="ORG-001") == []


@pytest.mark.asyncio
async def test_enterprise_connector_credential_create_accepts_comma_separated_scopes(tmp_path):
    server = _server(tmp_path)
    headers = _admin_headers(server)

    response = await server.handle_enterprise_connector_credentials_create(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/credentials",
            body={
                "connector_type": "teams",
                "display_name": "Teams Webhook",
                "secret_ref": "env://TEAMS_WEBHOOK_URL",
                "scopes": "message.send, extra.scope",
                "credential_id": "cred-teams",
            },
        )
    )

    payload = json.loads(response.text)
    assert response.status == 201
    assert payload["credential"]["scopes"] == ["extra.scope", "message.send"]


@pytest.mark.asyncio
async def test_enterprise_connector_credential_create_refreshes_registry_from_secret_ref(tmp_path):
    server = _server(tmp_path, secrets={"github_token": "ghp-test"})
    headers = _admin_headers(server)

    create_response = await server.handle_enterprise_connector_credentials_create(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/credentials",
            body={
                "connector_type": "github",
                "display_name": "GitHub App",
                "secret_ref": "secrets://github_token",
                "scopes": ["repo:read"],
                "credential_id": "cred-github",
            },
        )
    )

    assert create_response.status == 201
    assert server.connector_registry.list_types() == ["github"]
    assert server.connector_registry_errors == []

    revoke_response = await server.handle_enterprise_connector_credential_revoke(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/credentials/cred-github/revoke",
            match_info={"credential_id": "cred-github"},
        )
    )

    assert revoke_response.status == 200
    assert server.connector_registry.list_types() == []


@pytest.mark.asyncio
async def test_enterprise_connector_registry_refresh_reports_unresolved_secret(tmp_path):
    server = _server(tmp_path)
    headers = _admin_headers(server)

    response = await server.handle_enterprise_connector_credentials_create(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/credentials",
            body={
                "connector_type": "github",
                "display_name": "GitHub App",
                "secret_ref": "secrets://missing_github_token",
                "scopes": ["repo:read"],
                "credential_id": "cred-github",
            },
        )
    )

    assert response.status == 201
    assert server.connector_registry.list_types() == []
    assert server.connector_registry_errors == [
        {
            "credential_id": "cred-github",
            "connector_type": "github",
            "error": "HASHI secret is not set: missing_github_token",
        }
    ]


@pytest.mark.asyncio
async def test_enterprise_admin_can_execute_slack_dry_run_from_secret_ref(tmp_path):
    server = _server(tmp_path, secrets={"slack_webhook": "https://hooks.slack.test/services/abc"})
    headers = _admin_headers(server)
    create_response = await server.handle_enterprise_connector_credentials_create(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/credentials",
            body={
                "connector_type": "slack",
                "display_name": "Slack Webhook",
                "secret_ref": "secrets://slack_webhook",
                "scopes": ["message.send"],
                "credential_id": "cred-slack",
            },
        )
    )
    PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001").add_rule(
        action="connector.execute",
        resource="connector:slack:message.send",
        effect="allow",
        rule_id="pol-allow-slack-message",
    )

    response = await server.handle_enterprise_connector_execute(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/execute",
            body={
                "connector_type": "slack",
                "action": "message.send",
                "credential_id": "cred-slack",
                "dry_run": True,
                "parameters": {"text": "Hello from HASHI"},
            },
        )
    )

    payload = json.loads(response.text)
    assert create_response.status == 201
    assert server.connector_registry.list_types() == ["slack"]
    assert response.status == 200
    assert payload["ok"] is True
    assert payload["gate"]["allowed"] is True
    assert payload["result"]["status"] == "dry_run"
    assert payload["result"]["data"]["payload"] == {"text": "Hello from HASHI"}


@pytest.mark.asyncio
async def test_default_connector_policy_requires_approval_for_slack_messages(tmp_path):
    server = _server(tmp_path, secrets={"slack_webhook": "https://hooks.slack.test/services/abc"})
    headers = _admin_headers(server)
    await server.handle_enterprise_connector_credentials_create(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/credentials",
            body={
                "connector_type": "slack",
                "display_name": "Slack Webhook",
                "secret_ref": "secrets://slack_webhook",
                "scopes": ["message.send"],
                "credential_id": "cred-slack",
            },
        )
    )
    await server.handle_enterprise_policies_install_defaults(_FakeRequest(headers=headers))

    response = await server.handle_enterprise_connector_execute(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/execute",
            body={
                "connector_type": "slack",
                "action": "message.send",
                "credential_id": "cred-slack",
                "dry_run": True,
                "parameters": {"text": "Hello from HASHI"},
            },
        )
    )

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["ok"] is False
    assert payload["gate"]["allowed"] is False
    assert payload["gate"]["reason"] == "connector_action_requires_approval"
    assert payload["gate"]["policy_rule_id"] == "tpl-connector-slack-message-send-approval"
    assert payload["gate"]["approval_request_id"]
    assert payload["result"]["status"] == "connector_action_requires_approval"


@pytest.mark.asyncio
async def test_enterprise_admin_can_execute_google_chat_dry_run_from_secret_ref(tmp_path):
    server = _server(tmp_path, secrets={"google_chat_webhook": "https://chat.googleapis.com/v1/spaces/abc/messages?key=test"})
    headers = _admin_headers(server)
    create_response = await server.handle_enterprise_connector_credentials_create(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/credentials",
            body={
                "connector_type": "google_chat",
                "display_name": "Google Chat Webhook",
                "secret_ref": "secrets://google_chat_webhook",
                "scopes": ["message.send"],
                "credential_id": "cred-google-chat",
            },
        )
    )
    PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001").add_rule(
        action="connector.execute",
        resource="connector:google_chat:message.send",
        effect="allow",
        rule_id="pol-allow-google-chat-message",
    )

    response = await server.handle_enterprise_connector_execute(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/execute",
            body={
                "connector_type": "google_chat",
                "action": "message.send",
                "credential_id": "cred-google-chat",
                "dry_run": True,
                "parameters": {"text": "Hello from HASHI"},
            },
        )
    )

    payload = json.loads(response.text)
    assert create_response.status == 201
    assert server.connector_registry.list_types() == ["google_chat"]
    assert response.status == 200
    assert payload["ok"] is True
    assert payload["gate"]["allowed"] is True
    assert payload["result"]["status"] == "dry_run"
    assert payload["result"]["data"]["payload"] == {"text": "Hello from HASHI"}


@pytest.mark.asyncio
async def test_enterprise_admin_can_execute_teams_dry_run_from_secret_ref(tmp_path):
    server = _server(tmp_path, secrets={"teams_webhook": "https://outlook.office.com/webhook/abc"})
    headers = _admin_headers(server)
    create_response = await server.handle_enterprise_connector_credentials_create(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/credentials",
            body={
                "connector_type": "teams",
                "display_name": "Teams Webhook",
                "secret_ref": "secrets://teams_webhook",
                "scopes": ["message.send"],
                "credential_id": "cred-teams",
            },
        )
    )
    PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001").add_rule(
        action="connector.execute",
        resource="connector:teams:message.send",
        effect="allow",
        rule_id="pol-allow-teams-message",
    )

    response = await server.handle_enterprise_connector_execute(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/execute",
            body={
                "connector_type": "teams",
                "action": "message.send",
                "credential_id": "cred-teams",
                "dry_run": True,
                "parameters": {"text": "Hello from HASHI", "title": "HASHI"},
            },
        )
    )

    payload = json.loads(response.text)
    assert create_response.status == 201
    assert server.connector_registry.list_types() == ["teams"]
    assert response.status == 200
    assert payload["ok"] is True
    assert payload["gate"]["allowed"] is True
    assert payload["result"]["status"] == "dry_run"
    assert payload["result"]["data"]["payload"] == {"text": "Hello from HASHI", "title": "HASHI"}


@pytest.mark.asyncio
async def test_enterprise_admin_can_execute_feishu_dry_run_from_secret_ref(tmp_path):
    server = _server(tmp_path, secrets={"feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/abc"})
    headers = _admin_headers(server)
    create_response = await server.handle_enterprise_connector_credentials_create(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/credentials",
            body={
                "connector_type": "feishu",
                "display_name": "Feishu Webhook",
                "secret_ref": "secrets://feishu_webhook",
                "scopes": ["message.send"],
                "credential_id": "cred-feishu",
            },
        )
    )
    PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001").add_rule(
        action="connector.execute",
        resource="connector:feishu:message.send",
        effect="allow",
        rule_id="pol-allow-feishu-message",
    )

    response = await server.handle_enterprise_connector_execute(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/execute",
            body={
                "connector_type": "feishu",
                "action": "message.send",
                "credential_id": "cred-feishu",
                "dry_run": True,
                "parameters": {"text": "Hello from HASHI"},
            },
        )
    )

    payload = json.loads(response.text)
    assert create_response.status == 201
    assert server.connector_registry.list_types() == ["feishu"]
    assert response.status == 200
    assert payload["ok"] is True
    assert payload["gate"]["allowed"] is True
    assert payload["result"]["status"] == "dry_run"
    assert payload["result"]["data"]["payload"] == {
        "msg_type": "text",
        "content": {"text": "Hello from HASHI"},
    }


@pytest.mark.asyncio
async def test_default_connector_policy_requires_approval_for_google_chat_messages(tmp_path):
    server = _server(tmp_path, secrets={"google_chat_webhook": "https://chat.googleapis.com/v1/spaces/abc/messages?key=test"})
    headers = _admin_headers(server)
    await server.handle_enterprise_connector_credentials_create(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/credentials",
            body={
                "connector_type": "google_chat",
                "display_name": "Google Chat Webhook",
                "secret_ref": "secrets://google_chat_webhook",
                "scopes": ["message.send"],
                "credential_id": "cred-google-chat",
            },
        )
    )
    await server.handle_enterprise_policies_install_defaults(_FakeRequest(headers=headers))

    response = await server.handle_enterprise_connector_execute(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/execute",
            body={
                "connector_type": "google_chat",
                "action": "message.send",
                "credential_id": "cred-google-chat",
                "dry_run": True,
                "parameters": {"text": "Hello from HASHI"},
            },
        )
    )

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["ok"] is False
    assert payload["gate"]["allowed"] is False
    assert payload["gate"]["reason"] == "connector_action_requires_approval"
    assert payload["gate"]["policy_rule_id"] == "tpl-connector-google-chat-message-send-approval"
    assert payload["gate"]["approval_request_id"]
    assert payload["result"]["status"] == "connector_action_requires_approval"


@pytest.mark.asyncio
async def test_enterprise_connector_execute_rejects_message_send_without_text(tmp_path):
    server = _server(tmp_path, secrets={"slack_webhook": "https://hooks.slack.test/services/abc"})
    headers = _admin_headers(server)
    await server.handle_enterprise_connector_credentials_create(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/credentials",
            body={
                "connector_type": "slack",
                "display_name": "Slack Webhook",
                "secret_ref": "secrets://slack_webhook",
                "scopes": ["message.send"],
                "credential_id": "cred-slack",
            },
        )
    )

    response = await server.handle_enterprise_connector_execute(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/execute",
            body={
                "connector_type": "slack",
                "action": "message.send",
                "credential_id": "cred-slack",
                "dry_run": True,
                "parameters": {},
            },
        )
    )

    payload = json.loads(response.text)
    assert response.status == 400
    assert payload["ok"] is False
    assert "requires non-empty text" in payload["error"]


@pytest.mark.asyncio
async def test_enterprise_admin_can_execute_connector_through_gate(tmp_path):
    connector = _RecordingConnector()
    server = _server(tmp_path, connectors=[connector])
    headers = _admin_headers(server)
    _create_github_credential(server)

    response = await server.handle_enterprise_connector_execute(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/execute",
            body={
                "connector_type": "github",
                "action": "repo.read",
                "resource": "repo:Bazza1982/hashi",
                "credential_id": "cred-github",
                "project_id": "prj-research",
                "parameters": {"token": "redact-me"},
            },
        )
    )

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["ok"] is True
    assert payload["gate"]["allowed"] is True
    assert payload["result"]["data"] == {"resource": "repo:Bazza1982/hashi"}
    assert connector.actions[0].actor_id == "usr-admin"
    events = server.audit_ledger.query(event_type="connector")
    assert len(events) == 1
    assert events[0].context["parameters"]["token"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_enterprise_connector_execute_honors_policy_deny(tmp_path):
    connector = _FakeConnector()
    server = _server(tmp_path, connectors=[connector])
    headers = _admin_headers(server)
    _create_github_credential(server)
    PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001").add_rule(
        action="connector.execute",
        resource="connector:github:repo.read",
        effect="deny",
        rule_id="pol-deny-connector-read",
    )

    response = await server.handle_enterprise_connector_execute(
        _FakeRequest(
            headers=headers,
            path="/api/enterprise/connectors/execute",
            body={
                "connector_type": "github",
                "action": "repo.read",
                "resource": "repo:Bazza1982/hashi",
                "credential_id": "cred-github",
            },
        )
    )

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["ok"] is False
    assert payload["gate"]["allowed"] is False
    assert payload["gate"]["policy_rule_id"] == "pol-deny-connector-read"
    assert payload["result"]["status"] == "connector_action_denied"
