from __future__ import annotations

import json

from tools.enterprise_production_validation_plan import build_production_validation_plan, main


def test_enterprise_production_validation_plan_contains_required_steps():
    plan = build_production_validation_plan(
        release_name="hashi-prod",
        namespace="prod",
        image_tag="sha-prod",
        host="hashi.example.com",
        ingress_namespace="edge",
    )

    assert plan["schema_version"] == 1
    assert plan["release_name"] == "hashi-prod"
    assert plan["namespace"] == "prod"
    assert plan["host"] == "hashi.example.com"
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
    assert any("production-hardening.values.yaml" in arg for arg in plan["steps"][0]["argv"])
    assert "kind: (Ingress|NetworkPolicy|HorizontalPodAutoscaler|PodDisruptionBudget)" in plan["steps"][1]["argv"]
    assert "edge" in plan["steps"][2]["argv"]
    assert "image.tag=sha-prod" in plan["steps"][3]["argv"]
    assert "https://hashi.example.com/api/health" in plan["steps"][8]["argv"]


def test_enterprise_production_validation_plan_cli_writes_json(tmp_path):
    output = tmp_path / "production-plan.json"

    rc = main(
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
    )

    assert rc == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["release_name"] == "hashi-prod"
    assert payload["namespace"] == "prod"
    assert payload["image_tag"] == "sha-prod"
    assert payload["host"] == "hashi.example.com"
    assert payload["ingress_namespace"] == "edge"
