from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHART_DIR = ROOT / "deploy" / "helm" / "hashi-enterprise"
TEMPLATES_DIR = CHART_DIR / "templates"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_enterprise_helm_chart_files_exist():
    expected = {
        "Chart.yaml",
        "README.md",
        "values.yaml",
        "templates/_helpers.tpl",
        "templates/configmap.yaml",
        "templates/deployment.yaml",
        "templates/hpa.yaml",
        "templates/ingress.yaml",
        "templates/lease-rbac.yaml",
        "templates/networkpolicy.yaml",
        "templates/pdb.yaml",
        "templates/pvc.yaml",
        "templates/secret.example.yaml",
        "templates/service.yaml",
        "templates/serviceaccount.yaml",
        "examples/external-postgres-secret.kubernetes.yaml",
        "examples/multi-replica-rehearsal.values.yaml",
    }

    actual = {
        str(path.relative_to(CHART_DIR))
        for path in CHART_DIR.rglob("*")
        if path.is_file()
    }

    assert expected.issubset(actual)


def test_enterprise_helm_values_default_to_governed_single_replica():
    text = _read(CHART_DIR / "values.yaml")

    assert "replicaCount: 1" in text
    assert "deploymentProfile: enterprise" in text
    assert "organizationId: ORG-001" in text
    assert "bridgeHome: /data" in text
    assert 'workbenchPort: "18800"' in text
    assert "enabled: false" in text
    assert "networkPolicy:" in text
    assert "leaderElection:" in text
    assert "autoscaling:" in text
    assert "externalDatabase:" in text
    assert "secretKey: HASHI_ENTERPRISE_DATABASE_URL" in text
    assert "schedulerLease:" in text
    assert "name: superloop-scheduler" in text
    assert 'holder: "$(HASHI_POD_NAME)"' in text
    assert "minSize: \"1\"" in text
    assert "maxSize: \"4\"" in text
    assert "podDisruptionBudget:" in text
    assert "minAvailable: 1" in text
    assert "dbLease:" in text
    assert 'holder: "$(HASHI_POD_NAME)"' in text


def test_enterprise_helm_deployment_keeps_health_and_secret_contracts():
    text = _read(TEMPLATES_DIR / "deployment.yaml")

    assert "replicas: {{ .Values.replicaCount }}" in text
    assert "containerPort: {{ .Values.service.port }}" in text
    assert "configMapRef:" in text
    assert "secretRef:" in text
    assert "mountPath: {{ .Values.enterprise.bridgeHome }}" in text
    assert "mountPath: {{ .Values.connectorSecrets.mountPath }}" in text
    assert "readOnly: {{ .Values.connectorSecrets.readOnly }}" in text
    assert "livenessProbe:" in text
    assert "readinessProbe:" in text
    assert "path: {{ .Values.livenessProbe.path }}" in text
    assert "path: {{ .Values.readinessProbe.path }}" in text
    assert "name: HASHI_POD_NAME" in text
    assert "fieldPath: metadata.name" in text
    assert "name: POD_NAMESPACE" in text
    assert "fieldPath: metadata.namespace" in text
    assert "name: HASHI_ENTERPRISE_SCHEDULER_LEASE_HOLDER" in text
    assert "value: {{ .Values.schedulerLease.holder | quote }}" in text
    assert "{{- if .Values.externalDatabase.enabled }}" in text
    assert "name: HASHI_ENTERPRISE_DATABASE_URL" in text
    assert "name: {{ .Values.externalDatabase.secretName }}" in text
    assert "key: {{ .Values.externalDatabase.secretKey }}" in text


def test_enterprise_helm_external_database_example_uses_postgres_secret_contract():
    text = _read(CHART_DIR / "examples" / "external-postgres-secret.kubernetes.yaml")

    assert "kind: Secret" in text
    assert "name: hashi-enterprise-database" in text
    assert "HASHI_ENTERPRISE_DATABASE_URL:" in text
    assert "postgresql://hashi:replace-me@" in text
    assert "sslmode=require" in text


