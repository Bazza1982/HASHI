from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
K8S_DIR = ROOT / "deploy" / "kubernetes" / "enterprise"


def _read(name: str) -> str:
    return (K8S_DIR / name).read_text(encoding="utf-8")


def test_kubernetes_baseline_files_exist():
    expected = {
        "README.md",
        "kustomization.yaml",
        "namespace.yaml",
        "configmap.yaml",
        "secret.example.yaml",
        "persistent-volume-claim.yaml",
        "deployment.yaml",
        "service.yaml",
        "external-postgres-secret.example.yaml",
        "lease-rbac.example.yaml",
        "pod-disruption-budget.example.yaml",
    }

    assert expected.issubset({path.name for path in K8S_DIR.iterdir()})


def test_kustomization_references_all_manifests():
    text = _read("kustomization.yaml")

    for name in [
        "namespace.yaml",
        "configmap.yaml",
        "secret.example.yaml",
        "persistent-volume-claim.yaml",
        "deployment.yaml",
        "service.yaml",
    ]:
        assert f"- {name}" in text


def test_deployment_uses_enterprise_health_probes_and_port():
    text = _read("deployment.yaml")

    assert "containerPort: 18800" in text
    assert "name: HASHI_POD_NAME" in text
    assert "fieldPath: metadata.name" in text
    assert "name: POD_NAMESPACE" in text
    assert "fieldPath: metadata.namespace" in text
    assert "name: HASHI_ENTERPRISE_SCHEDULER_LEASE_HOLDER" in text
    assert 'value: "$(HASHI_POD_NAME)"' in text
    assert "livenessProbe:" in text
    assert "readinessProbe:" in text
    assert "path: /api/health" in text
    assert "port: workbench" in text


def test_deployment_mounts_data_and_secret_volumes():
    text = _read("deployment.yaml")

    assert "name: hashi-data" in text
    assert "mountPath: /data" in text
    assert "persistentVolumeClaim:" in text
    assert "claimName: hashi-enterprise-data" in text
    assert "name: hashi-secrets" in text
    assert "mountPath: /var/run/secrets/hashi/connectors" in text
    assert "readOnly: true" in text


def test_configmap_sets_enterprise_profile_and_bridge_home():
    text = _read("configmap.yaml")

    assert "HASHI_DEPLOYMENT_PROFILE: enterprise" in text
    assert "HASHI_BRIDGE_HOME: /data" in text
    assert 'HASHI_WORKBENCH_PORT: "18800"' in text
    assert "HASHI_ORGANIZATION_ID: ORG-001" in text
    assert 'HASHI_ENTERPRISE_BOOTSTRAP_COMPLETE: "false"' in text
    assert 'HASHI_ENTERPRISE_SCHEDULER_LEASE_ENABLED: "false"' in text
    assert "HASHI_ENTERPRISE_SCHEDULER_LEASE_BACKEND: db" in text
    assert "HASHI_ENTERPRISE_SCHEDULER_LEASE_NAME: superloop-scheduler" in text
    assert 'HASHI_ENTERPRISE_SCHEDULER_LEASE_TTL_SECONDS: "60"' in text
    assert "HASHI_ENTERPRISE_SCHEDULER_LEASE_K8S_NAMESPACE: hashi-enterprise" in text
    assert 'HASHI_ENTERPRISE_SCHEDULER_LEASE_K8S_IN_CLUSTER: "true"' in text
    assert 'HASHI_ENTERPRISE_SCHEDULER_LEASE_POOL_ENABLED: "false"' in text
    assert 'HASHI_ENTERPRISE_SCHEDULER_LEASE_POOL_MIN_SIZE: "1"' in text
    assert 'HASHI_ENTERPRISE_SCHEDULER_LEASE_POOL_MAX_SIZE: "4"' in text


def test_secret_example_does_not_contain_real_values():
    text = _read("secret.example.yaml")

    assert "change-me" in text
    assert "replace-me" in text
    assert "Example only" in text
    assert "HASHI_ENTERPRISE_DATABASE_URL: sqlite:////data/state/enterprise.sqlite" in text


def test_external_postgres_secret_example_documents_database_contract():
    text = _read("external-postgres-secret.example.yaml")

    assert "kind: Secret" in text
    assert "name: hashi-enterprise-database" in text
    assert "HASHI_ENTERPRISE_DATABASE_URL:" in text
    assert "postgresql://hashi:replace-me@" in text
    assert "sslmode=require" in text


def test_pod_disruption_budget_example_documents_multi_replica_guard():
    text = _read("pod-disruption-budget.example.yaml")

    assert "kind: PodDisruptionBudget" in text
    assert "minAvailable: 1" in text
    assert "app.kubernetes.io/name: hashi" in text
    assert "app.kubernetes.io/component: enterprise" in text


def test_lease_rbac_example_documents_native_kubernetes_lease_permissions():
    text = _read("lease-rbac.example.yaml")

    assert "kind: Role" in text
    assert "kind: RoleBinding" in text
    assert "coordination.k8s.io" in text
    assert "leases" in text
    assert "create" in text
    assert "update" in text
    assert "patch" in text
    assert "name: default" in text


def test_audit_export_daemon_uses_pod_name_db_lease():
    text = _read("audit-export-daemon.deployment.yaml")

    assert "name: HASHI_POD_NAME" in text
    assert "fieldPath: metadata.name" in text
    assert "- --db-lease-name" in text
    assert "- audit-export" in text
    assert "- --db-lease-holder" in text
    assert "- $(HASHI_POD_NAME)" in text
    assert "- --db-lease-ttl" in text
    assert '- "180"' in text
