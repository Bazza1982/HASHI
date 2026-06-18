from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def build_ha_rehearsal_plan(
    *,
    release_name: str = "hashi-enterprise",
    chart_path: str = "deploy/helm/hashi-enterprise",
    namespace: str = "hashi-enterprise",
    image_repository: str = "ghcr.io/example/hashi-enterprise",
    image_tag: str = "replace-me",
    values_path: str = "deploy/helm/hashi-enterprise/examples/multi-replica-rehearsal.values.yaml",
    database_secret_name: str = "hashi-enterprise-database",
    lease_load_count: int = 16,
    lease_load_workers: int = 4,
) -> dict[str, Any]:
    helm_base = [
        "helm",
        "upgrade",
        "--install",
        release_name,
        chart_path,
        "--namespace",
        namespace,
        "--create-namespace",
        "--values",
        values_path,
        "--set",
        f"image.repository={image_repository}",
        "--set",
        f"image.tag={image_tag}",
    ]
    steps = [
        {
            "id": "postgres-secret-check",
            "description": "Confirm the managed database URL Secret exists in the rehearsal namespace.",
            "argv": [
                "kubectl",
                "-n",
                namespace,
                "get",
                "secret",
                database_secret_name,
                "-o",
                "jsonpath={.data.HASHI_ENTERPRISE_DATABASE_URL}",
            ],
            "required": True,
        },
        {
            "id": "helm-render-check",
            "description": "Render the chart with the lease load Job enabled before changing the cluster.",
            "argv": [
                "helm",
                "template",
                release_name,
                chart_path,
                "--namespace",
                namespace,
                "--values",
                values_path,
                "--set",
                "externalDatabase.enabled=true",
                "--set",
                "leaseLoadRehearsal.enabled=true",
                "--set",
                f"leaseLoadRehearsal.leaseCount={lease_load_count}",
                "--set",
                f"leaseLoadRehearsal.maxWorkers={lease_load_workers}",
            ],
            "required": True,
        },
        {
            "id": "helm-upgrade",
            "description": "Install or upgrade the multi-replica rehearsal release.",
            "argv": helm_base,
            "required": True,
        },
        {
            "id": "rollout-status",
            "description": "Wait for the control-plane Deployment rollout.",
            "argv": ["kubectl", "-n", namespace, "rollout", "status", f"deploy/{release_name}"],
            "required": True,
        },
        {
            "id": "lease-load-job",
            "description": "Enable and run the in-cluster bounded lease load rehearsal Job.",
            "argv": helm_base
            + [
                "--set",
                "externalDatabase.enabled=true",
                "--set",
                "leaseLoadRehearsal.enabled=true",
                "--set",
                f"leaseLoadRehearsal.leaseCount={lease_load_count}",
                "--set",
                f"leaseLoadRehearsal.maxWorkers={lease_load_workers}",
            ],
            "required": True,
        },
        {
            "id": "lease-load-logs",
            "description": "Inspect the lease load rehearsal Job output.",
            "argv": ["kubectl", "-n", namespace, "logs", f"job/{release_name}-lease-load-rehearsal"],
            "required": True,
        },
        {
            "id": "scheduler-env-check",
            "description": "Confirm scheduler lease environment variables are present in the running pod.",
            "argv": [
                "kubectl",
                "-n",
                namespace,
                "exec",
                f"deploy/{release_name}",
                "--",
                "env",
                "|",
                "grep",
                "HASHI_ENTERPRISE_SCHEDULER_LEASE",
            ],
            "required": True,
        },
        {
            "id": "delete-one-pod",
            "description": "Delete one control-plane pod to rehearse replacement without duplicate scheduler work.",
            "argv": [
                "kubectl",
                "-n",
                namespace,
                "delete",
                "pod",
                "$(kubectl",
                "-n",
                namespace,
                "get",
                "pods",
                "-l",
                "app.kubernetes.io/component=enterprise",
                "-o",
                "jsonpath={.items[0].metadata.name})",
            ],
            "required": False,
        },
        {
            "id": "scheduler-lease-logs",
            "description": "Review scheduler lease acquisition/skip logs after failover rehearsal.",
            "argv": [
                "kubectl",
                "-n",
                namespace,
                "logs",
                f"deploy/{release_name}",
                "--tail=200",
                "|",
                "grep",
                "-E",
                "scheduler.*lease|Skipping scheduler tick",
            ],
            "required": False,
        },
        {
            "id": "rollback-single-replica",
            "description": "Rollback rehearsal settings to a single replica if validation fails.",
            "argv": [
                "helm",
                "upgrade",
                release_name,
                chart_path,
                "--namespace",
                namespace,
                "--set",
                "replicaCount=1",
                "--set",
                "schedulerLease.enabled=false",
                "--set",
                "schedulerLease.pool.enabled=false",
                "--set",
                "leaseLoadRehearsal.enabled=false",
                "--set",
                "podDisruptionBudget.enabled=false",
            ],
            "required": False,
        },
    ]
    return {
        "schema_version": 1,
        "release_name": release_name,
        "chart_path": chart_path,
        "namespace": namespace,
        "image_repository": image_repository,
        "image_tag": image_tag,
        "values_path": values_path,
        "database_secret_name": database_secret_name,
        "lease_load_count": lease_load_count,
        "lease_load_workers": lease_load_workers,
        "steps": steps,
        "notes": [
            "This plan generates commands only; it does not execute Helm or kubectl.",
            "Run against staging first, with a managed PostgreSQL database and initialized enterprise schema.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate enterprise Kubernetes HA rehearsal commands")
    parser.add_argument("--release-name", default="hashi-enterprise", help="Helm release name.")
    parser.add_argument("--chart-path", default="deploy/helm/hashi-enterprise", help="Helm chart path.")
    parser.add_argument("--namespace", default="hashi-enterprise", help="Kubernetes namespace.")
    parser.add_argument("--image-repository", default="ghcr.io/example/hashi-enterprise", help="Image repository.")
    parser.add_argument("--image-tag", default="replace-me", help="Image tag.")
    parser.add_argument(
        "--values-path",
        default="deploy/helm/hashi-enterprise/examples/multi-replica-rehearsal.values.yaml",
        help="Values file for the HA rehearsal.",
    )
    parser.add_argument("--database-secret-name", default="hashi-enterprise-database", help="Database Secret name.")
    parser.add_argument("--lease-load-count", type=int, default=16, help="Lease count for load rehearsal.")
    parser.add_argument("--lease-load-workers", type=int, default=4, help="Worker count for load rehearsal.")
    parser.add_argument("--output", help="Optional JSON output path.")
    args = parser.parse_args(argv)

    plan = build_ha_rehearsal_plan(
        release_name=args.release_name,
        chart_path=args.chart_path,
        namespace=args.namespace,
        image_repository=args.image_repository,
        image_tag=args.image_tag,
        values_path=args.values_path,
        database_secret_name=args.database_secret_name,
        lease_load_count=max(1, args.lease_load_count),
        lease_load_workers=max(1, args.lease_load_workers),
    )
    text = json.dumps(plan, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
