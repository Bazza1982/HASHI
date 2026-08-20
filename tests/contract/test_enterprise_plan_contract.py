from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.enterprise_k8s_ha_rehearsal_plan import main as ha_plan_main
from tools.enterprise_production_validation_plan import main as production_plan_main


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.contract


def test_ha_rehearsal_cli_preserves_required_failover_sequence(tmp_path: Path) -> None:
    output = tmp_path / "ha-plan.json"

    assert ha_plan_main(
        [
            "--release-name",
            "hashi-staging",
            "--namespace",
            "staging",
            "--image-tag",
            "sha-123",
            "--lease-load-count",
            "12",
            "--lease-load-workers",
            "3",
            "--output",
            str(output),
        ]
    ) == 0

    plan = json.loads(output.read_text(encoding="utf-8"))
    assert [step["id"] for step in plan["steps"]] == [
        "postgres-secret-check",
        "helm-render-check",
        "helm-upgrade",
        "rollout-status",
        "lease-load-job",
        "lease-load-logs",
        "scheduler-env-check",
        "delete-one-pod",
        "scheduler-lease-logs",
        "rollback-single-replica",
    ]
    assert all(step["required"] for step in plan["steps"][:7])
    assert not any(step["required"] for step in plan["steps"][7:])
    assert "leaseLoadRehearsal.leaseCount=12" in plan["steps"][4]["argv"]
    assert "leaseLoadRehearsal.maxWorkers=3" in plan["steps"][4]["argv"]
    assert "image.tag=sha-123" in plan["steps"][2]["argv"]


def test_image_smoke_cli_runs_without_site_packages_and_keeps_cluster_optional(
    tmp_path: Path,
) -> None:
    output = tmp_path / "image-plan.json"

    result = subprocess.run(
        [
            sys.executable,
            "-S",
            str(ROOT / "tools" / "enterprise_k8s_image_smoke_plan.py"),
            "--repo-root",
            str(ROOT),
            "--image-tag",
            "hashi:test",
            "--namespace",
            "smoke-ns",
            "--lease-name",
            "lease-smoke",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert plan["doctor"]["ok"] is True
    assert [step["id"] for step in plan["steps"]] == [
        "packaging-doctor",
        "docker-build",
        "image-import-check",
        "cli-help-check",
        "cluster-smoke",
    ]
    assert plan["steps"][-1]["required"] is False
    assert "hashi:test" in plan["steps"][1]["argv"]
    assert "lease-smoke" in plan["steps"][-1]["argv"]


def test_production_validation_cli_keeps_hardening_and_rollback_boundaries(
    tmp_path: Path,
) -> None:
    output = tmp_path / "production-plan.json"

    assert production_plan_main(
        [
            "--release-name",
            "hashi-prod",
            "--namespace",
            "prod",
            "--image-tag",
            "sha-prod",
            "--host",
            "hashi.example.com",
            "--ingress-namespace",
            "edge",
            "--output",
            str(output),
        ]
    ) == 0

    plan = json.loads(output.read_text(encoding="utf-8"))
    assert [step["id"] for step in plan["steps"]] == [
        "render-production-hardening",
        "render-resource-check",
        "ingress-namespace-label-check",
        "helm-upgrade-production-hardening",
        "rollout-status",
        "resource-inventory",
        "hpa-describe",
        "networkpolicy-describe",
        "https-health-check",
        "rollback-hardening-controls",
    ]
    assert all(step["required"] for step in plan["steps"][:9])
    assert plan["steps"][-1]["required"] is False
    assert "edge" in plan["steps"][2]["argv"]
    assert "image.tag=sha-prod" in plan["steps"][3]["argv"]
    assert "https://hashi.example.com/api/health" in plan["steps"][8]["argv"]
