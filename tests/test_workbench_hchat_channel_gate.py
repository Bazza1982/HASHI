from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
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
        self.enqueue_kwargs = []

    async def enqueue_api_text(self, text: str, **kwargs):
        self.enqueued.append(text)
        self.enqueue_kwargs.append(kwargs)
        return "req-1"


def _server(tmp_path: Path, *, profile: str = "enterprise", runtimes: list | None = None) -> WorkbenchApiServer:
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {
                    "deployment_profile": profile,
                    "organization_id": "ORG-001",
                    "instance_id": "HASHI1",
                },
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
        instance_id="HASHI1",
    )
    return WorkbenchApiServer(config_path=config_path, global_config=global_config, runtimes=runtimes or [])


def _payload() -> dict:
    message_id = "hchat-channel-gate-1"
    return {
        "to_agent": "nana",
        "to_instance": "HASHI1",
        "from_agent": "zelda",
        "from_instance": "HASHI2",
        "text": "please check this",
        "message_id": message_id,
        "originated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "idempotency_key": f"hchat:{message_id}",
    }


def _audit_events(tmp_path: Path) -> list[dict]:
    path = tmp_path / "state" / "enterprise_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_enterprise_hchat_exchange_denies_disabled_default_and_audits(tmp_path):
    runtime = _Runtime("nana")
    server = _server(tmp_path, runtimes=[runtime])
    server.identity_service.create_organization(org_id="ORG-001", name="Acme")
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
async def test_enterprise_hchat_exchange_allows_bound_target_agent(tmp_path):
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
    response = await server.handle_hchat_exchange(_FakeRequest(_payload()))

    assert response.status == 200
    assert json.loads(response.text)["ok"] is True
    assert len(runtime.enqueued) == 1
    assert runtime.enqueued[0].startswith("[hchat from zelda@HASHI2]")
    assert runtime.enqueue_kwargs[0]["source"] == "hchat"
    context = runtime.enqueue_kwargs[0]["request_metadata"]["_hchat_context"]
    assert context["from_agent"] == "zelda"
    assert context["from_instance"] == "HASHI2"
    assert _audit_events(tmp_path) == []


@pytest.mark.asyncio
async def test_personal_hchat_exchange_still_allows_without_channel_registry(tmp_path):
    runtime = _Runtime("nana")
    server = _server(tmp_path, profile="personal", runtimes=[runtime])
    response = await server.handle_hchat_exchange(_FakeRequest(_payload()))

    assert response.status == 200
    assert len(runtime.enqueued) == 1


@pytest.mark.asyncio
async def test_hchat_exchange_retry_reports_confirmation_terminal_state(tmp_path):
    runtime = _Runtime("nana")
    server = _server(tmp_path, profile="personal", runtimes=[runtime])
    session = server.session_store.resolve_primary_session(
        owner_id="user:default", agent_id="nana", establish=True
    )

    confirmed_payload = _payload()
    confirmed_payload["message_id"] = "hchat-confirmed-status"
    confirmed_payload["idempotency_key"] = "hchat:hchat-confirmed-status"
    confirmed_payload["originated_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=90)
    ).isoformat().replace("+00:00", "Z")
    pending = await server.handle_hchat_exchange(_FakeRequest(confirmed_payload))
    pending_data = json.loads(pending.text)
    assert pending.status == 202
    assert pending_data["confirmation_required"] is True
    assert runtime.enqueued == []

    server.ingress_confirmation.decide(
        confirmation_id=pending_data["confirmation_id"],
        decision="confirm",
        owner_id="user:default",
        execute_fn=lambda _record: "run-confirmed-status",
    )
    confirmed = await server.handle_hchat_exchange(_FakeRequest(confirmed_payload))
    confirmed_data = json.loads(confirmed.text)
    assert confirmed.status == 200
    assert confirmed_data["accepted"] is True
    assert confirmed_data["confirmation_required"] is False
    assert confirmed_data["state"] == "confirmed"
    assert confirmed_data["run_id"] == "run-confirmed-status"

    discarded_payload = _payload()
    discarded_payload["message_id"] = "hchat-discarded-status"
    discarded_payload["idempotency_key"] = "hchat:hchat-discarded-status"
    discarded_payload["originated_at"] = confirmed_payload["originated_at"]
    pending_discard = await server.handle_hchat_exchange(
        _FakeRequest(discarded_payload)
    )
    discard_data = json.loads(pending_discard.text)
    server.ingress_confirmation.decide(
        confirmation_id=discard_data["confirmation_id"],
        decision="discard",
        owner_id="user:default",
    )
    discarded = await server.handle_hchat_exchange(_FakeRequest(discarded_payload))
    discarded_data = json.loads(discarded.text)
    assert discarded.status == 200
    assert discarded_data["accepted"] is False
    assert discarded_data["confirmation_required"] is False
    assert discarded_data["state"] == "discarded"
    assert runtime.enqueued == []
    assert session["session_id"]
