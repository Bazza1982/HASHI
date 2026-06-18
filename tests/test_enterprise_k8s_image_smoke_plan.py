from __future__ import annotations

import json

from tools.enterprise_k8s_image_smoke_plan import build_image_smoke_plan, main


def test_kubernetes_image_smoke_plan_contains_required_steps():
    plan = build_image_smoke_plan(
        image_tag="ghcr.io/example/hashi-enterprise:k8s",
        namespace="hashi-enterprise",
        lease_name="scheduler-smoke",
    )

    assert plan["schema_version"] == 1
    assert plan["doctor"]["ok"] is True
    assert [step["id"] for step in plan["steps"]] == [
        "packaging-doctor",
        "docker-build",
        "image-import-check",
        "cli-help-check",
        "cluster-smoke",
    ]
    docker_build = plan["steps"][1]["argv"]
    cluster_smoke = plan["steps"][-1]
    assert "HASHI_ENTERPRISE_EXTRAS=kubernetes" in docker_build
    assert "ghcr.io/example/hashi-enterprise:k8s" in docker_build
    assert cluster_smoke["required"] is False
    assert "scheduler-smoke" in cluster_smoke["argv"]


def test_kubernetes_image_smoke_plan_cli_writes_json(tmp_path):
    output = tmp_path / "plan.json"

    rc = main(
        [
            "--image-tag",
            "hashi:test",
            "--namespace",
            "smoke-ns",
            "--lease-name",
            "lease-smoke",
            "--output",
            str(output),
        ]
    )

    assert rc == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["image_tag"] == "hashi:test"
    assert payload["namespace"] == "smoke-ns"
    assert payload["lease_name"] == "lease-smoke"
