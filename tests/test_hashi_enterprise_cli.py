from __future__ import annotations

import json
import sys
from threading import Lock
from types import SimpleNamespace

import pytest

import hashi
from orchestrator.enterprise import (
    EnterpriseAuditLedger,
    EnterpriseLeaseStore,
    IdentityService,
    KubernetesApiLeaseClient,
    KubernetesLeaseConflict,
    run_enterprise_lease_rehearsal,
)
from orchestrator.enterprise.store import SCHEMA_VERSION


def _prepare_enterprise_root(root):
    (root / "state").mkdir()
    (root / "state" / "enterprise.sqlite").write_text("db", encoding="utf-8")
    (root / "state" / "enterprise_audit.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "agents.json").write_text('{"agents":[]}\n', encoding="utf-8")
    (root / "agent_capabilities.json").write_text('{"agents":[]}\n', encoding="utf-8")


def test_enterprise_backup_cli_creates_archive_and_manifest(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hashi, "ROOT_DIR", tmp_path)
    _prepare_enterprise_root(tmp_path)
    output = tmp_path / "backup.tar.gz"

    rc = hashi.cmd_enterprise_backup(SimpleNamespace(output=str(output), include_workspaces=False))

    assert rc == 0
    assert output.exists()
    manifest = hashi.cmd_enterprise_backup_inspect(SimpleNamespace(archive=str(output)))
    assert manifest == 0
    text = capsys.readouterr().out
    assert "Enterprise backup written" in text
    assert "state/enterprise.sqlite" in text


def test_enterprise_restore_cli_restores_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(hashi, "ROOT_DIR", tmp_path)
    _prepare_enterprise_root(tmp_path)
    output = tmp_path / "backup.tar.gz"
    hashi.cmd_enterprise_backup(SimpleNamespace(output=str(output), include_workspaces=False))

    rc = hashi.cmd_enterprise_restore(
        SimpleNamespace(
            archive=str(output),
            destination=str(tmp_path / "restore"),
            overwrite=False,
        )
    )

    assert rc == 0
    assert (tmp_path / "restore" / "state" / "enterprise.sqlite").read_text(encoding="utf-8") == "db"


def test_enterprise_backup_cli_fails_when_required_state_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(hashi, "ROOT_DIR", tmp_path)
    (tmp_path / "agents.json").write_text('{"agents":[]}\n', encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="required backup item missing"):
        hashi.cmd_enterprise_backup(SimpleNamespace(output=str(tmp_path / "backup.tar.gz"), include_workspaces=False))


def test_enterprise_migrate_cli_initializes_schema(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hashi, "ROOT_DIR", tmp_path)
    db_path = tmp_path / "state" / "enterprise.sqlite"

    rc = hashi.cmd_enterprise_migrate(SimpleNamespace(db=str(db_path)))

    assert rc == 0
    assert db_path.exists()
    output = capsys.readouterr().out
    assert "Enterprise schema migrated" in output
    assert "Before: (none)" in output
    assert f"After : {SCHEMA_VERSION}" in output


def test_enterprise_lease_rehearsal_service_passes_with_sqlite(tmp_path):
    db_path = tmp_path / "enterprise.sqlite"
    IdentityService.from_path(db_path).create_organization(org_id="ORG-001", name="Acme")
    leases = EnterpriseLeaseStore.from_url(f"sqlite:///{db_path}", org_id="ORG-001")

    result = run_enterprise_lease_rehearsal(leases, lease_name="rehearsal-test", ttl_seconds=30)

    assert result.passed is True
    assert result.first_acquired_holder in {"rehearsal-a", "rehearsal-b"}
    assert result.blocked_holder in {"rehearsal-a", "rehearsal-b"}
    assert result.blocked_holder != result.first_acquired_holder
    assert result.takeover_succeeded is True
    assert leases.get("rehearsal-test") is None


def test_enterprise_lease_rehearse_cli_runs_sqlite_rehearsal(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hashi, "ROOT_DIR", tmp_path)
    db_path = tmp_path / "state" / "enterprise.sqlite"

    rc = hashi.cmd_enterprise_lease_rehearse(
        SimpleNamespace(
            db_url=f"sqlite:///{db_path}",
            org_id="ORG-001",
            lease_name="cli-rehearsal",
            holder_a="pod-a",
            holder_b="pod-b",
            ttl=30,
            no_ensure_org=False,
        )
    )

    assert rc == 0
    output = capsys.readouterr().out
    assert "Enterprise lease rehearsal completed" in output
    payload = json.loads(output[output.index("{"):])
    assert payload["passed"] is True
    assert payload["lease_name"] == "cli-rehearsal"


def test_enterprise_k8s_lease_rehearse_cli_runs_fake_rehearsal(monkeypatch, capsys):
    calls = {}

    def fake_from_config(*, in_cluster, kubeconfig_path):
        calls["in_cluster"] = in_cluster
        calls["kubeconfig_path"] = kubeconfig_path
        return _FakeKubernetesLeaseClient()

    monkeypatch.setattr(KubernetesApiLeaseClient, "from_config", staticmethod(fake_from_config))

    rc = hashi.cmd_enterprise_k8s_lease_rehearse(
        SimpleNamespace(
            namespace="hashi-enterprise",
            lease_name="k8s-cli-rehearsal",
            holder_a="pod-a",
            holder_b="pod-b",
            ttl=30,
            kubeconfig="/tmp/kubeconfig",
            in_cluster=None,
        )
    )

    assert rc == 0
    output = capsys.readouterr().out
    assert "Kubernetes lease rehearsal completed" in output
    payload = json.loads(output[output.index("{"):])
    assert payload["passed"] is True
    assert payload["namespace"] == "hashi-enterprise"
    assert payload["in_cluster"] is False
    assert payload["lease_name"] == "k8s-cli-rehearsal"
    assert calls == {"in_cluster": False, "kubeconfig_path": "/tmp/kubeconfig"}


def test_enterprise_audit_live_export_cli_sends_events_and_updates_checkpoint(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hashi, "ROOT_DIR", tmp_path)
    db_path = tmp_path / "state" / "enterprise.sqlite"
    identity = IdentityService.from_path(db_path)
    identity.create_organization(org_id="ORG-001", name="Acme")
    ledger = EnterpriseAuditLedger.from_path(db_path, org_id="ORG-001")
    event = ledger.append(event_type="auth", action="login", status="success", actor_id="usr-1")
    calls = []

    def transport(url, body, headers, timeout):
        calls.append((url, body, headers, timeout))
        return 202, "accepted"

    monkeypatch.setattr(hashi, "_http_post_transport", transport)

    rc = hashi.cmd_enterprise_audit_export_live(
        SimpleNamespace(
            endpoint="https://siem.example.com/ingest",
            format="ledger",
            db=None,
            org_id="ORG-001",
            checkpoint=None,
            batch_size=10,
            timeout=5.0,
            max_attempts=1,
            backoff=0.0,
            header=["Authorization: Bearer test"],
        )
    )

    assert rc == 0
    url, body, headers, timeout = calls[0]
    assert url == "https://siem.example.com/ingest"
    assert headers["authorization"] == "Bearer test"
    assert timeout == 5.0
    assert event.id in body.decode("utf-8")
    checkpoint = tmp_path / "state" / "audit_live_export_checkpoint.json"
    assert checkpoint.exists()
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["last_chain_index"] == event.chain_index
    output = capsys.readouterr().out
    assert "Enterprise audit live export completed" in output
    assert "Sent            : 1" in output
    assert "Lock            :" in output
    assert not (tmp_path / "state" / "audit_live_export_checkpoint.json.lock").exists()


class _FakeKubernetesLeaseClient:
    def __init__(self):
        self.leases = {}
        self.lock = Lock()

    def get_lease(self, namespace, name):
        with self.lock:
            return self.leases.get((namespace, name))

    def create_lease(self, lease):
        with self.lock:
            key = (lease.namespace, lease.name)
            if key in self.leases:
                raise KubernetesLeaseConflict()
            self.leases[key] = lease
            return lease

    def replace_lease(self, lease):
        with self.lock:
            key = (lease.namespace, lease.name)
            if key not in self.leases:
                raise KubernetesLeaseConflict()
            self.leases[key] = lease
            return lease

    def delete_lease(self, namespace, name, *, holder_identity):
        with self.lock:
            key = (namespace, name)
            lease = self.leases.get(key)
            if lease is None or lease.holder_identity != holder_identity:
                return False
            del self.leases[key]
            return True


def test_enterprise_audit_live_export_cli_uses_db_lease_when_configured(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hashi, "ROOT_DIR", tmp_path)
    db_path = tmp_path / "state" / "enterprise.sqlite"
    identity = IdentityService.from_path(db_path)
    identity.create_organization(org_id="ORG-001", name="Acme")
    ledger = EnterpriseAuditLedger.from_path(db_path, org_id="ORG-001")
    ledger.append(event_type="auth", action="login", status="success", actor_id="usr-1")
    calls = []

    def transport(url, body, headers, timeout):
        calls.append((url, body, headers, timeout))
        return 202, "accepted"

    monkeypatch.setattr(hashi, "_http_post_transport", transport)

    rc = hashi.cmd_enterprise_audit_export_live(
        SimpleNamespace(
            endpoint="https://siem.example.com/ingest",
            format="ledger",
            db=None,
            org_id="ORG-001",
            checkpoint=None,
            lock_path=None,
            db_lease_name="audit-export",
            db_lease_holder="pod-a",
            db_lease_ttl=30,
            batch_size=10,
            timeout=5.0,
            max_attempts=1,
            backoff=0.0,
            header=[],
            daemon=False,
        )
    )

    assert rc == 0
    assert len(calls) == 1
    assert EnterpriseLeaseStore.from_path(db_path, org_id="ORG-001").get("audit-export") is None
    assert "DB lease        : audit-export (pod-a)" in capsys.readouterr().out


def test_enterprise_audit_live_export_daemon_runs_bounded_cycles(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hashi, "ROOT_DIR", tmp_path)
    db_path = tmp_path / "state" / "enterprise.sqlite"
    identity = IdentityService.from_path(db_path)
    identity.create_organization(org_id="ORG-001", name="Acme")
    ledger = EnterpriseAuditLedger.from_path(db_path, org_id="ORG-001")
    event = ledger.append(event_type="auth", action="login", status="success", actor_id="usr-1")
    calls = []
    sleeps = []

    def transport(url, body, headers, timeout):
        calls.append((url, body, headers, timeout))
        return 202, "accepted"

    monkeypatch.setattr(hashi, "_http_post_transport", transport)
    monkeypatch.setattr(hashi.time, "sleep", sleeps.append)

    rc = hashi.cmd_enterprise_audit_export_live(
        SimpleNamespace(
            endpoint="https://siem.example.com/ingest",
            format="ledger",
            db=None,
            org_id="ORG-001",
            checkpoint=None,
            batch_size=10,
            timeout=5.0,
            max_attempts=1,
            backoff=0.0,
            header=["Authorization: Bearer test"],
            daemon=True,
            interval=0.5,
            max_cycles=2,
        )
    )

    assert rc == 0
    assert len(calls) == 1
    assert event.id in calls[0][1].decode("utf-8")
    assert sleeps == [0.5]
    checkpoint = tmp_path / "state" / "audit_live_export_checkpoint.json"
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["last_chain_index"] == event.chain_index
    output = capsys.readouterr().out
    assert "Enterprise audit live export daemon started" in output
    assert "Cycle 1: sent=1" in output
    assert "Cycle 2: sent=0" in output
    assert "completed max cycles" in output
    assert "Lock            :" in output
    assert not (tmp_path / "state" / "audit_live_export_checkpoint.json.lock").exists()


def test_enterprise_audit_live_export_daemon_uses_and_releases_db_lease(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hashi, "ROOT_DIR", tmp_path)
    db_path = tmp_path / "state" / "enterprise.sqlite"
    identity = IdentityService.from_path(db_path)
    identity.create_organization(org_id="ORG-001", name="Acme")
    ledger = EnterpriseAuditLedger.from_path(db_path, org_id="ORG-001")
    ledger.append(event_type="auth", action="login", status="success", actor_id="usr-1")
    calls = []

    def transport(url, body, headers, timeout):
        calls.append((url, body, headers, timeout))
        return 202, "accepted"

    monkeypatch.setattr(hashi, "_http_post_transport", transport)
    monkeypatch.setattr(hashi.time, "sleep", lambda _seconds: None)

    rc = hashi.cmd_enterprise_audit_export_live(
        SimpleNamespace(
            endpoint="https://siem.example.com/ingest",
            format="ledger",
            db=None,
            org_id="ORG-001",
            checkpoint=None,
            lock_path=None,
            db_lease_name="audit-export",
            db_lease_holder="pod-a",
            db_lease_ttl=30,
            batch_size=10,
            timeout=5.0,
            max_attempts=1,
            backoff=0.0,
            header=[],
            daemon=True,
            interval=0.5,
            max_cycles=2,
        )
    )

    assert rc == 0
    assert len(calls) == 1
    assert EnterpriseLeaseStore.from_path(db_path, org_id="ORG-001").get("audit-export") is None
    assert "DB lease        : audit-export (pod-a)" in capsys.readouterr().out


def test_enterprise_audit_live_export_cli_rejects_concurrent_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(hashi, "ROOT_DIR", tmp_path)
    db_path = tmp_path / "state" / "enterprise.sqlite"
    identity = IdentityService.from_path(db_path)
    identity.create_organization(org_id="ORG-001", name="Acme")
    ledger = EnterpriseAuditLedger.from_path(db_path, org_id="ORG-001")
    ledger.append(event_type="auth", action="login", status="success", actor_id="usr-1")
    checkpoint = tmp_path / "state" / "audit_live_export_checkpoint.json"
    lock_path = checkpoint.with_suffix(checkpoint.suffix + ".lock")
    lock_path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="lock is already held"):
        hashi.cmd_enterprise_audit_export_live(
            SimpleNamespace(
                endpoint="https://siem.example.com/ingest",
                format="ledger",
                db=None,
                org_id="ORG-001",
                checkpoint=None,
                lock_path=None,
                batch_size=10,
                timeout=5.0,
                max_attempts=1,
                backoff=0.0,
                header=[],
                daemon=False,
            )
        )