def test_enterprise_helm_configmap_sets_enterprise_environment():
    text = _read(TEMPLATES_DIR / "configmap.yaml")

    assert "HASHI_DEPLOYMENT_PROFILE:" in text
    assert "HASHI_INSTANCE_ID:" in text
    assert "HASHI_ORGANIZATION_ID:" in text
    assert "HASHI_BRIDGE_HOME:" in text
    assert "HASHI_WORKBENCH_PORT:" in text
    assert "HASHI_ENTERPRISE_SCHEDULER_LEASE_ENABLED:" in text
    assert "HASHI_ENTERPRISE_SCHEDULER_LEASE_BACKEND:" in text
    assert "HASHI_ENTERPRISE_SCHEDULER_LEASE_NAME:" in text
    assert "HASHI_ENTERPRISE_SCHEDULER_LEASE_TTL_SECONDS:" in text
    assert "HASHI_ENTERPRISE_SCHEDULER_LEASE_K8S_NAMESPACE:" in text
    assert "HASHI_ENTERPRISE_SCHEDULER_LEASE_K8S_IN_CLUSTER:" in text
    assert "HASHI_ENTERPRISE_SCHEDULER_LEASE_KUBECONFIG:" in text
    assert "HASHI_ENTERPRISE_SCHEDULER_LEASE_POOL_ENABLED:" in text
    assert "HASHI_ENTERPRISE_SCHEDULER_LEASE_POOL_MIN_SIZE:" in text
    assert "HASHI_ENTERPRISE_SCHEDULER_LEASE_POOL_MAX_SIZE:" in text


def test_enterprise_helm_readme_documents_kubernetes_backend_extra():
    text = _read(CHART_DIR / "README.md")

    assert "HASHI_ENTERPRISE_EXTRAS=kubernetes" in text
    assert "hashi-bridge[kubernetes]" in text
    assert "schedulerLease.backend=kubernetes" in text


def test_enterprise_helm_chart_includes_optional_ingress_network_policy_and_hpa():
    ingress = _read(TEMPLATES_DIR / "ingress.yaml")
    network_policy = _read(TEMPLATES_DIR / "networkpolicy.yaml")
    hpa = _read(TEMPLATES_DIR / "hpa.yaml")

    assert "{{- if .Values.ingress.enabled -}}" in ingress
    assert "kind: Ingress" in ingress
    assert "service:" in ingress
    assert "{{- if .Values.networkPolicy.enabled -}}" in network_policy
    assert "kind: NetworkPolicy" in network_policy
    assert "policyTypes:" in network_policy
    assert "{{- if .Values.autoscaling.enabled -}}" in hpa
    assert "kind: HorizontalPodAutoscaler" in hpa


def test_enterprise_helm_chart_includes_optional_pod_disruption_budget():
    pdb = _read(TEMPLATES_DIR / "pdb.yaml")

    assert "{{- if .Values.podDisruptionBudget.enabled -}}" in pdb
    assert "kind: PodDisruptionBudget" in pdb
    assert "minAvailable: {{ .Values.podDisruptionBudget.minAvailable }}" in pdb
    assert "app.kubernetes.io/name: {{ include \"hashi-enterprise.name\" . }}" in pdb
    assert "app.kubernetes.io/instance: {{ .Release.Name }}" in pdb


def test_enterprise_helm_chart_includes_optional_lease_rbac():
    text = _read(TEMPLATES_DIR / "lease-rbac.yaml")

    assert "{{- if .Values.leaderElection.rbac.enabled }}" in text
    assert "kind: Role" in text
    assert "kind: RoleBinding" in text
    assert "coordination.k8s.io" in text
    assert "leases" in text
    assert "create" in text
    assert "update" in text
    assert "patch" in text
    assert "name: {{ include \"hashi-enterprise.serviceAccountName\" . }}" in text


def test_enterprise_helm_audit_export_daemon_supports_optional_db_lease():
    text = _read(TEMPLATES_DIR / "audit-export-daemon.yaml")

    assert "{{- if .Values.auditExport.daemon.dbLease.enabled }}" in text
    assert "name: HASHI_POD_NAME" in text
    assert "fieldPath: metadata.name" in text
    assert "- --db-lease-name" in text
    assert "{{ .Values.auditExport.daemon.dbLease.name | quote }}" in text
    assert "- --db-lease-holder" in text
    assert "{{ .Values.auditExport.daemon.dbLease.holder | quote }}" in text
    assert "- --db-lease-ttl" in text
    assert "{{ .Values.auditExport.daemon.dbLease.ttl | quote }}" in text
