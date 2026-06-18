from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def build_production_validation_plan(
    *,
    release_name: str = "hashi-enterprise",
    chart_path: str = "deploy/helm/hashi-enterprise",
    namespace: str = "hashi-enterprise",
    image_repository: str = "ghcr.io/example/hashi-enterprise",
    image_tag: str = "replace-me",
    host: str = "hashi-enterprise.example.com",
    ingress_namespace: str = "ingress-nginx",
    values_paths: tuple[str, ...] = (
        "deploy/helm/hashi-enterprise/examples/multi-replica-rehearsal.values.yaml",
        "deploy/helm/hashi-enterprise/examples/production-hardening.values.yaml",
    ),
) -> dict[str, Any]:
    values_args: list[str] = []
    for values_path in values_paths:
        values_args.extend(["--values", values_path])

    helm_base = [
        "helm",
        "upgrade",
        "--install",
        release_name,
        chart_path,
        "--namespace",
        namespace,
        "--create-namespace",
        *values_args,
        "--set",
        f"image.repository={image_repository}",
        "--set",
        f"image.tag={image_tag}",
    ]
    render_output = "/tmp/hashi-enterprise-production-render.yaml"
    steps = [
        {
            "id": "render-production-hardening",
            "description": "Render the chart with HA and production hardening values before applying.",
            "argv": [
                "helm",
                "template",
                release_name,
                chart_path,
                "--namespace",
                namespace,
                *values_args,
                "--set",
                f"image.repository={image_repository}",
                "--set",
                f"image.tag={image_tag}",
                ">",
                render_output,
            ],
            "required": True,
        },
        {
            "id": "render-resource-check",
            "description": "Confirm rendered hardening resources are present.",
            "argv": [
                "grep",
                "-E",
                "kind: (Ingress|NetworkPolicy|HorizontalPodAutoscaler|PodDisruptionBudget)",
                render_output,
            ],
            "required": True,
        },
        {
            "id": "ingress-namespace-label-check",
            "description": "Confirm the ingress namespace label used by NetworkPolicy selectors.",
            "argv": ["kubectl", "get", "namespace", ingress_namespace, "--show-labels"],
            "required": True,
        },
        {
            "id": "helm-upgrade-production-hardening",
            "description": "Apply production hardening values after render review.",
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
            "id": "resource-inventory",
            "description": "List production hardening resources in the namespace.",
            "argv": ["kubectl", "-n", namespace, "get", "ingress,networkpolicy,hpa,pdb"],
            "required": True,
        },
        {
            "id": "hpa-describe",
            "description": "Inspect HPA metrics target and replica bounds.",
            "argv": ["kubectl", "-n", namespace, "describe", "hpa", release_name],
            "required": True,
        },
        {
            "id": "networkpolicy-describe",
            "description": "Inspect NetworkPolicy ingress and egress rules.",
            "argv": ["kubectl", "-n", namespace, "describe", "networkpolicy", release_name],
            "required": True,
        },
        {
            "id": "https-health-check",
            "description": "Confirm public HTTPS ingress reaches the health endpoint.",
            "argv": ["curl", "-fsS", f"https://{host}/api/health"],
            "required": True,
        },
        {
            "id": "rollback-hardening-controls",
            "description": "Disable ingress, NetworkPolicy, autoscaling, and PDB if validation fails.",
            "argv": [
                "helm",
                "upgrade",
                release_name,
                chart_path,
                "--namespace",
                namespace,
                "--set",
                "ingress.enabled=false",
                "--set",
                "networkPolicy.enabled=false",
                "--set",
                "autoscaling.enabled=false",
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
        "host": host,
        "ingress_namespace": ingress_namespace,
        "values_paths": list(values_paths),
        "steps": steps,
        "notes": [
            "This plan generates commands only; it does not execute Helm, kubectl, or curl.",
            "Review and adapt ingress controller, DNS, TLS Secret, CNI, and HPA metrics assumptions before use.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate enterprise production hardening validation commands")
    parser.add_argument("--release-name", default="hashi-enterprise", help="Helm release name.")
    parser.add_argument("--chart-path", default="deploy/helm/hashi-enterprise", help="Helm chart path.")
    parser.add_argument("--namespace", default="hashi-enterprise", help="Kubernetes namespace.")
    parser.add_argument("--image-repository", default="ghcr.io/example/hashi-enterprise", help="Image repository.")
    parser.add_argument("--image-tag", default="replace-me", help="Image tag.")
    parser.add_argument("--host", default="hashi-enterprise.example.com", help="Public HTTPS host to check.")
    parser.add_argument("--ingress-namespace", default="ingress-nginx", help="Ingress controller namespace.")
    parser.add_argument(
        "--values",
        action="append",
        dest="values_paths",
        help="Values file to include. May be provided multiple times.",
    )
    parser.add_argument("--output", help="Optional JSON output path.")
    args = parser.parse_args(argv)

    plan = build_production_validation_plan(
        release_name=args.release_name,
        chart_path=args.chart_path,
        namespace=args.namespace,
        image_repository=args.image_repository,
        image_tag=args.image_tag,
        host=args.host,
        ingress_namespace=args.ingress_namespace,
        values_paths=tuple(args.values_paths) if args.values_paths else (
            "deploy/helm/hashi-enterprise/examples/multi-replica-rehearsal.values.yaml",
            "deploy/helm/hashi-enterprise/examples/production-hardening.values.yaml",
        ),
    )
    text = json.dumps(plan, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