def test_enterprise_audit_live_export_cli_rejects_held_db_lease(tmp_path, monkeypatch):
    monkeypatch.setattr(hashi, "ROOT_DIR", tmp_path)
    db_path = tmp_path / "state" / "enterprise.sqlite"
    identity = IdentityService.from_path(db_path)
    identity.create_organization(org_id="ORG-001", name="Acme")
    ledger = EnterpriseAuditLedger.from_path(db_path, org_id="ORG-001")
    ledger.append(event_type="auth", action="login", status="success", actor_id="usr-1")
    EnterpriseLeaseStore.from_path(db_path, org_id="ORG-001").acquire(
        "audit-export",
        holder_id="pod-a",
        ttl_seconds=60,
    )

    with pytest.raises(ValueError, match="DB lease is already held"):
        hashi.cmd_enterprise_audit_export_live(
            SimpleNamespace(
                endpoint="https://siem.example.com/ingest",
                format="ledger",
                db=None,
                org_id="ORG-001",
                checkpoint=None,
                lock_path=None,
                db_lease_name="audit-export",
                db_lease_holder="pod-b",
                db_lease_ttl=30,
                batch_size=10,
                timeout=5.0,
                max_attempts=1,
                backoff=0.0,
                header=[],
                daemon=False,
            )
        )


