from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_enterprise_dockerfile_declares_enterprise_runtime():
    text = (ROOT / "Dockerfile.enterprise").read_text(encoding="utf-8")

    assert "FROM python:3.12-slim" in text
    assert "HASHI_DEPLOYMENT_PROFILE=enterprise" in text
    assert 'ARG HASHI_ENTERPRISE_EXTRAS=""' in text
    assert 'pip install --no-cache-dir ".[${HASHI_ENTERPRISE_EXTRAS}]"' in text
    assert 'CMD ["python", "main.py"]' in text


def test_enterprise_kubernetes_backend_has_optional_package_extra():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'kubernetes = ["kubernetes>=29.0.0,<32.0.0"]' in text


def test_enterprise_raw_kubernetes_docs_cover_kubernetes_backend_extra():
    text = (ROOT / "deploy" / "kubernetes" / "enterprise" / "README.md").read_text(encoding="utf-8")

    assert "HASHI_ENTERPRISE_EXTRAS=kubernetes" in text
    assert "hashi-bridge[kubernetes]" in text
    assert "HASHI_ENTERPRISE_SCHEDULER_LEASE_BACKEND" in text
    assert "k8s-lease-rehearse" in text


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
