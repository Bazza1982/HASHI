#!/usr/bin/env python3
"""Fail when protected HASHI core files are changed without explicit approval."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = "orchestrator/runtime_contract.py"
PROJECT_METADATA_PATH = "pyproject.toml"
CORE_REVIEW_DIRECTORY = "docs/core-reviews"
_PROJECT_SECTION = re.compile(
    r"(?ms)^\[project\]\s*$\n(?P<body>.*?)(?=^\[|\Z)"
)
_PROJECT_VERSION = re.compile(r'(?m)^\s*version\s*=\s*"([^"]+)"\s*$')
_RELEASE_PREFIX = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:[A-Za-z0-9.+-]*)$"
)


def _parse_manifest(source: str) -> tuple[str, ...]:
    # Read data, never execute code from the branch being checked.
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "CORE_SOURCE_PATHS"
            for target in node.targets
        ):
            paths = ast.literal_eval(node.value)
            if isinstance(paths, (tuple, list)) and all(isinstance(p, str) for p in paths):
                return tuple(paths)
    raise ValueError("CORE_SOURCE_PATHS must be a literal sequence of paths")


PROTECTED_CORE_PATHS = _parse_manifest(
    (REPOSITORY_ROOT / MANIFEST_PATH).read_text(encoding="utf-8")
)


def _repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip())


def _changed_files(args: argparse.Namespace) -> set[str]:
    cmd = ["git", "diff", "--name-only", "--no-renames"]
    if args.cached:
        cmd.append("--cached")
    if args.base:
        cmd.extend([args.base, "--"])
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    changed = {
        line.strip().replace("\\", "/")
        for line in result.stdout.splitlines()
        if line.strip()
    }
    if not args.cached and not args.base:
        staged = subprocess.run(
            ["git", "diff", "--name-only", "--no-renames", "--cached"],
            check=True, capture_output=True, text=True,
        )
        changed.update(line.strip() for line in staged.stdout.splitlines() if line.strip())
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            check=True,
            capture_output=True,
            text=True,
        )
        changed.update(
            line.strip().replace("\\", "/")
            for line in untracked.stdout.splitlines()
            if line.strip()
        )
    return changed


def _protected_paths(root: Path, args: argparse.Namespace) -> set[str]:
    current = root / MANIFEST_PATH
    protected = set(
        _parse_manifest(current.read_text(encoding="utf-8"))
        if current.is_file() else PROTECTED_CORE_PATHS
    )
    # A candidate cannot erase protection by editing its own manifest. Keep
    # HEAD, the branch baseline and (for commits) the exact index view too.
    refs = ["HEAD"]
    if args.base:
        refs.append(args.base)
    if args.cached:
        refs.append("")
    for ref in dict.fromkeys(refs):
        result = subprocess.run(
            ["git", "show", f"{ref}:{MANIFEST_PATH}"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            protected.update(_parse_manifest(result.stdout))
    return protected


def _is_authorized(args: argparse.Namespace) -> bool:
    return args.authorized or os.environ.get("HASHI_CORE_EDIT_AUTHORIZED") == "1"


def _missing_manifest_paths(root: Path) -> list[str]:
    return sorted(path for path in PROTECTED_CORE_PATHS if not (root / path).is_file())


def _git_text(spec: str) -> str:
    result = subprocess.run(
        ["git", "show", spec],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "path is unavailable"
        raise ValueError(f"cannot read {spec}: {detail}")
    return result.stdout


def _candidate_text(root: Path, path: str, args: argparse.Namespace) -> str:
    if args.cached:
        return _git_text(f":{path}")
    try:
        return (root / path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read candidate {path}: {exc}") from exc


def _project_release(source: str) -> tuple[str, tuple[int, int, int]]:
    section = _PROJECT_SECTION.search(source)
    if section is None:
        raise ValueError("pyproject.toml does not define [project]")
    match = _PROJECT_VERSION.search(section.group("body"))
    if match is None:
        raise ValueError("[project] does not define a literal version")
    version = match.group(1)
    release = _RELEASE_PREFIX.fullmatch(version)
    if release is None:
        raise ValueError(f"unsupported project version: {version}")
    return version, tuple(int(release.group(index)) for index in range(1, 4))


def _core_review_digest(
    root: Path,
    protected_paths: set[str],
    args: argparse.Namespace,
) -> str:
    digest = hashlib.sha256()
    for path in sorted(protected_paths):
        try:
            source = _candidate_text(root, path, args).encode("utf-8")
        except ValueError:
            source = b"<missing>"
        encoded_path = path.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(4, "big"))
        digest.update(encoded_path)
        digest.update(len(source).to_bytes(8, "big"))
        digest.update(source)
    return f"sha256:{digest.hexdigest()}"


def _added_review_paths(args: argparse.Namespace) -> list[str]:
    if not args.base:
        return []
    cmd = ["git", "diff", "--name-only", "--diff-filter=A", "--no-renames"]
    if args.cached:
        cmd.append("--cached")
    cmd.extend([args.base, "--", CORE_REVIEW_DIRECTORY])
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return sorted(
        path.strip().replace("\\", "/")
        for path in result.stdout.splitlines()
        if path.strip().startswith(f"{CORE_REVIEW_DIRECTORY}/")
        and path.strip().endswith(".json")
    )


def _review_record_errors(
    source: str,
    *,
    product_version: str,
    core_digest: str,
) -> list[str]:
    try:
        record = json.loads(source)
    except json.JSONDecodeError as exc:
        return [f"invalid JSON: {exc}"]
    if not isinstance(record, dict):
        return ["record must be a JSON object"]

    errors: list[str] = []
    if record.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    required_text = (
        "change_id",
        "authorization_reference",
        "implementer",
        "reviewer",
        "reviewed_at",
        "summary",
    )
    for field in required_text:
        if not isinstance(record.get(field), str) or not record[field].strip():
            errors.append(f"{field} must be a non-empty string")
    if str(record.get("verdict") or "").casefold() != "approved":
        errors.append("verdict must be approved")
    if record.get("product_version") != product_version:
        errors.append(f"product_version must be {product_version}")
    if record.get("core_digest") != core_digest:
        errors.append("core_digest does not match the candidate protected Core")

    implementer = str(record.get("implementer") or "").strip().casefold()
    reviewer = str(record.get("reviewer") or "").strip().casefold()
    if implementer and reviewer and implementer == reviewer:
        errors.append("reviewer must be independent from implementer")

    reviewed_at = str(record.get("reviewed_at") or "").strip()
    if reviewed_at:
        try:
            parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
        except ValueError:
            errors.append("reviewed_at must be an ISO-8601 timestamp")
        else:
            if parsed.tzinfo is None:
                errors.append("reviewed_at must include a timezone")
    return errors


def _major_version_policy_errors(
    root: Path,
    protected_paths: set[str],
    args: argparse.Namespace,
) -> list[str]:
    if not args.base:
        return ["--base is required for an authorized Core major-version change"]

    errors: list[str] = []
    try:
        base_version, base_release = _project_release(
            _git_text(f"{args.base}:{PROJECT_METADATA_PATH}")
        )
        product_version, candidate_release = _project_release(
            _candidate_text(root, PROJECT_METADATA_PATH, args)
        )
    except ValueError as exc:
        return [str(exc)]

    if candidate_release[0] <= base_release[0]:
        errors.append(
            "protected Core requires a product major-version increment "
            f"({base_version} -> {product_version})"
        )
    if candidate_release[1:] != (0, 0):
        errors.append(
            f"Core major release must reset minor and patch to zero: {product_version}"
        )

    core_digest = _core_review_digest(root, protected_paths, args)
    try:
        review_paths = _added_review_paths(args)
    except subprocess.CalledProcessError as exc:
        errors.append(f"cannot inspect Core review records: {exc}")
        return errors
    if not review_paths:
        errors.append(
            f"add an independent review record under {CORE_REVIEW_DIRECTORY}/"
        )
        return errors

    invalid_records: list[str] = []
    for path in review_paths:
        try:
            record_errors = _review_record_errors(
                _candidate_text(root, path, args),
                product_version=product_version,
                core_digest=core_digest,
            )
        except ValueError as exc:
            record_errors = [str(exc)]
        if not record_errors:
            return errors
        invalid_records.append(f"{path}: {'; '.join(record_errors)}")
    errors.append("no added Core review record matches the candidate")
    errors.extend(invalid_records)
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cached", action="store_true", help="check staged changes")
    parser.add_argument("--base", help="optional git base/ref to diff against")
    parser.add_argument(
        "--authorized",
        action="store_true",
        help="acknowledge explicit user authorization for a protected Core edit",
    )
    parser.add_argument(
        "--major-version-change",
        action="store_true",
        help="enforce the authorized Core major-release and review-record policy",
    )
    parser.add_argument(
        "--print-core-digest",
        action="store_true",
        help="print the candidate protected-Core digest for an independent review",
    )
    parser.add_argument(
        "--validate-manifest",
        action="store_true",
        help="fail if a protected path does not exist",
    )
    args = parser.parse_args(argv)

    root = _repo_root()
    os.chdir(root)
    if args.validate_manifest:
        missing = _missing_manifest_paths(root)
        if missing:
            print("protected core manifest: invalid", file=sys.stderr)
            for path in missing:
                print(f"- missing: {path}", file=sys.stderr)
            return 3
        print("protected core manifest: ok")
    if args.print_core_digest:
        try:
            print(_core_review_digest(root, _protected_paths(root, args), args))
        except (ValueError, SyntaxError, OSError) as exc:
            print(f"protected core digest: unavailable: {exc}", file=sys.stderr)
            return 3
        return 0
    changed = _changed_files(args)
    try:
        protected = sorted(changed & _protected_paths(root, args))
    except (ValueError, SyntaxError, OSError) as exc:
        print(f"protected core manifest: invalid: {exc}", file=sys.stderr)
        return 3

    if not protected:
        print("protected core check: ok")
        return 0

    if not _is_authorized(args):
        print("protected core check: blocked", file=sys.stderr)
        print("Protected HASHI Core files changed without explicit authorization:", file=sys.stderr)
        for path in protected:
            print(f"- {path}", file=sys.stderr)
        print(
            "\nOnly a user-authorized Core major-version migration may proceed. "
            "Ordinary fixes, refactors and reboot work do not authorize Core edits.",
            file=sys.stderr,
        )
        return 2

    if not args.major_version_change:
        print("protected core release policy: blocked", file=sys.stderr)
        print(
            "Explicit Core authorization alone is insufficient. The task must authorize "
            "a major-version migration, checked with --major-version-change and --base.",
            file=sys.stderr,
        )
        return 4

    try:
        review_scope = _protected_paths(root, args)
    except (ValueError, SyntaxError, OSError) as exc:
        print(f"protected core manifest: invalid: {exc}", file=sys.stderr)
        return 3
    errors = _major_version_policy_errors(root, review_scope, args)
    if errors:
        print("protected core release policy: blocked", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 4

    print("protected core check: authorized major-version change")
    for path in protected:
        print(f"- {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
