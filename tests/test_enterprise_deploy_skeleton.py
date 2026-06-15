from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_enterprise_dockerfile_declares_enterprise_runtime():
    text = (ROOT / "Dockerfile.enterprise").read_text(encoding="utf-8")

    assert "FROM python:3.12-slim" in text
    assert "HASHI_DEPLOYMENT_PROFILE=enterprise" in text
    assert 'CMD ["python", "main.py"]' in text


def test_enterprise_compose_mounts_governed_volumes_and_healthcheck():
    text = (ROOT / "deploy" / "docker-compose.enterprise.yml").read_text(encoding="utf-8")

    assert "hashi_enterprise_state" in text
    assert "hashi_enterprise_workspaces" in text
    assert "HASHI_BRIDGE_HOME: /data" in text
    assert "/api/health" in text


def test_enterprise_env_example_uses_placeholder_secret():
    text = (ROOT / "deploy" / "enterprise.env.example").read_text(encoding="utf-8")

    assert "HASHI_DEPLOYMENT_PROFILE=enterprise" in text
    assert "HASHI_WORKBENCH_ADMIN_TOKEN=change-me" in text
