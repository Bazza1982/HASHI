#!/usr/bin/env python3
"""Verify HER's pinned upstream Claw source against its certification baseline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HER_ROOT = PROJECT_ROOT / "hashi_assets" / "her"
RELEASES_ROOT = HER_ROOT / "releases"
MANIFEST_PATH = HER_ROOT / "manifest.json"
BASELINE_PATH = HER_ROOT / "certification_baseline.json"
CANDIDATE_MANIFEST_NAME = "release_manifest.json"
CANDIDATE_BASELINE_NAME = "release_certification_baseline.json"


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


def _run(
    command: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_inside(root: Path, relative: str, *, label: str) -> Path:
    root = root.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise CertificationError(f"{label} escapes {root}: {relative}")
    return path


def _release_inputs(release_dir: Path | None) -> tuple[dict, dict, Path]:
    if release_dir is None:
        manifest = _load_json(MANIFEST_PATH)
        baseline = _load_json(BASELINE_PATH)
        resolved_release = _resolve_inside(
            RELEASES_ROOT,
            str(manifest["version"]),
            label="active release directory",
        )
        return manifest, baseline, resolved_release

    resolved_release = release_dir.resolve()
    if not resolved_release.is_relative_to(RELEASES_ROOT.resolve()):
        raise CertificationError(f"Candidate release directory is outside {RELEASES_ROOT}")
    manifest = _load_json(resolved_release / CANDIDATE_MANIFEST_NAME)
    baseline = _load_json(resolved_release / CANDIDATE_BASELINE_NAME)
    if resolved_release.name != manifest.get("version"):
        raise CertificationError(
            f"Candidate directory {resolved_release.name!r} does not match version {manifest.get('version')!r}"
        )
    return manifest, baseline, resolved_release


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

    source_branch = str(manifest.get("source_branch") or "")
    actual_branch = _git_output(source_root, "branch", "--show-current")
    if source_branch and actual_branch != source_branch:
        raise CertificationError(f"Source branch {actual_branch!r} is not declared {source_branch!r}")

    rust_root = source_root / "rust"
    if not (rust_root / "Cargo.toml").is_file():
        raise CertificationError(f"Rust workspace not found at {rust_root}")
    return rust_root


def _verify_release_evidence(
    source_root: Path,
    release_dir: Path,
    manifest: dict,
) -> dict | None:
    evidence_relative = manifest.get("certification_evidence")
    if evidence_relative is None:
        return None
    evidence_path = _resolve_inside(HER_ROOT, str(evidence_relative), label="certification evidence")
    if evidence_path.parent != release_dir:
        raise CertificationError("Certification evidence is not stored inside the selected release")
    evidence = _load_json(evidence_path)
    if evidence.get("certification_status") != "candidate_complete":
        raise CertificationError("Release evidence is not a completed candidate certificate")
    if evidence.get("release_version") != manifest.get("version"):
        raise CertificationError("Release evidence version does not match the manifest")

    source = evidence.get("source") or {}
    for key in ("source_commit", "upstream_commit", "source_branch"):
        if source.get(key) != manifest.get(key):
            raise CertificationError(f"Release evidence {key} does not match the manifest")
    tag = str(source.get("certified_tag") or "")
    tag_object = str(source.get("tag_object") or "")
    if not tag or not tag_object:
        raise CertificationError("Release evidence is missing the certified source tag")
    if _git_output(source_root, "rev-parse", f"refs/tags/{tag}") != tag_object:
        raise CertificationError(f"Certified tag {tag} object does not match release evidence")
    if _git_output(source_root, "rev-parse", f"refs/tags/{tag}^{{}}") != manifest["source_commit"]:
        raise CertificationError(f"Certified tag {tag} does not resolve to the source commit")

    bundle = source.get("bundle") or {}
    manifest_bundle = manifest.get("source_bundle") or {}
    for key in ("path", "sha256", "size_bytes"):
        if bundle.get(key) != manifest_bundle.get(key):
            raise CertificationError(f"Source bundle {key} differs between manifest and evidence")
    bundle_path = _resolve_inside(HER_ROOT, str(bundle.get("path") or ""), label="source bundle")
    if not bundle_path.is_file():
        raise CertificationError(f"Source bundle is missing: {bundle_path}")
    if bundle_path.stat().st_size != int(bundle.get("size_bytes", -1)):
        raise CertificationError("Source bundle size does not match release evidence")
    if _sha256(bundle_path) != bundle.get("sha256"):
        raise CertificationError("Source bundle SHA-256 does not match release evidence")
    bundle_verify = _run(["git", "bundle", "verify", str(bundle_path)], source_root)
    if bundle_verify.returncode != 0:
        raise CertificationError(f"Source bundle verification failed:\n{bundle_verify.stdout}")
    bundle_heads = _run(
        ["git", "bundle", "list-heads", str(bundle_path), f"refs/tags/{tag}"],
        source_root,
    )
    if bundle_heads.returncode != 0 or not bundle_heads.stdout.startswith(f"{tag_object} "):
        raise CertificationError("Source bundle does not contain the certified tag object")

    artifacts = evidence.get("artifacts") or {}
    if set(artifacts) != set(manifest.get("binaries") or {}):
        raise CertificationError("Release evidence does not cover every packaged platform")
    for platform_key, binary_metadata in manifest["binaries"].items():
        artifact = artifacts[platform_key]
        for key in ("path", "sha256", "rust_target_triple"):
            if artifact.get(key) != binary_metadata.get(key):
                raise CertificationError(f"{platform_key} evidence {key} does not match the manifest")
        binary = _resolve_inside(HER_ROOT, binary_metadata["path"], label=f"{platform_key} binary")
        if not binary.is_file():
            raise CertificationError(f"Packaged binary is missing: {binary}")
        if binary.name != binary_metadata["binary_name"]:
            raise CertificationError(f"{platform_key} binary name does not match the manifest")
        if _sha256(binary) != binary_metadata["sha256"]:
            raise CertificationError(f"{platform_key} binary SHA-256 does not match the manifest")
        binary_bytes = binary.read_bytes()
        expected_magic = b"\x7fELF" if platform_key.startswith("linux") else b"MZ"
        if not binary_bytes.startswith(expected_magic):
            raise CertificationError(f"{platform_key} binary has the wrong executable format")
        for value in (
            manifest["source_commit"],
            manifest["source_branch"],
            binary_metadata["rust_target_triple"],
        ):
            if str(value).encode("utf-8") not in binary_bytes:
                raise CertificationError(f"{platform_key} binary omits embedded provenance {value!r}")
        if artifact.get("provenance_dirty") is not False:
            raise CertificationError(f"{platform_key} evidence does not certify a clean source build")

    linux = _resolve_inside(
        HER_ROOT,
        manifest["binaries"]["linux-x86_64"]["path"],
        label="Linux binary",
    )
    version = _run([str(linux), "version", "--output-format", "json"], PROJECT_ROOT)
    if version.returncode != 0:
        raise CertificationError(f"Linux binary version probe failed:\n{version.stdout}")
    try:
        provenance = json.loads(version.stdout)
    except json.JSONDecodeError as exc:
        raise CertificationError("Linux binary version probe returned invalid JSON") from exc
    if provenance.get("git_sha") != manifest["source_commit"]:
        raise CertificationError("Linux binary embedded source commit does not match the manifest")
    if provenance.get("branch") != manifest["source_branch"] or provenance.get("is_dirty") is not False:
        raise CertificationError("Linux binary provenance is not the declared clean source branch")
    if provenance.get("target") != manifest["binaries"]["linux-x86_64"]["rust_target_triple"]:
        raise CertificationError("Linux binary target does not match the manifest")

    validations = evidence.get("validations") or {}
    required_validations = {
        "rust_workspace",
        "clippy_baseline",
        "hashi_python",
        "her_debug_lab",
        "gateway_scheduler",
        "cross_layer_media",
        "windows_native_smokes",
    }
    if not required_validations.issubset(validations):
        raise CertificationError("Release evidence omits required validation results")
    for name in required_validations:
        row = validations[name]
        if row.get("status") != "passed" or int(row.get("failed", 0)) != 0:
            raise CertificationError(f"Release evidence validation {name} is not passing")
    return evidence


def _verify_workspace_tests(rust_root: Path, baseline: dict) -> None:
    rust_baseline = baseline["rust_workspace"]
    result = _run(list(rust_baseline["command"]), rust_root)
    if result.returncode != 0:
        raise CertificationError(f"Rust workspace tests failed:\n{result.stdout}")


def _verify_hashi_integration(source_root: Path, manifest: dict, baseline: dict) -> None:
    integration = baseline.get("hashi_integration")
    if not isinstance(integration, dict) or not integration.get("command"):
        raise CertificationError("Certification baseline omits the HASHI integration suite")
    linux = _resolve_inside(
        HER_ROOT,
        manifest["binaries"]["linux-x86_64"]["path"],
        label="Linux binary",
    )
    environment = dict(os.environ)
    environment.update(
        {
            "HASHI_HER_STAGED_BINARY": str(linux),
            "HASHI_HER_STAGED_SHA256": str(manifest["binaries"]["linux-x86_64"]["sha256"]),
            "HASHI_HER_SOURCE_ROOT": str(source_root),
        }
    )
    result = _run(list(integration["command"]), PROJECT_ROOT, env=environment)
    if result.returncode != 0:
        raise CertificationError(f"HASHI integration tests failed:\n{result.stdout}")


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
    if len(expected) != len(set(expected)):
        raise CertificationError("Clippy baseline contains duplicate diagnostics")

    result = _run(list(clippy_baseline["command"]), rust_root)
    if not expected:
        if result.returncode != 0:
            raise CertificationError(f"Clippy failed:\n{result.stdout}")
        return
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
        "--release-dir",
        type=Path,
        help="Verify staged release metadata without changing the active manifest.",
    )
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Verify commit and baseline linkage without running Cargo.",
    )
    args = parser.parse_args()

    try:
        manifest, baseline, release_dir = _release_inputs(args.release_dir)
        rust_root = _verify_metadata(args.source_root.resolve(), manifest, baseline)
        evidence = _verify_release_evidence(
            args.source_root.resolve(),
            release_dir,
            manifest,
        )
        if args.release_dir is not None and evidence is None:
            raise CertificationError("Staged release metadata must include certification evidence")
        if not args.metadata_only:
            _verify_workspace_tests(rust_root, baseline)
            _verify_clippy(rust_root, baseline)
            _verify_hashi_integration(args.source_root.resolve(), manifest, baseline)
    except CertificationError as exc:
        print(f"Claw certification FAILED: {exc}", file=sys.stderr)
        return 1

    mode = "metadata" if args.metadata_only else "full"
    target = "staged" if args.release_dir is not None else "active"
    print(
        f"Claw certification OK ({target} {mode}): {baseline['runtime_version']} "
        f"source={baseline['source_commit']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
