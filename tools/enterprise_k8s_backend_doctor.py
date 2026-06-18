from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def run_doctor(
    repo_root: Path | str = ROOT,
    *,
    require_installed: bool = False,
    import_name: str = "kubernetes",
) -> dict[str, Any]:
    root = Path(repo_root)
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    dockerfile = (root / "Dockerfile.enterprise").read_text(encoding="utf-8")
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    module_installed = importlib.util.find_spec(import_name) is not None
    checks = {
        "pyproject_extra": 'kubernetes = ["kubernetes>=29.0.0,<32.0.0"]' in pyproject,
        "requirements_comment": "Optional: Kubernetes Lease scheduler backend" in requirements,
        "docker_build_arg": 'ARG HASHI_ENTERPRISE_EXTRAS=""' in dockerfile,
        "docker_installs_extra": 'pip install --no-cache-dir ".[${HASHI_ENTERPRISE_EXTRAS}]"' in dockerfile,
        "module_installed": module_installed,
    }
    required = [
        "pyproject_extra",
        "requirements_comment",
        "docker_build_arg",
        "docker_installs_extra",
    ]
    if require_installed:
        required.append("module_installed")
    missing = [name for name in required if not checks[name]]
    return {
        "ok": not missing,
        "checks": checks,
        "missing": missing,
        "require_installed": require_installed,
        "install_hint": "pip install 'hashi-bridge[kubernetes]' or build with HASHI_ENTERPRISE_EXTRAS=kubernetes",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Kubernetes Lease backend packaging prerequisites")
    parser.add_argument("--repo-root", default=str(ROOT), help="Repository root. Defaults to this checkout.")
    parser.add_argument(
        "--require-installed",
        action="store_true",
        help="Fail unless the Kubernetes Python package is importable in this environment.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args(argv)
    result = run_doctor(args.repo_root, require_installed=args.require_installed)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        status = "ok" if result["ok"] else "failed"
        print(f"Kubernetes Lease backend packaging doctor: {status}")
        for name, value in result["checks"].items():
            print(f"  {name}: {value}")
        if result["missing"]:
            print(f"  missing: {', '.join(result['missing'])}")
            print(f"  hint: {result['install_hint']}")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
