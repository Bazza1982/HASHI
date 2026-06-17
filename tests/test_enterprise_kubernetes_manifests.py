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


def test_secret_example_does_not_contain_real_values():
    text = _read("secret.example.yaml")

    assert "change-me" in text
    assert "replace-me" in text
    assert "Example only" in text
