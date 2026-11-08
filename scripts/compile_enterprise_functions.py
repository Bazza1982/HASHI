#!/usr/bin/env python3
"""Resolve optional enterprise Functions to an exact platform wheel hash lock."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def compile_profile(extras, output):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("Use a new build lock output")
    names = [name.strip() for name in extras.split(",") if name.strip()]
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    groups = project["optional-dependencies"]
    if any(name not in groups or name == "test" for name in names):
        raise ValueError("Unknown/non-runtime enterprise extra")
    canonical = ROOT / "constraints/functions-py312.hashes.lock"
    output.parent.mkdir(parents=True, exist_ok=True)
    if not names:
        output.write_bytes(canonical.read_bytes())
        return output
    report = output.with_suffix(".pip-report.json")
    requirements = [value for name in names for value in groups[name]]
    # This is a fresh image build interpreter. --dry-run --ignore-installed
    # resolves wheel metadata only; no existing environment is modified.
    subprocess.run([
        sys.executable, "-B", "-m", "pip", "install", "--dry-run", "--ignore-installed",
        "--only-binary=:all:", "--report", str(report),
        "-r", str(ROOT / "constraints/standard-py312.lock"), *requirements,
    ], check=True, timeout=900, capture_output=True, text=True)
    resolved = json.loads(report.read_bytes())["install"]
    lines = []
    seen = set()
    for item in resolved:
        name, version = item["metadata"]["name"], item["metadata"]["version"]
        normalized = name.lower().replace("_", "-").replace(".", "-")
        digest = item["download_info"]["archive_info"]["hashes"]["sha256"]
        if normalized in seen or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Ambiguous package or missing wheel SHA256 in resolution")
        seen.add(normalized)
        lines.append(f"{normalized}=={version} " + chr(92) + f"\n    --hash=sha256:{digest}")
    output.write_text("\n".join(sorted(lines)) + "\n", encoding="utf-8")
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extras", default="")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    print(compile_profile(args.extras, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# HASHI-Fingerprint: hfp_e3062f529e4d432c965b5609936767db
