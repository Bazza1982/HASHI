from __future__ import annotations

import json

from tools.enterprise_k8s_ha_rehearsal_plan import build_ha_rehearsal_plan, main


def test_enterprise_k8s_ha_rehearsal_plan_contains_required_steps():
    plan = build_ha_rehearsal_plan(
        release_name="hashi-staging",
        namespace="staging",
        image_repository="ghcr.io/example/hashi-enterprise",
        image_tag="sha-test",
        lease_load_count=12,
        lease_load_workers=3,
    )

    assert plan["schema_version"] == 1
    assert plan["release_name"] == "hashi-staging"
    assert plan["namespace"] == "staging"
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
    rendered = plan["steps"][1]["argv"]
    lease_job = plan["steps"][4]["argv"]
    assert "leaseLoadRehearsal.enabled=true" in rendered
    assert "leaseLoadRehearsal.leaseCount=12" in lease_job
    assert "leaseLoadRehearsal.maxWorkers=3" in lease_job
    assert "image.tag=sha-test" in plan["steps"][2]["argv"]


def test_enterprise_k8s_ha_rehearsal_plan_cli_writes_json(tmp_path):
    output = tmp_path / "ha-plan.json"

    rc = main(
        [
            "--release-name",
            "hashi-staging",
            "--namespace",
            "staging",
            "--image-tag",
            "sha-123",
            "--lease-load-count",
            "10",
            "--lease-load-workers",
            "2",
            "--output",
            str(output),
        ]
    )

    assert rc == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["release_name"] == "hashi-staging"
    assert payload["namespace"] == "staging"
    assert payload["image_tag"] == "sha-123"
    assert payload["lease_load_count"] == 10
    assert payload["lease_load_workers"] == 2
