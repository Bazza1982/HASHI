from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.workbench_api import WorkbenchApiServer


class _FakeRequest:
    pass


def _server(tmp_path: Path, *, profile: str) -> WorkbenchApiServer:
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


@pytest.mark.asyncio
async def test_enterprise_health_includes_governance_services(tmp_path):
    server = _server(tmp_path, profile="enterprise")

    response = await server.handle_health(_FakeRequest())

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["ok"] is True
    assert payload["enterprise"]["ok"] is True
    assert payload["enterprise"]["profile"] == "enterprise"
    assert payload["enterprise"]["organization_id"] == "ORG-001"
    assert payload["enterprise"]["services"] == {
        "identity": True,
        "channel_registry": True,
        "audit_ledger": True,
        "policy_evaluator": True,
    }


@pytest.mark.asyncio
async def test_personal_health_keeps_legacy_shape_without_enterprise_block(tmp_path):
    server = _server(tmp_path, profile="personal")

    response = await server.handle_health(_FakeRequest())

    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["ok"] is True
    assert "enterprise" not in payload
