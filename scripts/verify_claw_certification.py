#!/usr/bin/env python3
"""Verify the pinned HASHI Claw source against its exact certification baseline."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "hashi_assets" / "claw" / "manifest.json"
BASELINE_PATH = PROJECT_ROOT / "hashi_assets" / "claw" / "certification_baseline.json"


class CertificationError(RuntimeError):
    pass


def _load_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CertificationError(f"Cannot load {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CertificationError(f"Expected a JSON object in {path}")
    return payload


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def _git_output(source_root: Path, *args: str) -> str:
    result = _run(["git", *args], source_root)
    if result.returncode != 0:
        raise CertificationError(f"git {' '.join(args)} failed:\n{result.stdout}")
    return result.stdout.strip()


def _verify_metadata(source_root: Path, manifest: dict, baseline: dict) -> Path:
    for key in ("runtime_version", "upstream_commit", "source_commit"):
        manifest_key = "version" if key == "runtime_version" else key
        if baseline.get(key) != manifest.get(manifest_key):
            raise CertificationError(
                f"Baseline {key}={baseline.get(key)!r} does not match "
                f"manifest {manifest_key}={manifest.get(manifest_key)!r}"
            )

    source_commit = str(baseline["source_commit"])
    upstream_commit = str(baseline["upstream_commit"])
    actual_head = _git_output(source_root, "rev-parse", "HEAD")
    if actual_head != source_commit:
        raise CertificationError(f"Source HEAD {actual_head} is not certified {source_commit}")
    dirty = _git_output(source_root, "status", "--porcelain")
    if dirty:
        raise CertificationError(f"Certified Claw source is dirty:\n{dirty}")

    ancestor = _run(
        ["git", "merge-base", "--is-ancestor", upstream_commit, source_commit],
        source_root,
    )
    if ancestor.returncode != 0:
        raise CertificationError(f"Pinned upstream {upstream_commit} is not an ancestor of {source_commit}")

    rust_root = source_root / "rust"
    if not (rust_root / "Cargo.toml").is_file():
        raise CertificationError(f"Rust workspace not found at {rust_root}")
    return rust_root


def _verify_workspace_tests(rust_root: Path, baseline: dict) -> None:
    rust_baseline = baseline["rust_workspace"]
    allowed = rust_baseline["expected_upstream_failures"]
    if len(allowed) != 1:
        raise CertificationError(f"Expected exactly one Rust test exception, found {len(allowed)}")

    all_other = _run(list(rust_baseline["all_other_tests_command"]), rust_root)
    if all_other.returncode != 0:
        raise CertificationError(f"Rust workspace has a non-baselined failure:\n{all_other.stdout}")

    item = allowed[0]
    command = [
        "cargo",
        "test",
        "-p",
        str(item["package"]),
        "--test",
        str(item["target"]),
        str(item["test"]),
        "--",
        "--exact",
        "--nocapture",
    ]
    expected = _run(command, rust_root)
    if expected.returncode == 0:
        raise CertificationError(
            f"Baselined Rust test {item['test']} now passes; remove the stale exception"
        )
    missing = [value for value in item["evidence"] if value not in expected.stdout]
    if missing:
        raise CertificationError(
            f"Baselined Rust test failed differently; missing evidence {missing}:\n{expected.stdout}"
        )


def _parse_clippy_diagnostics(output: str) -> list[tuple[str, str, int, str]]:
    block_pattern = re.compile(
        r"^error:.*?^\s*-->\s+([^\n]+?):(\d+):\d+\n(.*?)(?=^error:|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    diagnostics: list[tuple[str, str, int, str]] = []
    for match in block_pattern.finditer(output):
        lint_match = re.search(r"index\.html#([a-z0-9_]+)", match.group(3))
        if lint_match is None:
            continue
        path = match.group(1)
        parts = Path(path).parts
        package = parts[1] if len(parts) > 1 and parts[0] == "crates" else ""
        diagnostics.append((package, path, int(match.group(2)), lint_match.group(1)))
    return diagnostics


def _verify_clippy(rust_root: Path, baseline: dict) -> None:
    clippy_baseline = baseline["clippy"]
    expected = sorted(
        (
            str(item["package"]),
            str(item["path"]),
            int(item["line"]),
            str(item["lint"]),
        )
        for item in clippy_baseline["expected_upstream_diagnostics"]
    )
    if len(expected) != 6 or len(set(expected)) != 6:
        raise CertificationError("Clippy baseline must contain exactly six unique diagnostics")

    result = _run(list(clippy_baseline["command"]), rust_root)
    if result.returncode == 0:
        raise CertificationError("Clippy now passes; remove the stale diagnostic baseline")
    actual = sorted(_parse_clippy_diagnostics(result.stdout))
    if actual != expected:
        raise CertificationError(
            "Clippy diagnostics differ from the certified baseline.\n"
            f"Expected: {expected}\nActual:   {actual}\n\n{result.stdout}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Verify commit and baseline linkage without running Cargo.",
    )
    args = parser.parse_args()

    try:
        manifest = _load_json(MANIFEST_PATH)
        baseline = _load_json(BASELINE_PATH)
        rust_root = _verify_metadata(args.source_root.resolve(), manifest, baseline)
        if not args.metadata_only:
            _verify_workspace_tests(rust_root, baseline)
            _verify_clippy(rust_root, baseline)
    except CertificationError as exc:
        print(f"Claw certification FAILED: {exc}", file=sys.stderr)
        return 1

    mode = "metadata" if args.metadata_only else "full"
    print(
        f"Claw certification OK ({mode}): {baseline['runtime_version']} "
        f"source={baseline['source_commit']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
