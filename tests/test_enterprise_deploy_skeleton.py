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
    assert "enterprise_k8s_backend_doctor.py" in text
    assert "enterprise_k8s_ha_rehearsal_plan.py" in text
    assert "enterprise_k8s_image_smoke_plan.py" in text


def test_enterprise_raw_kubernetes_includes_lease_load_rehearsal_job_example():
    text = (ROOT / "deploy" / "kubernetes" / "enterprise" / "lease-load-rehearsal-job.example.yaml").read_text(
        encoding="utf-8"
    )

    assert "kind: Job" in text
    assert "lease-load-rehearsal" in text
    assert "HASHI_ENTERPRISE_DATABASE_URL" in text
    assert "hashi-enterprise-database" in text
    assert "lease-load-rehearse" in text
    assert "--lease-count" in text
    assert "--max-workers" in text
    assert "--no-ensure-org" in text


def test_enterprise_kubernetes_image_smoke_workflow_generates_artifact_only():
    text = (ROOT / ".github" / "workflows" / "enterprise-k8s-image-smoke-plan.yml").read_text(
        encoding="utf-8"
    )

    assert "python tools/enterprise_k8s_backend_doctor.py --json" in text
    assert "python tools/enterprise_k8s_image_smoke_plan.py" in text
    assert "actions/upload-artifact@v4" in text
    assert "hashi-k8s-image-smoke-plan.json" in text
    assert "docker build" not in text


def test_enterprise_kubernetes_ha_rehearsal_plan_workflow_generates_artifact_only():
    text = (ROOT / ".github" / "workflows" / "enterprise-k8s-ha-rehearsal-plan.yml").read_text(
        encoding="utf-8"
    )

    assert "python tools/enterprise_k8s_ha_rehearsal_plan.py" in text
    assert "python -m json.tool hashi-k8s-ha-rehearsal-plan.json" in text
    assert "actions/upload-artifact@v4" in text
    assert "hashi-k8s-ha-rehearsal-plan.json" in text
    assert "helm upgrade" not in text
    assert "kubectl" not in text


def test_enterprise_helm_render_workflow_validates_lease_load_job():
    text = (ROOT / ".github" / "workflows" / "enterprise-helm-render.yml").read_text(encoding="utf-8")

    assert "azure/setup-helm@v4" in text
    assert "helm lint deploy/helm/hashi-enterprise" in text
    assert "helm template hashi-enterprise deploy/helm/hashi-enterprise" in text
    assert "leaseLoadRehearsal.enabled=true" in text
    assert "externalDatabase.enabled=true" in text
    assert "lease-load-rehearse" in text
    assert "HASHI_ENTERPRISE_DATABASE_URL" in text
    assert "--lease-count" in text
    assert "--max-workers" in text


def test_enterprise_postgres_lease_workflow_runs_real_integration_test():
    text = (ROOT / ".github" / "workflows" / "enterprise-postgres-lease.yml").read_text(encoding="utf-8")

    assert "postgres:16" in text
    assert "HASHI_ENTERPRISE_POSTGRES_TEST_URL" in text
    assert 'python -m pip install pytest "psycopg[binary]"' in text
    assert "pytest -q tests/test_enterprise_postgres_integration.py" in text


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


def test_enterprise_production_hardening_runbook_covers_ingress_policy_and_scaling():
    text = (ROOT / "docs" / "HASHI_ENTERPRISE_PRODUCTION_HARDENING_RUNBOOK.md").read_text(
        encoding="utf-8"
    )

    assert "production-hardening.values.yaml" in text
    assert "Ingress" in text
    assert "NetworkPolicy" in text
    assert "HorizontalPodAutoscaler" in text
    assert "PodDisruptionBudget" in text
    assert "curl -fsS https://hashi-enterprise.example.com/api/health" in text
    assert "metrics-server" in text