def test_enterprise_audit_live_export_cli_rejects_lock_path_matching_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(hashi, "ROOT_DIR", tmp_path)
    db_path = tmp_path / "state" / "enterprise.sqlite"
    identity = IdentityService.from_path(db_path)
    identity.create_organization(org_id="ORG-001", name="Acme")
    checkpoint = tmp_path / "state" / "audit_live_export_checkpoint.json"

    with pytest.raises(ValueError, match="lock path must differ"):
        hashi.cmd_enterprise_audit_export_live(
            SimpleNamespace(
                endpoint="https://siem.example.com/ingest",
                format="ledger",
                db=None,
                org_id="ORG-001",
                checkpoint=str(checkpoint),
                lock_path=str(checkpoint),
                batch_size=10,
                timeout=5.0,
                max_attempts=1,
                backoff=0.0,
                header=[],
                daemon=False,
            )
        )


def test_enterprise_audit_live_export_cli_accepts_vendor_formats():
    seen = []

    def fake_export(args):
        seen.append(args.format)
        return 0

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(hashi, "cmd_enterprise_audit_export_live", fake_export)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "hashi",
                "enterprise",
                "audit-export-live",
                "--endpoint",
                "https://splunk.example.com/services/collector/event",
                "--format",
                "splunk-hec",
            ],
        )
        with pytest.raises(SystemExit) as splunk_exit:
            hashi.main()
        assert splunk_exit.value.code == 0
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "hashi",
                "enterprise",
                "audit-export-live",
                "--endpoint",
                "https://elastic.example.com/hashi-audit/_bulk",
                "--format",
                "elastic-bulk",
            ],
        )
        with pytest.raises(SystemExit) as elastic_exit:
            hashi.main()
        assert elastic_exit.value.code == 0
    finally:
        monkeypatch.undo()

    assert seen == ["splunk-hec", "elastic-bulk"]


def test_enterprise_audit_live_export_cli_rejects_malformed_header():
    with pytest.raises(ValueError, match="Name: value"):
        hashi._parse_http_headers(["broken"])
