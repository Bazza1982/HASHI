from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.workbench_api import WorkbenchApiServer


class _FakeRequest:
    def __init__(self, payload: dict | None = None):
        self._payload = payload or {}
        self.headers = {}
        self.path = "/api/bridge/hchat-exchange"

    async def json(self):
        return self._payload


class _Runtime:
    def __init__(self, name: str):
        self.name = name
        self.enqueued = []

    async def enqueue_api_text(self, text: str):
        self.enqueued.append(text)
        return "req-1"


def _server(tmp_path: Path, *, profile: str = "enterprise", runtimes: list | None = None) -> WorkbenchApiServer:
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
    return WorkbenchApiServer(config_path=config_path, global_config=global_config, runtimes=runtimes or [])


def _payload() -> dict:
    return {
        "to_agent": "nana",
        "to_instance": "HASHI1",
        "from_agent": "zelda",
        "from_instance": "HASHI2",
        "text": "please check this",
    }


def _audit_events(tmp_path: Path) -> list[dict]:
    path = tmp_path / "state" / "enterprise_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_enterprise_hchat_exchange_denies_disabled_default_and_audits(tmp_path, monkeypatch):
    runtime = _Runtime("nana")
    server = _server(tmp_path, runtimes=[runtime])
    server.identity_service.create_organization(org_id="ORG-001", name="Acme")
    monkeypatch.setattr("orchestrator.ticket_manager.detect_instance", lambda _root: "HASHI1")

    response = await server.handle_hchat_exchange(_FakeRequest(_payload()))

    assert response.status == 403
    assert json.loads(response.text)["error"] == "hchat ingress denied: channel_disabled"
    assert runtime.enqueued == []
    event = _audit_events(tmp_path)[-1]
    assert event["event_type"] == "channel"
    assert event["status"] == "denied"
    assert event["context"]["channel_type"] == "hchat"
    assert event["context"]["direction"] == "ingress"
    assert event["context"]["to_agent"] == "nana"


@pytest.mark.asyncio
async def test_enterprise_hchat_exchange_allows_bound_target_agent(tmp_path, monkeypatch):
    runtime = _Runtime("nana")
    server = _server(tmp_path, runtimes=[runtime])
    server.identity_service.create_organization(org_id="ORG-001", name="Acme")
    server.channel_registry.ensure_default_channels(org_id="ORG-001")
    server.channel_registry.register_channel(org_id="ORG-001", channel_type="hchat", enabled=True)
    server.channel_registry.bind_channel(
        org_id="ORG-001",
        channel_type="hchat",
        scope_type="agent",
        scope_id="nana",
        permission="ingress",
    )
    monkeypatch.setattr("orchestrator.ticket_manager.detect_instance", lambda _root: "HASHI1")

    response = await server.handle_hchat_exchange(_FakeRequest(_payload()))

    assert response.status == 200
    assert json.loads(response.text)["ok"] is True
    assert len(runtime.enqueued) == 1
    assert runtime.enqueued[0].startswith("[hchat from zelda@HASHI2]")
    assert _audit_events(tmp_path) == []


@pytest.mark.asyncio
async def test_personal_hchat_exchange_still_allows_without_channel_registry(tmp_path, monkeypatch):
    runtime = _Runtime("nana")
    server = _server(tmp_path, profile="personal", runtimes=[runtime])
    monkeypatch.setattr("orchestrator.ticket_manager.detect_instance", lambda _root: "HASHI1")

    response = await server.handle_hchat_exchange(_FakeRequest(_payload()))

    assert response.status == 200
    assert len(runtime.enqueued) == 1
