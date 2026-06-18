from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.enterprise_k8s_backend_doctor import run_doctor


def build_image_smoke_plan(
    repo_root: Path | str = ROOT,
    *,
    image_tag: str = "hashi-enterprise:k8s-lease-smoke",
    namespace: str = "hashi-enterprise",
    lease_name: str = "superloop-scheduler-smoke",
) -> dict[str, Any]:
    root = Path(repo_root)
    doctor = run_doctor(root)
    steps = [
        {
            "id": "packaging-doctor",
            "description": "Validate optional Kubernetes backend packaging contract.",
            "argv": ["python", "tools/enterprise_k8s_backend_doctor.py", "--json"],
            "required": True,
        },
        {
            "id": "docker-build",
            "description": "Build an enterprise image that installs the Kubernetes optional extra.",
            "argv": [
                "docker",
                "build",
                "-f",
                "Dockerfile.enterprise",
                "--build-arg",
                "HASHI_ENTERPRISE_EXTRAS=kubernetes",
                "-t",
                image_tag,
                ".",
            ],
            "required": True,
        },
        {
            "id": "image-import-check",
            "description": "Confirm the built image can import the Kubernetes Python package.",
            "argv": [
                "docker",
                "run",
                "--rm",
                image_tag,
                "python",
                "-c",
                "import kubernetes; print(kubernetes.__version__)",
            ],
            "required": True,
        },
        {
            "id": "cli-help-check",
            "description": "Confirm the Kubernetes Lease rehearsal CLI is present in the image.",
            "argv": [
                "docker",
                "run",
                "--rm",
                image_tag,
                "python",
                "hashi.py",
                "enterprise",
                "k8s-lease-rehearse",
                "--help",
            ],
            "required": True,
        },
        {
            "id": "cluster-smoke",
            "description": "Run the Lease smoke rehearsal against a target cluster using a mounted kubeconfig.",
            "argv": [
                "docker",
                "run",
                "--rm",
                "-v",
                "${KUBECONFIG}:/kubeconfig:ro",
                "-e",
                "KUBECONFIG=/kubeconfig",
                image_tag,
                "python",
                "hashi.py",
                "enterprise",
                "k8s-lease-rehearse",
                "--namespace",
                namespace,
                "--lease-name",
                lease_name,
                "--kubeconfig",
                "/kubeconfig",
            ],
            "required": False,
        },
    ]
    return {
        "schema_version": 1,
        "repo_root": str(root),
        "image_tag": image_tag,
        "namespace": namespace,
        "lease_name": lease_name,
        "doctor": doctor,
        "steps": steps,
        "notes": [
            "This plan does not execute Docker or contact Kubernetes.",
            "Run cluster-smoke only with a kubeconfig that has coordination.k8s.io/leases permissions.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate Kubernetes Lease backend image smoke commands")
    parser.add_argument("--repo-root", default=str(ROOT), help="Repository root. Defaults to this checkout.")
    parser.add_argument("--image-tag", default="hashi-enterprise:k8s-lease-smoke", help="Image tag to use in commands.")
    parser.add_argument("--namespace", default="hashi-enterprise", help="Kubernetes namespace for the smoke Lease.")
    parser.add_argument("--lease-name", default="superloop-scheduler-smoke", help="Lease name for the smoke rehearsal.")
    parser.add_argument("--output", help="Optional JSON output path.")
    args = parser.parse_args(argv)
    plan = build_image_smoke_plan(
        args.repo_root,
        image_tag=args.image_tag,
        namespace=args.namespace,
        lease_name=args.lease_name,
    )
    text = json.dumps(plan, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0 if plan["doctor"]["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
