from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.enterprise import EnterpriseAuditLedger, PolicyEvaluator
from orchestrator.workbench_api import WorkbenchApiServer


class _FakeRequest:
    def __init__(
        self,
        payload: dict | None = None,
        *,
        headers: dict | None = None,
        path: str = "",
        query: dict | None = None,
        match_info: dict | None = None,
    ):
        self._payload = payload or {}
        self.headers = headers or {}
        self.path = path
        self.query = query or {}
        self.match_info = match_info or {}

    async def json(self):
        return self._payload


def _server(tmp_path: Path, *, profile: str = "enterprise") -> WorkbenchApiServer:
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {"deployment_profile": profile, "organization_id": "ORG-001"},
                "agents": [],
            }
        ),
        encoding="utf-8",
    )
    global_config = SimpleNamespace(
        deployment_profile=profile,
        organization_id="ORG-001",
        bridge_home=tmp_path,
        workbench_port=18800,
        project_root=tmp_path,
    )
    return WorkbenchApiServer(config_path=config_path, global_config=global_config)


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


@pytest.mark.asyncio
async def test_enterprise_admin_can_list_pending_approvals(tmp_path):
    server = _server(tmp_path)
    headers = _admin_headers(server)
    evaluator = PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    evaluator.create_approval_request(
        action="file.write",
        resource="file:report.md",
        context={"actor_id": "usr-1", "project_id": "prj-finance"},
        request_id="appr-list",
    )

    response = await server.handle_enterprise_approvals(_FakeRequest(headers=headers))

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["ok"] is True
    assert payload["count"] == 1
    assert payload["approvals"][0]["id"] == "appr-list"
    assert payload["approvals"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_enterprise_admin_can_approve_request(tmp_path):
    server = _server(tmp_path)
    headers = _admin_headers(server)
    evaluator = PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    evaluator.create_approval_request(
        action="file.write",
        resource="file:report.md",
        context={"actor_id": "usr-1", "project_id": "prj-finance", "task_id": "task-1"},
        request_id="appr-approve",
    )

    response = await server.handle_enterprise_approval_approve(
        _FakeRequest(
            {"reason": "approved for closeout"},
            headers=headers,
            match_info={"request_id": "appr-approve"},
        )
    )

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["approval"]["status"] == "approved"
    assert payload["approval"]["decided_by"] == "usr-admin"
    assert payload["approval"]["decision_reason"] == "approved for closeout"
    ledger = EnterpriseAuditLedger.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    events = ledger.query(event_type="policy")
    assert events[-1].action == "approval.decide"
    assert events[-1].status == "approved"
    assert events[-1].request_id == "appr-approve"


@pytest.mark.asyncio
async def test_enterprise_admin_can_deny_request(tmp_path):
    server = _server(tmp_path)
    headers = _admin_headers(server)
    evaluator = PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    evaluator.create_approval_request(
        action="backend.switch",
        resource="backend:grok-cli",
        request_id="appr-deny",
    )

    response = await server.handle_enterprise_approval_deny(
        _FakeRequest(
            {"reason": "backend not approved"},
            headers=headers,
            match_info={"request_id": "appr-deny"},
        )
    )

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["approval"]["status"] == "denied"
    assert payload["approval"]["decision_reason"] == "backend not approved"
