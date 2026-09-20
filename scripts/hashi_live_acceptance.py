#!/usr/bin/env python3
"""Evidence-first runner for HASHI and external-frontend live acceptance.

The runner never sends commands to HASHI and never performs lifecycle actions.
It freezes the checklist, captures runtime/source snapshots, records operator
evidence, and enforces the protected-Core and repository invariants.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


CODE_ROOT = Path(__file__).resolve().parents[1]
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from orchestrator.runtime_contract import (  # noqa: E402
    CORE_SOURCE_PATHS,
    core_source_digest,
)


DEFAULT_SUITE = (
    CODE_ROOT / "live_acceptance" / "suites" / "hashi_frontend_essential.json"
)
DEFAULT_OUTPUT_ROOT = CODE_ROOT / "state" / "live-tests"
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
VALID_STATUSES = frozenset({"not_run", "pass", "fail", "blocked", "skip"})


class AcceptanceError(RuntimeError):
    """The suite, run state, or supplied evidence is invalid."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def compact_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AcceptanceError(f"Cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AcceptanceError(f"Invalid JSON in {path}: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def append_event(run_dir: Path, event: str, **details: Any) -> None:
    payload = {"at": utc_now(), "event": event, **details}
    with (run_dir / "events.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def file_fingerprint(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"exists": False}
    stat = path.stat()
    return {
        "exists": True,
        "sha256": sha256_file(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def require_name(value: str, kind: str) -> str:
    if not SAFE_NAME.fullmatch(value):
        raise AcceptanceError(
            f"Invalid {kind} {value!r}; use 1-80 letters, digits, '.', '_' or '-'"
        )
    return value


def suite_items(suite: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in suite["items"]}


def validate_suite(suite: Any) -> dict[str, Any]:
    errors: list[str] = []
    if not isinstance(suite, dict):
        raise AcceptanceError("Suite root must be a JSON object")
    if suite.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    suite_id = suite.get("suite_id")
    if not isinstance(suite_id, str) or not SAFE_NAME.fullmatch(suite_id):
        errors.append("suite_id is missing or invalid")
    policy = suite.get("policy")
    if not isinstance(policy, dict):
        errors.append("policy must be an object")
        policy = {}
    if policy.get("automatic_lifecycle_actions") is not False:
        errors.append("automatic_lifecycle_actions must be false")
    evidence_types_raw = suite.get("evidence_types")
    if not isinstance(evidence_types_raw, list) or not evidence_types_raw:
        errors.append("evidence_types must be a non-empty list")
        evidence_types: set[str] = set()
    else:
        evidence_types = {
            value for value in evidence_types_raw if isinstance(value, str)
        }
        if len(evidence_types) != len(evidence_types_raw):
            errors.append("evidence_types must contain unique strings")
    items = suite.get("items")
    if not isinstance(items, list) or not items:
        errors.append("items must be a non-empty list")
        items = []
    ids: list[str] = []
    for index, item in enumerate(items):
        prefix = f"items[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not SAFE_NAME.fullmatch(item_id):
            errors.append(f"{prefix}.id is missing or invalid")
            continue
        ids.append(item_id)
        if not isinstance(item.get("title"), str) or not item["title"].strip():
            errors.append(f"{prefix}.title is required")
        if not isinstance(item.get("required"), bool):
            errors.append(f"{prefix}.required must be boolean")
        for field in ("operator_steps", "pass_criteria", "depends_on"):
            value = item.get(field)
            if not isinstance(value, list) or any(
                not isinstance(entry, str) or not entry.strip() for entry in value
            ):
                errors.append(f"{prefix}.{field} must be a string list")
        minimum = item.get("evidence_minimum")
        if not isinstance(minimum, dict) or not minimum:
            errors.append(f"{prefix}.evidence_minimum must be a non-empty object")
        else:
            for evidence_type, count in minimum.items():
                if evidence_type not in evidence_types:
                    errors.append(
                        f"{prefix}.evidence_minimum uses unknown type {evidence_type!r}"
                    )
                if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                    errors.append(
                        f"{prefix}.evidence_minimum[{evidence_type!r}] must be >= 1"
                    )
    if len(ids) != len(set(ids)):
        errors.append("item ids must be unique")
    known = set(ids)
    graph: dict[str, list[str]] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("id") not in known:
            continue
        dependencies = item.get("depends_on", [])
        graph[item["id"]] = list(dependencies) if isinstance(dependencies, list) else []
        for dependency in graph[item["id"]]:
            if dependency not in known:
                errors.append(f"{item['id']} depends on unknown item {dependency!r}")
            if dependency == item["id"]:
                errors.append(f"{item['id']} cannot depend on itself")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(item_id: str) -> None:
        if item_id in visiting:
            errors.append(f"dependency cycle includes {item_id}")
            return
        if item_id in visited:
            return
        visiting.add(item_id)
        for dependency in graph.get(item_id, []):
            if dependency in graph:
                visit(dependency)
        visiting.remove(item_id)
        visited.add(item_id)

    for item_id in graph:
        visit(item_id)
    if errors:
        raise AcceptanceError("Invalid live acceptance suite:\n- " + "\n- ".join(errors))
    return suite


def load_suite(path: Path) -> dict[str, Any]:
    return validate_suite(read_json(path.resolve()))


def run_git(code_root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(code_root), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        error = (result.stderr or result.stdout).strip()
        raise AcceptanceError(f"git {' '.join(arguments)} failed: {error}")
    return result.stdout.rstrip("\r\n")


def read_pid(path: Path) -> int | None:
    try:
        value = int(path.read_text(encoding="utf-8-sig").strip())
    except (OSError, ValueError):
        return None
    return value if value > 0 else None


def pid_is_alive(pid: int | None) -> bool | None:
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def capture_snapshot(code_root: Path, bridge_home: Path, label: str) -> dict[str, Any]:
    code_root = code_root.resolve()
    bridge_home = bridge_home.resolve()
    protected_files = {
        relative: file_fingerprint(code_root / relative)
        for relative in CORE_SOURCE_PATHS
    }
    runtime_dir = bridge_home / "state" / "instance"
    core_pid = read_pid(runtime_dir / "process.pid")
    state_files = {
        filename: file_fingerprint(runtime_dir / filename)
        for filename in (
            "function-topology.json",
            "kernel.json",
            "reboot-receipts.json",
            "terminal.json",
        )
    }
    return {
        "schema_version": 1,
        "captured_at": utc_now(),
        "label": label,
        "source": {
            "git_head": run_git(code_root, "rev-parse", "HEAD"),
            "git_branch": run_git(code_root, "branch", "--show-current"),
            "git_status": run_git(
                code_root, "status", "--porcelain=v1", "--untracked-files=all"
            ).splitlines(),
            "core_source_digest": core_source_digest(code_root),
            "protected_files": protected_files,
        },
        "runtime": {
            "core_pid": core_pid,
            "core_pid_alive": pid_is_alive(core_pid),
            "state_files": state_files,
        },
    }


def snapshot_path(run_dir: Path, label: str) -> Path:
    return run_dir / "snapshots" / f"{require_name(label, 'snapshot label')}.json"


def load_run(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    run_dir = run_dir.resolve()
    run = read_json(run_dir / "run.json")
    suite = load_suite(run_dir / "suite.json")
    if not isinstance(run, dict) or run.get("schema_version") != 1:
        raise AcceptanceError(f"Invalid run state in {run_dir}")
    if run.get("suite_id") != suite.get("suite_id"):
        raise AcceptanceError("Run state and frozen suite do not match")
    return run, suite


def save_run(run_dir: Path, run: dict[str, Any]) -> None:
    run["updated_at"] = utc_now()
    write_json(run_dir / "run.json", run)


def capture_run_snapshot(run_dir: Path, label: str) -> Path:
    run, _suite = load_run(run_dir)
    path = snapshot_path(run_dir, label)
    if path.exists():
        raise AcceptanceError(f"Snapshot {label!r} already exists")
    snapshot = capture_snapshot(
        Path(run["context"]["code_root"]),
        Path(run["context"]["bridge_home"]),
        label,
    )
    write_json(path, snapshot)
    run.setdefault("snapshots", []).append(label)
    save_run(run_dir, run)
    append_event(run_dir, "snapshot_captured", label=label, path=str(path))
    return path


def expectation_result(before: Any, after: Any, expected: str) -> dict[str, Any]:
    if expected == "same":
        passed = before == after
    elif expected == "changed":
        passed = before != after
    elif expected == "any":
        passed = True
    else:
        raise AcceptanceError(f"Unknown expectation {expected!r}")
    return {
        "expected": expected,
        "before": before,
        "after": after,
        "passed": passed,
    }


def compare_snapshots(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    core_pid: str,
    core_digest: str,
    git_head: str,
    git_status: str,
) -> dict[str, Any]:
    checks = {
        "core_pid": expectation_result(
            before["runtime"].get("core_pid"),
            after["runtime"].get("core_pid"),
            core_pid,
        ),
        "core_source_digest": expectation_result(
            before["source"].get("core_source_digest"),
            after["source"].get("core_source_digest"),
            core_digest,
        ),
        "git_head": expectation_result(
            before["source"].get("git_head"),
            after["source"].get("git_head"),
            git_head,
        ),
        "git_status": expectation_result(
            before["source"].get("git_status"),
            after["source"].get("git_status"),
            git_status,
        ),
    }
    protected_before = before["source"].get("protected_files")
    protected_after = after["source"].get("protected_files")
    checks["protected_core_manifest"] = expectation_result(
        protected_before, protected_after, core_digest
    )
    return {
        "schema_version": 1,
        "created_at": utc_now(),
        "before": before.get("label"),
        "after": after.get("label"),
        "passed": all(check["passed"] for check in checks.values()),
        "checks": checks,
    }


def evidence_counts(item_state: dict[str, Any]) -> Counter[str]:
    return Counter(
        evidence.get("type")
        for evidence in item_state.get("evidence", [])
        if isinstance(evidence, dict) and isinstance(evidence.get("type"), str)
    )


def evidence_shortfalls(
    item_definition: dict[str, Any], item_state: dict[str, Any]
) -> dict[str, int]:
    counts = evidence_counts(item_state)
    return {
        evidence_type: required - counts[evidence_type]
        for evidence_type, required in item_definition["evidence_minimum"].items()
        if counts[evidence_type] < required
    }


def relative_evidence_path(run_dir: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(run_dir.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def attach_evidence_file(
    run_dir: Path,
    run: dict[str, Any],
    suite: dict[str, Any],
    item_id: str,
    evidence_type: str,
    source: Path,
    *,
    copy: bool = True,
) -> dict[str, Any]:
    if evidence_type not in suite["evidence_types"] or evidence_type == "observation":
        raise AcceptanceError(f"Invalid file evidence type {evidence_type!r}")
    source = source.resolve()
    if not source.is_file():
        raise AcceptanceError(f"Evidence file does not exist: {source}")
    if copy:
        target_dir = run_dir / "evidence" / item_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / (
            f"{compact_timestamp()}-{uuid.uuid4().hex[:6]}-"
            f"{evidence_type}-{source.name}"
        )
        shutil.copy2(source, target)
    else:
        target = source
    evidence = {
        "type": evidence_type,
        "captured_at": utc_now(),
        "path": relative_evidence_path(run_dir, target),
        "sha256": sha256_file(target),
        "size": target.stat().st_size,
    }
    run["items"][item_id]["evidence"].append(evidence)
    return evidence


def attach_observation(
    run: dict[str, Any], item_id: str, text: str
) -> dict[str, Any]:
    normalized = text.strip()
    if not normalized:
        raise AcceptanceError("Observation cannot be empty")
    evidence = {
        "type": "observation",
        "captured_at": utc_now(),
        "text": normalized,
    }
    run["items"][item_id]["evidence"].append(evidence)
    return evidence


def parse_file_evidence(values: list[str]) -> list[tuple[str, Path]]:
    parsed: list[tuple[str, Path]] = []
    for value in values:
        evidence_type, separator, raw_path = value.partition("=")
        if not separator or not evidence_type or not raw_path:
            raise AcceptanceError(
                f"Invalid --file {value!r}; expected TYPE=PATH"
            )
        parsed.append((evidence_type.strip(), Path(raw_path.strip())))
    return parsed


def require_passing_comparisons(
    run_dir: Path,
    item_state: dict[str, Any],
    prospective_files: list[tuple[str, Path]],
) -> None:
    comparison_paths: list[Path] = []
    for evidence in item_state.get("evidence", []):
        if not isinstance(evidence, dict) or evidence.get("type") != "comparison":
            continue
        path = Path(str(evidence.get("path") or ""))
        comparison_paths.append(path if path.is_absolute() else run_dir / path)
    comparison_paths.extend(
        path.resolve()
        for evidence_type, path in prospective_files
        if evidence_type == "comparison"
    )
    for path in comparison_paths:
        comparison = read_json(path)
        if not isinstance(comparison, dict) or comparison.get("passed") is not True:
            raise AcceptanceError(
                f"Cannot mark pass; comparison did not pass: {path}"
            )


def start_run(args: argparse.Namespace, suite: dict[str, Any]) -> Path:
    code_root = Path(args.code_root).resolve()
    bridge_home = Path(args.bridge_home or code_root).resolve()
    output_root = Path(args.output_root).resolve()
    instance_id = require_name(args.instance_id, "instance id")
    run_id = f"{compact_timestamp()}-{instance_id}-{uuid.uuid4().hex[:8]}"
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    write_json(run_dir / "suite.json", suite)
    items = {
        item["id"]: {
            "status": "not_run",
            "evidence": [],
            "notes": [],
            "updated_at": None,
        }
        for item in suite["items"]
    }
    run = {
        "schema_version": 1,
        "run_id": run_id,
        "suite_id": suite["suite_id"],
        "status": "running",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "context": {
            "instance_id": instance_id,
            "agent": args.agent,
            "client_label": args.client_label,
            "client_url": args.client_url or None,
            "code_root": str(code_root),
            "bridge_home": str(bridge_home),
        },
        "snapshots": [],
        "items": items,
    }
    write_json(run_dir / "run.json", run)
    append_event(
        run_dir,
        "run_started",
        run_id=run_id,
        instance_id=instance_id,
        agent=args.agent,
        client_label=args.client_label,
    )
    capture_run_snapshot(run_dir, "baseline")
    return run_dir


def next_item(suite: dict[str, Any], run: dict[str, Any]) -> dict[str, Any] | None:
    for item in suite["items"]:
        if item["required"] and run["items"][item["id"]]["status"] != "pass":
            return item
    return None


def print_status(run_dir: Path) -> None:
    run, suite = load_run(run_dir)
    required = [item for item in suite["items"] if item["required"]]
    passed = sum(run["items"][item["id"]]["status"] == "pass" for item in required)
    failed = [
        item["id"]
        for item in required
        if run["items"][item["id"]]["status"] in {"fail", "blocked"}
    ]
    print(f"Run: {run['run_id']}")
    print(f"Status: {run['status']}")
    print(f"Progress: {passed}/{len(required)} ({passed * 100 // len(required)}%)")
    if failed:
        print("Failed/blocked: " + ", ".join(failed))
    pending = next_item(suite, run)
    if pending:
        print(f"Next: {pending['id']} — {pending['title']}")
        shortfalls = evidence_shortfalls(pending, run["items"][pending["id"]])
        if shortfalls:
            print(
                "Evidence still needed: "
                + ", ".join(f"{key} x{value}" for key, value in shortfalls.items())
            )
    else:
        print("Next: finish")


def render_guide(item: dict[str, Any]) -> None:
    print(f"{item['id']} — {item['title']}")
    print("Steps:")
    for index, step in enumerate(item["operator_steps"], start=1):
        print(f"  {index}. {step}")
    print("Pass criteria:")
    for criterion in item["pass_criteria"]:
        print(f"  - {criterion}")
    print(
        "Evidence: "
        + ", ".join(
            f"{key} x{value}" for key, value in item["evidence_minimum"].items()
        )
    )


def record_item(args: argparse.Namespace) -> None:
    run_dir = Path(args.run).resolve()
    run, suite = load_run(run_dir)
    items = suite_items(suite)
    if args.item not in items:
        raise AcceptanceError(f"Unknown item {args.item!r}")
    if args.status not in VALID_STATUSES - {"not_run"}:
        raise AcceptanceError(f"Invalid status {args.status!r}")
    definition = items[args.item]
    state = run["items"][args.item]
    for dependency in definition["depends_on"]:
        if args.status == "pass" and run["items"][dependency]["status"] != "pass":
            raise AcceptanceError(
                f"Cannot pass {args.item}; dependency {dependency} has not passed"
            )
    file_evidence = parse_file_evidence(args.file)
    for evidence_type, path in file_evidence:
        if evidence_type not in suite["evidence_types"] or evidence_type == "observation":
            raise AcceptanceError(f"Invalid file evidence type {evidence_type!r}")
        if not path.resolve().is_file():
            raise AcceptanceError(f"Evidence file does not exist: {path.resolve()}")
    observations = [observation.strip() for observation in args.observation]
    if any(not observation for observation in observations):
        raise AcceptanceError("Observation cannot be empty")
    if args.status == "pass":
        prospective = evidence_counts(state)
        prospective.update(evidence_type for evidence_type, _path in file_evidence)
        prospective["observation"] += len(observations)
        missing = {
            evidence_type: required - prospective[evidence_type]
            for evidence_type, required in definition["evidence_minimum"].items()
            if prospective[evidence_type] < required
        }
        if missing:
            raise AcceptanceError(
                "Cannot mark pass; evidence still needed: "
                + ", ".join(f"{key} x{value}" for key, value in missing.items())
            )
        require_passing_comparisons(run_dir, state, file_evidence)
    added: list[dict[str, Any]] = []
    for evidence_type, path in file_evidence:
        added.append(
            attach_evidence_file(
                run_dir, run, suite, args.item, evidence_type, path
            )
        )
    for observation in observations:
        added.append(attach_observation(run, args.item, observation))
    if args.note:
        state["notes"].append({"at": utc_now(), "text": args.note.strip()})
    state["status"] = args.status
    state["updated_at"] = utc_now()
    save_run(run_dir, run)
    append_event(
        run_dir,
        "item_recorded",
        item=args.item,
        status=args.status,
        evidence_added=len(added),
    )
    print_status(run_dir)


def compare_run_snapshots(args: argparse.Namespace) -> tuple[Path, bool]:
    run_dir = Path(args.run).resolve()
    run, suite = load_run(run_dir)
    if args.item and args.item not in suite_items(suite):
        raise AcceptanceError(f"Unknown item {args.item!r}")
    before = read_json(snapshot_path(run_dir, args.before))
    after = read_json(snapshot_path(run_dir, args.after))
    comparison = compare_snapshots(
        before,
        after,
        core_pid=args.expect_core_pid,
        core_digest=args.expect_core_digest,
        git_head=args.expect_git_head,
        git_status=args.expect_git_status,
    )
    comparisons_dir = run_dir / "comparisons"
    path = comparisons_dir / f"{args.before}--{args.after}.json"
    if path.exists():
        raise AcceptanceError(f"Comparison already exists: {path}")
    write_json(path, comparison)
    if args.item:
        attach_evidence_file(
            run_dir,
            run,
            suite,
            args.item,
            "comparison",
            path,
            copy=False,
        )
        save_run(run_dir, run)
    append_event(
        run_dir,
        "snapshots_compared",
        before=args.before,
        after=args.after,
        passed=comparison["passed"],
        item=args.item,
    )
    print(f"Comparison: {path}")
    for name, result in comparison["checks"].items():
        print(f"  {'PASS' if result['passed'] else 'FAIL'} {name}: {result['expected']}")
    return path, bool(comparison["passed"])


def report_markdown(
    run: dict[str, Any], suite: dict[str, Any], invariant: dict[str, Any]
) -> str:
    required = [item for item in suite["items"] if item["required"]]
    passed = sum(run["items"][item["id"]]["status"] == "pass" for item in required)
    lines = [
        f"# {suite['title']}",
        "",
        f"- Run: `{run['run_id']}`",
        f"- Result: **{run['status'].upper()}**",
        f"- Instance: `{run['context']['instance_id']}`",
        f"- Agent: `{run['context']['agent']}`",
        f"- Client: `{run['context']['client_label']}`",
        f"- Progress: {passed}/{len(required)}",
        f"- Core/source invariant: {'PASS' if invariant['passed'] else 'FAIL'}",
        "",
        "## Items",
        "",
        "| Item | Status | Evidence |",
        "|---|---:|---:|",
    ]
    for item in suite["items"]:
        state = run["items"][item["id"]]
        lines.append(
            f"| {item['title']} | {state['status']} | {len(state['evidence'])} |"
        )
    lines.extend(["", "## Invariant checks", ""])
    for name, result in invariant["checks"].items():
        lines.append(
            f"- {'PASS' if result['passed'] else 'FAIL'} `{name}` "
            f"(expected {result['expected']})"
        )
    lines.append("")
    return "\n".join(lines)


def finish_run(run_dir: Path) -> bool:
    run_dir = run_dir.resolve()
    run, suite = load_run(run_dir)
    if "final" in run.get("snapshots", []):
        raise AcceptanceError("Run already has a final snapshot")
    capture_run_snapshot(run_dir, "final")
    run, suite = load_run(run_dir)
    baseline = read_json(snapshot_path(run_dir, "baseline"))
    final = read_json(snapshot_path(run_dir, "final"))
    policy = suite["policy"]
    invariant = compare_snapshots(
        baseline,
        final,
        core_pid="any",
        core_digest=(
            "same" if policy.get("core_source_must_remain_unchanged") else "any"
        ),
        git_head="same" if policy.get("git_head_must_remain_unchanged") else "any",
        git_status=(
            "same" if policy.get("working_tree_must_remain_unchanged") else "any"
        ),
    )
    invariant_path = run_dir / "comparisons" / "baseline--final.json"
    write_json(invariant_path, invariant)
    missing = [
        item["id"]
        for item in suite["items"]
        if item["required"] and run["items"][item["id"]]["status"] != "pass"
    ]
    passed = not missing and invariant["passed"]
    run["status"] = "passed" if passed else "failed"
    run["finished_at"] = utc_now()
    run["final_invariant"] = relative_evidence_path(run_dir, invariant_path)
    if missing:
        run["failure_summary"] = {"required_items_not_passed": missing}
    save_run(run_dir, run)
    (run_dir / "report.md").write_text(
        report_markdown(run, suite, invariant), encoding="utf-8", newline="\n"
    )
    append_event(
        run_dir,
        "run_finished",
        status=run["status"],
        invariant_passed=invariant["passed"],
        missing=missing,
    )
    print(f"Result: {run['status'].upper()}")
    print(f"Report: {run_dir / 'report.md'}")
    if missing:
        print("Required items not passed: " + ", ".join(missing))
    return passed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run evidence-first HASHI/external-frontend live acceptance"
    )
    parser.add_argument(
        "--suite", default=str(DEFAULT_SUITE), help="suite manifest JSON"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("validate", help="validate the suite manifest")

    guide = commands.add_parser("guide", help="show one item's live steps")
    guide.add_argument("--item", required=True)

    start = commands.add_parser("start", help="start a run and capture baseline")
    start.add_argument("--instance-id", required=True)
    start.add_argument("--agent", required=True)
    start.add_argument("--client-label", default="External frontend")
    start.add_argument("--client-url")
    start.add_argument("--code-root", default=str(CODE_ROOT))
    start.add_argument("--bridge-home")
    start.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))

    snapshot = commands.add_parser("snapshot", help="capture a named snapshot")
    snapshot.add_argument("--run", required=True)
    snapshot.add_argument("--label", required=True)

    compare = commands.add_parser("compare", help="compare two snapshots")
    compare.add_argument("--run", required=True)
    compare.add_argument("--before", required=True)
    compare.add_argument("--after", required=True)
    compare.add_argument("--item")
    compare.add_argument(
        "--expect-core-pid", choices=("same", "changed", "any"), required=True
    )
    compare.add_argument(
        "--expect-core-digest", choices=("same", "changed", "any"), default="same"
    )
    compare.add_argument(
        "--expect-git-head", choices=("same", "changed", "any"), default="same"
    )
    compare.add_argument(
        "--expect-git-status", choices=("same", "changed", "any"), default="same"
    )

    record = commands.add_parser("record", help="record an item's result/evidence")
    record.add_argument("--run", required=True)
    record.add_argument("--item", required=True)
    record.add_argument(
        "--status", choices=("pass", "fail", "blocked", "skip"), required=True
    )
    record.add_argument(
        "--file", action="append", default=[], metavar="TYPE=PATH"
    )
    record.add_argument("--observation", action="append", default=[])
    record.add_argument("--note")

    status = commands.add_parser("status", help="show progress and next item")
    status.add_argument("--run", required=True)

    finish = commands.add_parser("finish", help="finalize and enforce invariants")
    finish.add_argument("--run", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command in {"validate", "guide", "start"}:
            suite = load_suite(Path(args.suite))
        if args.command == "validate":
            print(
                f"Suite valid: {suite['suite_id']} ({len(suite['items'])} items)"
            )
        elif args.command == "guide":
            item = suite_items(suite).get(args.item)
            if item is None:
                raise AcceptanceError(f"Unknown item {args.item!r}")
            render_guide(item)
        elif args.command == "start":
            run_dir = start_run(args, suite)
            print(f"Run created: {run_dir}")
            print_status(run_dir)
        elif args.command == "snapshot":
            path = capture_run_snapshot(Path(args.run), args.label)
            print(f"Snapshot: {path}")
        elif args.command == "compare":
            _path, passed = compare_run_snapshots(args)
            return 0 if passed else 1
        elif args.command == "record":
            record_item(args)
        elif args.command == "status":
            print_status(Path(args.run))
        elif args.command == "finish":
            return 0 if finish_run(Path(args.run)) else 1
        return 0
    except AcceptanceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
