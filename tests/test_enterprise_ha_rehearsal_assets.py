from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELM_EXAMPLE = ROOT / "deploy" / "helm" / "hashi-enterprise" / "examples" / "multi-replica-rehearsal.values.yaml"
RUNBOOK = ROOT / "docs" / "HASHI_ENTERPRISE_K8S_HA_REHEARSAL.md"


def test_multi_replica_rehearsal_values_enable_ha_guards():
    text = HELM_EXAMPLE.read_text(encoding="utf-8")

    assert "replicaCount: 2" in text
    assert "externalDatabase:" in text
    assert "enabled: true" in text
    assert "secretName: hashi-enterprise-database" in text
    assert "leaderElection:" in text
    assert "rbac:" in text
    assert "podDisruptionBudget:" in text
    assert "minAvailable: 1" in text
    assert "schedulerLease:" in text
    assert "name: superloop-scheduler" in text
    assert "pool:" in text
    assert "maxSize: \"4\"" in text
    assert "auditExport:" in text
    assert "dbLease:" in text
    assert "name: audit-export" in text


def test_kubernetes_ha_rehearsal_runbook_covers_rollout_failure_and_rollback():
    text = RUNBOOK.read_text(encoding="utf-8")

    assert "enterprise_k8s_ha_rehearsal_plan.py" in text
    assert "lease-rehearse" in text
    assert "multi-replica-rehearsal.values.yaml" in text
    assert "rollout status deploy/hashi-enterprise" in text
    assert "delete pod" in text
    assert "Skipping scheduler tick" in text
    assert "schedulerLease.enabled=false" in text
    assert "does not move the audit ledger itself to PostgreSQL" in text
