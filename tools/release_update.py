"""Source preparation and closed-loop adoption for a registered HASHI instance.

Staging qualifies source without activation. A full release additionally verifies
Core, Functions, every Worker, and the independently supervised Remote process.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence

from remote.security.shared_token import load_shared_token
from tools.remote_rescue import probe_capabilities, rescue_restart


_HEX_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_MAX_HTTP_BODY = 1024 * 1024


class ReleaseUpdateError(RuntimeError):
    """A release phase failed without claiming a completed deployment."""

    def __init__(self, phase: str, message: str, *, details: Any = None):
        super().__init__(message)
        self.phase = str(phase)
        self.details = details

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "phase": self.phase,
            "error": str(self),
        }
        if self.details not in (None, "", [], {}):
            payload["details"] = self.details
        return payload


_MAX_FAILURE_PATHS = 10

_PHASE_FAILURE_CODES = {
    "source_update": "instance_source",
    "source_preflight": "instance_source",
    "source_fetch": "fetch_failed",
    "source_fast_forward": "not_fast_forward",
    "source_verify": "commit_mismatch",
    "source_force_stash": "stash_failed",
    "source_force_backup": "backup_failed",
    "source_force_checkout": "checkout_failed",
    "protected_core_gate": "protected_gate",
    "stage_runtime": "runtime_unavailable",
    "stage_qualification": "qualification_failed",
    "stage_verify": "commit_mismatch",
    "stage_receipt": "receipt_failed",
    "stage_io": "state_io",
}


def _release_failure(
    code: str, *, source_changed: bool | None, stage: str, **values: Any
) -> dict[str, Any]:
    """Build the structured failure that user-facing surfaces render.

    ``stage`` is ``pre_change`` before the checkout has moved, ``post_change``
    once it has, and ``unknown`` when the failure position cannot be proven.
    """

    failure: dict[str, Any] = {
        "code": str(code),
        "source_changed": source_changed,
        "stage": str(stage),
    }
    for key, value in values.items():
        if value not in (None, "", [], {}):
            failure[key] = value
    return failure


def _dirty_summary(status: str) -> dict[str, Any]:
    modified: list[str] = []
    untracked: list[str] = []
    for line in str(status or "").splitlines():
        if not line.strip():
            continue
        if len(line) >= 3 and line[2] == " ":
            path = line[3:].strip()
        elif len(line) >= 2 and line[1] == " ":
            path = line[2:].strip()
        else:
            path = line.strip()
        if not path:
            continue
        path = path.replace("\\", "/")
        if line.startswith("??"):
            untracked.append(path)
        else:
            modified.append(path)
    paths = modified + untracked
    return {
        "dirty_count": len(paths),
        "modified_count": len(modified),
        "untracked_count": len(untracked),
        "modified_paths": modified[:_MAX_FAILURE_PATHS],
        "untracked_paths": untracked[:_MAX_FAILURE_PATHS],
        "dirty_paths": paths[:_MAX_FAILURE_PATHS],
        "dirty_truncated": len(paths) > _MAX_FAILURE_PATHS,
    }


def release_failure(exc: ReleaseUpdateError) -> dict[str, Any]:
    """Return the structured failure carried by an error, or derive one."""

    details = exc.details
    candidates: list[Any] = []
    if isinstance(details, dict):
        candidates.append(details)
        cause = details.get("cause")
        if isinstance(cause, dict):
            candidates.append(cause)
    for candidate in candidates:
        failure = candidate.get("failure") if isinstance(candidate, dict) else None
        if isinstance(failure, dict) and failure.get("code"):
            return dict(failure)
    code = _PHASE_FAILURE_CODES.get(exc.phase, "internal")
    return _release_failure(code, source_changed=None, stage="unknown")


class RuntimeReleaseAdapter(Protocol):
    def preflight(self) -> dict[str, Any]: ...

    def restart_core(self, expected_commit: str) -> dict[str, Any]: ...

    def restart_remote(self) -> dict[str, Any]: ...

    def wait_for_version(self, expected_commit: str) -> dict[str, Any]: ...


def _progress(message: str) -> None:
    print(f"[hashi update] {message}", file=sys.stderr, flush=True)


def _bounded_output(value: str | None, *, limit: int = 4000) -> str:
    text = str(value or "").strip()
    return text[-limit:]


def _run(
    argv: Sequence[str],
    *,
    cwd: Path,
    phase: str,
    timeout: float = 300.0,
    env: dict[str, str] | None = None,
    check: bool = True,
    stdout: Any = subprocess.PIPE,
    stderr: Any = subprocess.PIPE,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            [str(item) for item in argv],
            cwd=cwd,
            env=env,
            check=False,
            capture_output=False,
            stdout=stdout,
            stderr=stderr,
            text=True,
            timeout=max(1.0, float(timeout)),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseUpdateError(phase, f"command could not complete: {exc}") from exc
    if check and completed.returncode != 0:
        details = _bounded_output(completed.stderr) or _bounded_output(completed.stdout)
        raise ReleaseUpdateError(
            phase,
            f"command failed with exit code {completed.returncode}",
            details=details,
        )
    return completed


def _git(
    root: Path,
    *args: str,
    phase: str = "source_update",
    check: bool = True,
    timeout: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    return _run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=root,
        phase=phase,
        timeout=timeout,
        check=check,
    )


def _git_text(root: Path, *args: str, phase: str = "source_update") -> str:
    return str(_git(root, *args, phase=phase).stdout or "").strip()


def _validate_ref_inputs(remote: str, branch: str) -> tuple[str, str]:
    selected_remote = str(remote or "").strip()
    selected_branch = str(branch or "").strip()
    if (
        not selected_remote
        or selected_remote.startswith("-")
        or any(char in selected_remote for char in "\r\n\x00")
    ):
        raise ReleaseUpdateError("source_preflight", "invalid Git remote name")
    if (
        not selected_branch
        or selected_branch.startswith("-")
        or any(char in selected_branch for char in "\r\n\x00")
    ):
        raise ReleaseUpdateError("source_preflight", "invalid Git branch name")
    return selected_remote, selected_branch


def _require_clean_checkout(
    root: Path,
    *,
    phase: str,
    failure_code: str = "local_changes",
    source_changed: bool = False,
    previous_commit: str | None = None,
) -> str:
    if not root.is_dir():
        raise ReleaseUpdateError(
            phase,
            f"source root does not exist: {root}",
            details={"failure": _release_failure(
                "instance_source", source_changed=source_changed,
                stage="pre_change" if not source_changed else "post_change",
                root=str(root),
            )},
        )
    inside = _git_text(root, "rev-parse", "--is-inside-work-tree", phase=phase)
    if inside.casefold() != "true":
        raise ReleaseUpdateError(
            phase,
            "selected instance is not a Git checkout",
            details={"failure": _release_failure(
                "instance_source", source_changed=source_changed,
                stage="pre_change" if not source_changed else "post_change",
                root=str(root),
            )},
        )
    status = _git_text(
        root,
        "status",
        "--porcelain",
        "--untracked-files=normal",
        phase=phase,
    )
    if status:
        summary = _dirty_summary(status)
        code = failure_code
        if (
            code == "local_changes"
            and summary.get("modified_count") == 0
            and summary.get("untracked_count")
        ):
            code = "untracked_only"
        raise ReleaseUpdateError(
            phase,
            "source checkout must be clean before release",
            details={
                "failure": _release_failure(
                    code,
                    source_changed=source_changed,
                    stage="pre_change" if not source_changed else "post_change",
                    previous_commit=previous_commit,
                    **summary,
                ),
                "status": _bounded_output(status),
            },
        )
    head = _git_text(root, "rev-parse", "HEAD", phase=phase).casefold()
    if not _HEX_COMMIT.fullmatch(head):
        raise ReleaseUpdateError(
            phase,
            "Git HEAD is not an exact commit",
            details={"failure": _release_failure(
                failure_code, source_changed=source_changed,
                stage="pre_change" if not source_changed else "post_change",
                previous_commit=head,
            )},
        )
    return head


def _with_failure(
    exc: ReleaseUpdateError,
    code: str,
    *,
    source_changed: bool,
    stage: str,
    **values: Any,
) -> ReleaseUpdateError:
    """Attach one structured failure class without dropping earlier detail."""

    details = dict(exc.details) if isinstance(exc.details, dict) else {}
    if exc.details not in (None, "", [], {}) and not isinstance(exc.details, dict):
        details["cause"] = exc.details
    details["failure"] = _release_failure(
        code, source_changed=source_changed, stage=stage, **values
    )
    return ReleaseUpdateError(exc.phase, str(exc), details=details)


def fast_forward_checkout(
    code_root: Path | str,
    *,
    remote: str = "origin",
    branch: str = "main",
    switch_to_branch: bool = True,
) -> dict[str, Any]:
    """Fetch one branch and advance the selected checkout by fast-forward only.

    A clean checkout on another branch is only accepted when that HEAD is an
    ancestor of the fetched target; it is then moved to the requested branch at
    the exact target commit.  Nothing is reset, forced or discarded.
    """

    root = Path(code_root).expanduser().resolve()
    try:
        selected_remote, selected_branch = _validate_ref_inputs(remote, branch)
    except ReleaseUpdateError as exc:
        raise _with_failure(
            exc, "instance_source", source_changed=False, stage="pre_change",
            remote=str(remote), branch=str(branch), root=str(root),
        ) from exc
    head_before = _git_text(root, "rev-parse", "HEAD", phase="source_preflight").casefold()
    old_commit = _require_clean_checkout(
        root, phase="source_preflight",
        previous_commit=head_before if _HEX_COMMIT.fullmatch(head_before) else None,
    )
    previous_branch = _git_text(
        root, "rev-parse", "--abbrev-ref", "HEAD", phase="source_preflight"
    )
    try:
        _git(root, "remote", "get-url", selected_remote, phase="source_preflight")
        _git(
            root,
            "check-ref-format",
            "--branch",
            selected_branch,
            phase="source_preflight",
        )
    except ReleaseUpdateError as exc:
        raise _with_failure(
            exc, "instance_source", source_changed=False, stage="pre_change",
            remote=selected_remote, branch=selected_branch, root=str(root),
            previous_branch=previous_branch, previous_commit=old_commit,
        ) from exc

    _progress(f"fetching {selected_remote}/{selected_branch}")
    try:
        _git(
            root,
            "fetch",
            "--no-tags",
            selected_remote,
            selected_branch,
            phase="source_fetch",
            timeout=300.0,
        )
    except ReleaseUpdateError as exc:
        raise _with_failure(
            exc, "fetch_failed", source_changed=False, stage="pre_change",
            remote=selected_remote, branch=selected_branch,
            previous_branch=previous_branch, previous_commit=old_commit,
        ) from exc
    target_commit = _git_text(
        root, "rev-parse", "--verify", "FETCH_HEAD^{commit}", phase="source_fetch"
    ).casefold()
    if not _HEX_COMMIT.fullmatch(target_commit):
        raise ReleaseUpdateError(
            "source_fetch",
            "fetched target is not a commit",
            details={"failure": _release_failure(
                "fetch_failed", source_changed=False, stage="pre_change",
                remote=selected_remote, branch=selected_branch,
                previous_branch=previous_branch, previous_commit=old_commit,
            )},
        )

    ancestry = _git(
        root,
        "merge-base",
        "--is-ancestor",
        old_commit,
        target_commit,
        phase="source_fast_forward",
        check=False,
    )
    if ancestry.returncode == 1:
        raise ReleaseUpdateError(
            "source_fast_forward",
            "fetched target is not a fast-forward of the deployed source",
            details={
                "current": old_commit,
                "target": target_commit,
                "failure": _release_failure(
                    "not_fast_forward", source_changed=False, stage="pre_change",
                    previous_branch=previous_branch, previous_commit=old_commit,
                    target_commit=target_commit, branch=selected_branch,
                ),
            },
        )
    if ancestry.returncode != 0:
        raise ReleaseUpdateError(
            "source_fast_forward",
            "could not verify source ancestry",
            details={
                "failure": _release_failure(
                    "not_fast_forward", source_changed=False, stage="pre_change",
                    previous_branch=previous_branch, previous_commit=old_commit,
                    target_commit=target_commit, branch=selected_branch,
                ),
                "status": _bounded_output(ancestry.stderr),
            },
        )

    changed = target_commit != old_commit
    switched = switch_to_branch and previous_branch != selected_branch
    try:
        if switched:
            _progress(
                f"checking out {selected_branch} at {target_commit[:12]} "
                f"(was {previous_branch})"
            )
            _git(
                root,
                "checkout",
                "-B",
                selected_branch,
                target_commit,
                phase="source_fast_forward",
            )
        elif changed:
            _progress(f"advancing source to {target_commit[:12]}")
            _git(
                root,
                "merge",
                "--ff-only",
                target_commit,
                phase="source_fast_forward",
            )
    except ReleaseUpdateError as exc:
        raise _with_failure(
            exc, "checkout_failed", source_changed=False, stage="pre_change",
            previous_branch=previous_branch, previous_commit=old_commit,
            target_commit=target_commit, branch=selected_branch,
        ) from exc
    adopted = _require_clean_checkout(
        root, phase="source_verify", failure_code="commit_mismatch", source_changed=True
    )
    if adopted != target_commit:
        raise ReleaseUpdateError(
            "source_verify",
            "source did not adopt the exact fetched commit",
            details={
                "expected": target_commit,
                "actual": adopted,
                "failure": _release_failure(
                    "commit_mismatch", source_changed=True, stage="post_change",
                    previous_branch=previous_branch, previous_commit=old_commit,
                    target_commit=target_commit, actual_commit=adopted,
                ),
            },
        )
    return {
        "old_commit": old_commit,
        "target_commit": target_commit,
        "changed": changed,
        "remote": selected_remote,
        "branch": selected_branch,
        "previous_branch": previous_branch,
        "switched_to_branch": switched,
        "forced": False,
    }


def force_align_checkout(
    code_root: Path | str,
    *,
    remote: str = "origin",
    branch: str = "main",
) -> dict[str, Any]:
    """Explicit force: preserve local work, then check out the fetched target.

    Order is deliberate: fetch first (a network failure changes nothing), then
    stash tracked and untracked changes (never ignored files), record a backup
    ref when the old HEAD is not a surviving named branch, and only then move
    the checkout.  Local commits are never reset away or force-pushed.
    """

    root = Path(code_root).expanduser().resolve()
    try:
        selected_remote, selected_branch = _validate_ref_inputs(remote, branch)
    except ReleaseUpdateError as exc:
        raise _with_failure(
            exc, "instance_source", source_changed=False, stage="pre_change",
            remote=str(remote), branch=str(branch), root=str(root),
        ) from exc
    if not root.is_dir():
        raise ReleaseUpdateError(
            "source_preflight",
            f"source root does not exist: {root}",
            details={"failure": _release_failure(
                "instance_source", source_changed=False, stage="pre_change",
                root=str(root),
            )},
        )
    try:
        inside = _git_text(root, "rev-parse", "--is-inside-work-tree", phase="source_preflight")
        _git(root, "remote", "get-url", selected_remote, phase="source_preflight")
        _git(root, "check-ref-format", "--branch", selected_branch, phase="source_preflight")
    except ReleaseUpdateError as exc:
        raise _with_failure(
            exc, "instance_source", source_changed=False, stage="pre_change",
            remote=selected_remote, branch=selected_branch, root=str(root),
        ) from exc
    if inside.casefold() != "true":
        raise ReleaseUpdateError(
            "source_preflight",
            "selected instance is not a Git checkout",
            details={"failure": _release_failure(
                "instance_source", source_changed=False, stage="pre_change",
                root=str(root),
            )},
        )

    previous_commit = _git_text(root, "rev-parse", "HEAD", phase="source_preflight").casefold()
    if not _HEX_COMMIT.fullmatch(previous_commit):
        raise ReleaseUpdateError(
            "source_preflight",
            "Git HEAD is not an exact commit",
            details={"failure": _release_failure(
                "instance_source", source_changed=False, stage="pre_change",
                root=str(root), previous_commit=previous_commit,
            )},
        )
    previous_branch = _git_text(
        root, "rev-parse", "--abbrev-ref", "HEAD", phase="source_preflight"
    )
    status_before = _git_text(
        root, "status", "--porcelain", "--untracked-files=normal", phase="source_preflight"
    )
    dirty = _dirty_summary(status_before)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    _progress(f"fetching {selected_remote}/{selected_branch}")
    try:
        _git(
            root, "fetch", "--no-tags", selected_remote, selected_branch,
            phase="source_fetch", timeout=300.0,
        )
    except ReleaseUpdateError as exc:
        raise _with_failure(
            exc, "fetch_failed", source_changed=False, stage="pre_change",
            remote=selected_remote, branch=selected_branch,
            previous_branch=previous_branch, previous_commit=previous_commit,
        ) from exc
    target_commit = _git_text(
        root, "rev-parse", "--verify", "FETCH_HEAD^{commit}", phase="source_fetch"
    ).casefold()
    if not _HEX_COMMIT.fullmatch(target_commit):
        raise ReleaseUpdateError(
            "source_fetch",
            "fetched target is not a commit",
            details={"failure": _release_failure(
                "fetch_failed", source_changed=False, stage="pre_change",
                remote=selected_remote, branch=selected_branch,
                previous_branch=previous_branch, previous_commit=previous_commit,
            )},
        )

    stash_commit: str | None = None
    stash_message: str | None = None
    if status_before:
        stash_message = (
            f"hashideploy-force {stamp} "
            f"{previous_branch or 'detached'}@{previous_commit[:12]}"
        )
        _progress("preserving local changes before checkout")
        try:
            stashed = _git(
                root, "stash", "push", "--include-untracked", "-m", stash_message,
                phase="source_force_stash", check=False,
            )
        except ReleaseUpdateError as exc:
            raise _with_failure(
                exc, "stash_failed", source_changed=False, stage="pre_change",
                previous_branch=previous_branch, previous_commit=previous_commit,
                target_commit=target_commit, dirty_count=dirty["dirty_count"],
                dirty_paths=dirty["dirty_paths"],
            ) from exc
        if stashed.returncode != 0:
            raise ReleaseUpdateError(
                "source_force_stash",
                "local changes could not be stashed safely",
                details={
                    "failure": _release_failure(
                        "stash_failed", source_changed=False, stage="pre_change",
                        previous_branch=previous_branch, previous_commit=previous_commit,
                        target_commit=target_commit, dirty_count=dirty["dirty_count"],
                        dirty_paths=dirty["dirty_paths"],
                    ),
                    "status": _bounded_output(stashed.stderr) or _bounded_output(stashed.stdout),
                },
            )
        try:
            stash_commit = _git_text(
                root, "rev-parse", "--verify", "refs/stash", phase="source_force_stash"
            ).casefold()
            if not _HEX_COMMIT.fullmatch(stash_commit):
                stash_commit = None
            remaining = _git_text(
                root, "status", "--porcelain", "--untracked-files=normal",
                phase="source_force_stash",
            )
        except ReleaseUpdateError as exc:
            raise _with_failure(
                exc, "stash_failed", source_changed=False, stage="pre_change",
                previous_branch=previous_branch, previous_commit=previous_commit,
                target_commit=target_commit, stash_message=stash_message,
            ) from exc
        if remaining:
            raise ReleaseUpdateError(
                "source_force_stash",
                "some local changes could not be preserved",
                details={
                    "failure": _release_failure(
                        "stash_incomplete", source_changed=False, stage="pre_change",
                        previous_branch=previous_branch, previous_commit=previous_commit,
                        target_commit=target_commit, stash_commit=stash_commit,
                        stash_message=stash_message,
                        **_dirty_summary(remaining),
                    ),
                    "status": _bounded_output(remaining),
                },
            )

    ancestry = _git(
        root, "merge-base", "--is-ancestor", previous_commit, target_commit,
        phase="source_force_backup", check=False,
    )
    if ancestry.returncode not in (0, 1):
        raise ReleaseUpdateError(
            "source_force_backup",
            "could not verify whether local commits were already merged",
            details={
                "failure": _release_failure(
                    "backup_failed", source_changed=False, stage="pre_change",
                    previous_branch=previous_branch, previous_commit=previous_commit,
                    target_commit=target_commit, stash_commit=stash_commit,
                    stash_message=stash_message,
                ),
                "status": _bounded_output(ancestry.stderr),
            },
        )
    # Any commit that is not part of the target history gets an explicit
    # backup ref; a named branch also keeps its own pointer untouched.
    backup_ref: str | None = None
    if ancestry.returncode == 1:
        backup_ref = f"refs/hashideploy-backups/{stamp}-{previous_commit[:12]}"
        _progress(f"recording pre-force HEAD at {backup_ref}")
        try:
            _git(
                root, "update-ref", backup_ref, previous_commit,
                phase="source_force_backup",
            )
        except ReleaseUpdateError as exc:
            raise _with_failure(
                exc, "backup_failed", source_changed=False, stage="pre_change",
                previous_branch=previous_branch, previous_commit=previous_commit,
                target_commit=target_commit, stash_commit=stash_commit,
                stash_message=stash_message,
            ) from exc

    _progress(f"checking out {selected_branch} at {target_commit[:12]}")
    checked_out = _git(
        root, "checkout", "-B", selected_branch, target_commit,
        phase="source_force_checkout", check=False,
    )
    if checked_out.returncode != 0:
        if previous_branch not in {"", "HEAD"}:
            _git(
                root, "checkout", previous_branch,
                phase="source_force_checkout", check=False,
            )
        raise ReleaseUpdateError(
            "source_force_checkout",
            "the checkout could not be moved to the fetched target",
            details={
                "failure": _release_failure(
                    "checkout_failed", source_changed=False, stage="pre_change",
                    previous_branch=previous_branch, previous_commit=previous_commit,
                    target_commit=target_commit, stash_commit=stash_commit,
                    stash_message=stash_message, backup_ref=backup_ref,
                ),
                "status": _bounded_output(checked_out.stderr)
                or _bounded_output(checked_out.stdout),
            },
        )
    adopted = _git_text(root, "rev-parse", "HEAD", phase="source_verify").casefold()
    remaining = _git_text(
        root, "status", "--porcelain", "--untracked-files=normal", phase="source_verify"
    )
    if adopted != target_commit or remaining:
        raise ReleaseUpdateError(
            "source_verify",
            "the forced checkout did not adopt the exact clean target",
            details={
                "failure": _release_failure(
                    "commit_mismatch", source_changed=True, stage="post_change",
                    previous_branch=previous_branch, previous_commit=previous_commit,
                    target_commit=target_commit, actual_commit=adopted,
                    stash_commit=stash_commit, stash_message=stash_message,
                    backup_ref=backup_ref, **_dirty_summary(remaining),
                ),
            },
        )
    return {
        "old_commit": previous_commit,
        "target_commit": target_commit,
        "changed": previous_commit != target_commit,
        "remote": selected_remote,
        "branch": selected_branch,
        "previous_branch": previous_branch,
        "switched_to_branch": previous_branch != selected_branch,
        "forced": True,
        "stash_commit": stash_commit,
        "stash_message": stash_message,
        "stashed_dirty_count": dirty["dirty_count"],
        "stashed_paths": dirty["dirty_paths"],
        "stashed_truncated": dirty["dirty_truncated"],
        "backup_ref": backup_ref,
    }


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _component_commit(value: Any) -> str:
    component = _mapping(value)
    return str(
        component.get("source_commit")
        or _mapping(component.get("provenance")).get("commit")
        or ""
    ).casefold()


def validate_release_payload(
    payload: dict[str, Any],
    *,
    expected_commit: str,
    expected_instance: str,
    expected_agents: Sequence[str] | None = None,
) -> list[str]:
    """Return every reason the live release is not the exact requested commit."""

    target = str(expected_commit or "").casefold()
    expected_id = str(expected_instance or "").strip().upper()
    issues: list[str] = []
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return ["version_payload_invalid"]

    instance = _mapping(payload.get("instance"))
    if str(instance.get("id") or "").strip().upper() != expected_id:
        issues.append("instance_mismatch")

    running = _mapping(payload.get("running"))
    if str(running.get("commit") or "").casefold() != target:
        issues.append("running_commit_mismatch")
    if _component_commit(running.get("functions")) != target:
        issues.append("functions_commit_mismatch")
    if running.get("mixed_workers") is True:
        issues.append("worker_generations_mixed")

    workers = running.get("workers")
    if not isinstance(workers, list) or not workers:
        issues.append("workers_missing")
    else:
        worker_names: set[str] = set()
        for index, raw_worker in enumerate(workers):
            worker = _mapping(raw_worker)
            label = str(worker.get("agent") or index)
            worker_names.add(label)
            if worker.get("phase") != "ACTIVE" or worker.get("accepting") is not True:
                issues.append(f"worker_not_ready:{label}")
            if _component_commit(worker) != target:
                issues.append(f"worker_commit_mismatch:{label}")
        required_agents = {
            str(agent).strip()
            for agent in (expected_agents or ())
            if str(agent).strip()
        }
        if required_agents and worker_names != required_agents:
            issues.append("worker_set_mismatch")

    remote = _mapping(running.get("remote"))
    if remote.get("status") != "active":
        issues.append("remote_not_active")
    remote_commit = _component_commit(remote)
    if not remote_commit:
        issues.append("remote_provenance_missing")
    elif remote_commit != target:
        issues.append("remote_commit_mismatch")

    source = _mapping(payload.get("source"))
    if source.get("available") is not True:
        issues.append("source_unavailable")
    if str(source.get("commit") or "").casefold() != target:
        issues.append("source_commit_mismatch")
    if source.get("dirty") is not False:
        issues.append("source_not_clean")

    state = _mapping(payload.get("state"))
    if state.get("code") != "running_matches_source":
        issues.append(f"version_state:{state.get('code') or 'missing'}")
    if state.get("reasons"):
        issues.append("version_reasons_present")

    return list(dict.fromkeys(issues))


def complete_runtime_release(
    adapter: RuntimeReleaseAdapter,
    *,
    expected_commit: str,
    expected_instance: str,
) -> dict[str, Any]:
    """Run the indivisible runtime portion of a source release."""

    preflight = adapter.preflight()
    core_restart = adapter.restart_core(expected_commit)
    remote_restart = adapter.restart_remote()
    version = adapter.wait_for_version(expected_commit)
    issues = validate_release_payload(
        version,
        expected_commit=expected_commit,
        expected_instance=expected_instance,
        expected_agents=_mapping(preflight.get("runtime")).get("agents"),
    )
    if issues:
        raise ReleaseUpdateError(
            "final_version_gate",
            "release is incomplete: " + ", ".join(issues),
            details={"issues": issues, "version": version},
        )
    return {
        "ok": True,
        "target_commit": expected_commit,
        "instance_id": expected_instance,
        "preflight": preflight,
        "core_restart": core_restart,
        "remote_restart": remote_restart,
        "version_state": _mapping(version.get("state")).get("code"),
    }


class LocalReleaseAdapter:
    """Real local Core/Remote lifecycle adapter used after source adoption."""

    def __init__(
        self,
        *,
        code_root: Path | str,
        bridge_home: Path | str,
        instance_id: str,
        api_port: int,
        expected_commit: str,
        timeout: float = 300.0,
        python_executable: str | None = None,
        platform_name: str | None = None,
    ):
        self.code_root = Path(code_root).expanduser().resolve()
        self.bridge_home = Path(bridge_home).expanduser().resolve()
        self.instance_id = str(instance_id).strip().upper()
        self.api_port = int(api_port)
        self.expected_commit = str(expected_commit).casefold()
        self.timeout = max(30.0, float(timeout))
        self.python_executable = str(python_executable or sys.executable)
        self.platform_name = str(platform_name or os.name)
        self.expected_agents: set[str] = set()
        self.shared_token = load_shared_token(self.bridge_home)
        if not self.shared_token:
            raise ReleaseUpdateError(
                "runtime_preflight",
                "Remote shared-token authentication is not configured",
            )

    def _http_json(
        self,
        method: str,
        path: str,
        *,
        token: str = "",
        timeout: float = 5.0,
    ) -> tuple[int, dict[str, Any]]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.api_port, timeout=max(0.5, float(timeout))
        )
        headers = {"Accept": "application/json"}
        if token:
            headers["X-Workbench-Token"] = token
        try:
            connection.request(method, path, headers=headers)
            response = connection.getresponse()
            raw = response.read(_MAX_HTTP_BODY + 1)
            if len(raw) > _MAX_HTTP_BODY:
                raise ReleaseUpdateError(
                    "runtime_probe", "local HASHI response exceeded 1 MiB"
                )
            value = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(value, dict):
                raise ReleaseUpdateError(
                    "runtime_probe", "local HASHI returned non-object JSON"
                )
            return int(response.status), value
        finally:
            connection.close()

    def _runtime_activity(self) -> dict[str, Any]:
        try:
            health_status, health = self._http_json("GET", "/api/health")
            agents_status, agents_payload = self._http_json("GET", "/api/agents")
            jobs_status, jobs_payload = self._http_json(
                "GET",
                "/api/background-jobs?state=created,starting,running,cancel_requested&limit=200",
            )
        except Exception as exc:
            if isinstance(exc, ReleaseUpdateError):
                raise
            raise ReleaseUpdateError(
                "runtime_preflight", f"local HASHI health is unavailable: {exc}"
            ) from exc

        if (
            health_status != 200
            or health.get("ok") is not True
            or str(health.get("instance_id") or "").strip().upper() != self.instance_id
            or health.get("ready") is not True
        ):
            raise ReleaseUpdateError(
                "runtime_preflight",
                "selected HASHI instance is not healthy and ready",
                details=health,
            )
        agents = agents_payload.get("agents")
        jobs = jobs_payload.get("jobs")
        if agents_status != 200 or not isinstance(agents, list) or not agents:
            raise ReleaseUpdateError(
                "runtime_preflight", "Agent activity could not be verified"
            )
        if jobs_status != 200 or not isinstance(jobs, list):
            raise ReleaseUpdateError(
                "runtime_preflight", "background-job activity could not be verified"
            )
        agent_names = {
            str(item.get("name") or item.get("id") or "").strip()
            for item in agents
            if isinstance(item, dict)
        }
        if "" in agent_names or len(agent_names) != len(agents):
            raise ReleaseUpdateError(
                "runtime_preflight", "Agent identity could not be verified"
            )
        busy_agents = [
            str(item.get("name") or item.get("id") or "unknown")
            for item in agents
            if isinstance(item, dict)
            and (
                item.get("is_generating") is True
                or int(item.get("queue_depth") or 0) > 0
                or item.get("active_transfer") is True
            )
        ]
        busy_jobs = [
            str(item.get("job_id") or "unknown")
            for item in jobs
            if isinstance(item, dict)
        ]
        if busy_agents or busy_jobs:
            raise ReleaseUpdateError(
                "runtime_preflight",
                "selected HASHI instance is busy; no release action was started",
                details={"agents": busy_agents, "background_jobs": busy_jobs},
            )
        return {
            "ready": True,
            "agent_count": len(agents),
            "agents": sorted(agent_names, key=str.casefold),
            "background_jobs": 0,
        }

    def _remote_capabilities(self) -> dict[str, Any]:
        try:
            payload = probe_capabilities(
                self.instance_id,
                shared_token=self.shared_token,
                from_instance=self.instance_id,
                timeout=10,
                root=self.bridge_home,
                local_instance_id=self.instance_id,
            )
        except Exception as exc:
            raise ReleaseUpdateError(
                "remote_preflight",
                f"independent Remote is not reachable: {exc}",
            ) from exc
        capabilities = _mapping(payload.get("capabilities"))
        supervisor = _mapping(payload.get("remote_supervisor"))
        if (
            payload.get("ok") is not True
            or capabilities.get("rescue_restart") is not True
            or supervisor.get("mode") != "supervised"
        ):
            raise ReleaseUpdateError(
                "remote_preflight",
                "independent supervised Remote cannot perform a verified Core restart",
                details=payload,
            )
        return {
            "base_url": payload.get("base_url"),
            "supervisor_mode": supervisor.get("mode"),
            "rescue_restart": True,
        }

    def preflight(self) -> dict[str, Any]:
        head = _require_clean_checkout(self.code_root, phase="runtime_preflight")
        if head != self.expected_commit:
            raise ReleaseUpdateError(
                "runtime_preflight",
                "post-update process is not running from the fetched commit",
                details={"expected": self.expected_commit, "actual": head},
            )
        _progress("verifying idle Core and independent Remote")
        runtime = self._runtime_activity()
        self.expected_agents = set(runtime["agents"])
        return {
            "runtime": runtime,
            "remote": self._remote_capabilities(),
        }

    def restart_core(self, expected_commit: str) -> dict[str, Any]:
        _progress("asking independent Remote to cold-restart HASHI Core")
        try:
            code, payload = rescue_restart(
                self.instance_id,
                reason=f"closed-loop source release {expected_commit[:12]}",
                target_instance=self.instance_id,
                extra_payload={"request_source": "hashi_update"},
                shared_token=self.shared_token,
                from_instance=self.instance_id,
                timeout=int(self.timeout),
                root=self.bridge_home,
                local_instance_id=self.instance_id,
            )
        except Exception as exc:
            raise ReleaseUpdateError(
                "core_restart", f"Remote could not restart HASHI Core: {exc}"
            ) from exc
        if (
            code != 0
            or payload.get("ok") is not True
            or payload.get("state") != "completed"
        ):
            raise ReleaseUpdateError(
                "core_restart",
                "Remote did not produce a successful terminal Core restart receipt",
                details=payload,
            )
        return {
            "restart_id": payload.get("restart_id"),
            "state": payload.get("state"),
            "old_pid": _mapping(payload.get("evidence")).get("old_pid"),
            "new_pid": _mapping(payload.get("evidence")).get("new_pid"),
        }

    def _controller_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        environment.update(
            {
                "HASHI_ROOT": str(self.code_root),
                "HASHI_INSTANCE_ID": self.instance_id,
                "HASHI_REMOTE_PYTHON": self.python_executable,
                "BRIDGE_HOME": str(self.bridge_home),
                "BRIDGE_CODE_ROOT": str(self.code_root),
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        return environment

    def restart_remote(self) -> dict[str, Any]:
        _progress("re-registering and restarting independent Remote")
        environment = self._controller_environment()
        if self.platform_name == "nt":
            controller = self.code_root / "bin" / "hashi_remote_ctl.ps1"
            powershell = shutil.which("powershell.exe") or "powershell.exe"
            base = [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(controller),
            ]
            common = [
                "-HashiRoot",
                str(self.code_root),
                "-Python",
                self.python_executable,
                "-InstanceId",
                self.instance_id,
            ]
            _run(
                [*base, "register", *common],
                cwd=self.code_root,
                phase="remote_register",
                timeout=90.0,
                env=environment,
            )
            _run(
                [*base, "restart", *common],
                cwd=self.code_root,
                phase="remote_restart",
                timeout=90.0,
                env=environment,
            )
            return {"controller": "windows_scheduled_task", "re_registered": True}
        controller = self.code_root / "bin" / "hashi-remote-ctl.sh"
        _run(
            ["bash", str(controller), "register"],
            cwd=self.code_root,
            phase="remote_register",
            timeout=90.0,
            env=environment,
        )
        _run(
            ["bash", str(controller), "restart"],
            cwd=self.code_root,
            phase="remote_restart",
            timeout=90.0,
            env=environment,
        )
        return {"controller": "systemd_user", "re_registered": True}

    def wait_for_version(self, expected_commit: str) -> dict[str, Any]:
        _progress("waiting for the all-component version gate")
        deadline = time.monotonic() + self.timeout
        last_payload: dict[str, Any] | None = None
        last_error = ""
        while time.monotonic() < deadline:
            try:
                status, payload = self._http_json("GET", "/api/version", timeout=5.0)
                if status == 200:
                    last_payload = payload
                    issues = validate_release_payload(
                        payload,
                        expected_commit=expected_commit,
                        expected_instance=self.instance_id,
                        expected_agents=self.expected_agents,
                    )
                    if not issues:
                        return payload
                    last_error = ", ".join(issues)
                else:
                    last_error = f"HTTP {status}"
            except Exception as exc:
                last_error = str(exc)
            time.sleep(0.5)
        if last_payload is not None:
            return last_payload
        raise ReleaseUpdateError(
            "final_version_gate",
            "HASHI version endpoint did not recover before timeout",
            details=last_error,
        )


def _release_environment(
    code_root: Path,
    bridge_home: Path,
    instance_id: str,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment.update(
        {
            "HASHI_INSTANCE_ID": instance_id,
            "BRIDGE_HOME": str(bridge_home),
            "BRIDGE_CODE_ROOT": str(code_root),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return environment


def _runtime_release_argv(
    *,
    runtime_command: Sequence[str],
    code_root: Path,
    release_home: Path,
    instance_id: str,
    api_port: int,
    expected_commit: str,
    timeout: float,
) -> list[str]:
    # bridge_ctl.ps1 identifies Core-owned Python processes by --bridge-home.
    # This coordinator must survive the Core restart, so it uses a distinct
    # private option that the runtime controller cannot mistake for ownership.
    return [
        *runtime_command,
        "-m",
        "tools.release_update",
        "deploy",
        "--code-root",
        str(code_root),
        "--release-home",
        str(release_home),
        "--instance-id",
        str(instance_id),
        "--api-port",
        str(int(api_port)),
        "--expected-commit",
        str(expected_commit),
        "--timeout",
        str(int(timeout)),
    ]


def _verify_release_source(root: Path, home: Path, instance_id: str, runtime_command: Sequence[str]) -> None:
    protected_check = root / "scripts" / "check_protected_core_changes.py"
    if not protected_check.is_file():
        raise ReleaseUpdateError("source_verify", "protected-Core release gate is missing")
    _run(
        [*runtime_command, str(protected_check)], cwd=root,
        phase="protected_core_gate", timeout=120.0,
        env=_release_environment(root, home, instance_id),
    )


def _qualify_staged_functions(root: Path, home: Path, staging: Path) -> dict[str, Any]:
    # Reuse the owning qualification contract, without preparing/activating a
    # live application or publishing the active generation/recovery pointer.
    from types import SimpleNamespace
    from orchestrator.function_generation import probe_function_generation
    from orchestrator.function_worker_supervisor import materialize_generation_artifact
    from orchestrator.pathing import build_bridge_paths
    from orchestrator.runtime_contract import (
        bind_process_runtime, enforce_runtime_contract, load_core_runtime_selection,
    )

    try:
        selection = load_core_runtime_selection(root)
        bind_process_runtime(selection.functions, code_root=root)
        runtime = enforce_runtime_contract(root)
    except Exception as exc:
        raise ReleaseUpdateError(
            "stage_runtime",
            "The selected Core/Functions runtime pair cannot run this source. "
            "Prepare a qualified runtime pair for the target before retrying; "
            "no packages were installed and nothing was restarted.",
            details=str(exc),
        ) from exc
    try:
        kernel = SimpleNamespace(
            paths=build_bridge_paths(root, home, canonical_home=True),
            runtime_fingerprint=runtime,
        )
        generation = probe_function_generation(kernel)
        artifact = materialize_generation_artifact(staging, generation)
    except Exception as exc:
        raise ReleaseUpdateError(
            "stage_qualification",
            "The fetched Functions failed isolated qualification; fix the reported "
            "source or dependency problem before adopting it.",
            details=str(exc),
        ) from exc
    return {
        "source_commit": generation.receipt.source_commit,
        "generation_id": generation.manifest.generation_id,
        "artifact_root": str(artifact),
        "functions_runtime_id": selection.functions.runtime_id,
        "core_runtime_id": selection.core.runtime_id,
    }


def _normalize_source_meta(value: Any) -> dict[str, Any]:
    """Accept only the bounded force evidence the parent passed to staging."""

    if value in (None, "", {}):
        return {}
    if not isinstance(value, dict):
        raise ReleaseUpdateError("stage_verify", "source metadata must be an object")
    forced = value.get("forced") is True
    if not forced:
        return {}
    meta: dict[str, Any] = {"forced": True}
    for key in ("previous_branch", "previous_commit", "stash_commit", "backup_ref"):
        item = value.get(key)
        if item is None:
            continue
        text = str(item).strip()
        if not text or len(text) > 200 or any(ch in text for ch in "\r\n\x00"):
            raise ReleaseUpdateError("stage_verify", f"invalid {key}")
        if key.endswith("commit") and not _HEX_COMMIT.fullmatch(text.casefold()):
            raise ReleaseUpdateError("stage_verify", f"invalid {key}")
        meta[key] = text
    message = value.get("stash_message")
    if isinstance(message, str) and message.strip():
        meta["stash_message"] = message.strip()[:200]
    count = value.get("stashed_dirty_count")
    if isinstance(count, int) and count >= 0:
        meta["stashed_dirty_count"] = count
    paths = value.get("stashed_paths")
    if isinstance(paths, list):
        meta["stashed_paths"] = [str(p)[:300] for p in paths[:8]]
    return meta


def prepare_staged_release(
    *, code_root: Path | str, bridge_home: Path | str,
    instance_id: str, expected_commit: str, source_meta: Any = None,
) -> dict[str, Any]:
    """Qualify committed source and persist a receipt; never activate it."""
    from datetime import datetime
    from uuid import uuid4
    from orchestrator.config_json import write_config_json

    root = Path(code_root).expanduser().resolve()
    home = Path(bridge_home).expanduser().resolve()
    target = str(expected_commit).casefold()
    meta = _normalize_source_meta(source_meta)
    if not _HEX_COMMIT.fullmatch(target):
        raise ReleaseUpdateError("stage_verify", "expected source commit is invalid")
    if _require_clean_checkout(
        root, phase="stage_verify", failure_code="commit_mismatch", source_changed=True
    ) != target:
        raise ReleaseUpdateError(
            "stage_verify", "source commit changed before qualification",
            details={"failure": _release_failure(
                "commit_mismatch", source_changed=True, stage="post_change",
                target_commit=target,
            )},
        )
    staging = home / "state" / "source_deployments" / uuid4().hex
    qualification = _qualify_staged_functions(root, home, staging)
    if qualification.get("source_commit") != target:
        raise ReleaseUpdateError(
            "stage_verify", "qualified source commit differs from target",
            details={"failure": _release_failure(
                "commit_mismatch", source_changed=True, stage="post_change",
                target_commit=target,
                actual_commit=str(qualification.get("source_commit") or ""),
            )},
        )
    if _require_clean_checkout(
        root, phase="stage_verify", failure_code="commit_mismatch", source_changed=True
    ) != target:
        raise ReleaseUpdateError(
            "stage_verify", "source commit changed during qualification",
            details={"failure": _release_failure(
                "commit_mismatch", source_changed=True, stage="post_change",
                target_commit=target,
            )},
        )
    receipt_path = staging / "ready.json"
    result = {
        "ok": True, "state": "ready_to_reboot", "message": "Ready to reboot",
        "instance_id": instance_id, "target_commit": target,
        "code_root": str(root), "bridge_home": str(home),
        "restarts_performed": False, "running_verified": False,
        "forced": bool(meta.get("forced")),
        "force": meta if meta.get("forced") else {},
        "qualification": qualification, "receipt_path": str(receipt_path),
        "validated_at": datetime.now().astimezone().isoformat(),
        "next_action": "Adopt the prepared Functions with /reboot when ready, then "
                       "check /version full. Core and Remote adoption remain separate.",
    }
    try:
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        write_config_json(receipt_path, result, expected_revision=None)
    except Exception as exc:
        raise ReleaseUpdateError(
            "stage_receipt", "Could not durably record readiness; inspect the "
            "receipt before retrying. Nothing was restarted.",
            details={
                "cause": str(exc),
                "failure": _release_failure(
                    "receipt_failed", source_changed=True, stage="post_change",
                    target_commit=target,
                ),
            },
        ) from exc
    return result


def run_staged_source_release(
    *, code_root: Path | str, bridge_home: Path | str, instance_id: str,
    runtime_command: Sequence[str], remote: str = "origin", branch: str = "main",
    timeout: float = 300.0, force: bool = False,
) -> dict[str, Any]:
    """Fetch, verify and prepare source without any live lifecycle adapter."""
    from orchestrator.config_json import file_write_lock

    root = Path(code_root).expanduser().resolve()
    home = Path(bridge_home).expanduser().resolve()
    if not runtime_command:
        raise ReleaseUpdateError("stage_runtime", "a qualified management runtime is required")
    source = None
    try:
        (home / "state").mkdir(parents=True, exist_ok=True)
        with file_write_lock(home / "state" / "source_deployment.lock"):
            if force:
                source = force_align_checkout(root, remote=remote, branch=branch)
            else:
                source = fast_forward_checkout(root, remote=remote, branch=branch)
            _verify_release_source(root, home, instance_id, runtime_command)
            # An older fetched coordinator rejects the distinct 'stage' verb;
            # it can never silently fall back to the restarting 'deploy' verb.
            argv = [
                *runtime_command, "-m", "tools.release_update", "stage",
                "--code-root", str(root), "--release-home", str(home),
                "--instance-id", instance_id,
                "--expected-commit", source["target_commit"],
            ]
            if source.get("forced"):
                argv += ["--source-meta", json.dumps({
                    "forced": True,
                    "previous_branch": source.get("previous_branch"),
                    "previous_commit": source.get("old_commit"),
                    "stash_commit": source.get("stash_commit"),
                    "stash_message": source.get("stash_message"),
                    "backup_ref": source.get("backup_ref"),
                    "stashed_dirty_count": source.get("stashed_dirty_count"),
                    "stashed_paths": source.get("stashed_paths"),
                }, ensure_ascii=False, sort_keys=True)]
            completed = _run(
                argv, cwd=root, phase="stage_qualification",
                timeout=float(timeout) + 180.0,
                env=_release_environment(root, home, instance_id), check=False,
                stderr=None,
            )
            try:
                result = json.loads(completed.stdout or "")
            except (ValueError, TypeError) as exc:
                raise ReleaseUpdateError(
                    "stage_qualification", "The fetched coordinator did not return a "
                    "staging receipt. It may not support stage-only deployment.",
                    details=_bounded_output(completed.stdout),
                ) from exc
            if not isinstance(result, dict):
                raise ReleaseUpdateError("stage_qualification", "invalid staging receipt")
            if completed.returncode != 0 or result.get("ok") is not True:
                raise ReleaseUpdateError(
                    str(result.get("phase") or "stage_qualification"),
                    str(result.get("error") or "source staging failed"),
                    details=result.get("details"),
                )
            if (
                result.get("state") != "ready_to_reboot"
                or result.get("target_commit") != source["target_commit"]
                or result.get("instance_id") != instance_id
                or result.get("restarts_performed") is not False
                or result.get("running_verified") is not False
            ):
                raise ReleaseUpdateError("stage_verify", "staging receipt does not match the request")
            if _require_clean_checkout(root, phase="stage_verify") != source["target_commit"]:
                raise ReleaseUpdateError("stage_verify", "source changed after qualification")
            return {**result, "source": source}
    except ReleaseUpdateError as exc:
        failure = release_failure(exc)
        if failure.get("source_changed") is None and isinstance(source, dict) and source:
            # Reaching the stage means the checkout had already moved.
            failure["source_changed"] = True
            failure["stage"] = "post_change"
            failure.setdefault("target_commit", source.get("target_commit"))
        raise ReleaseUpdateError(
            exc.phase, str(exc),
            details={"cause": exc.details, "source": source, "failure": failure,
                     "restarts_performed": False,
                     "next_action": "Inspect the reported source/runtime error before "
                                    "retrying or adopting. Source is retained at the "
                                    "reported target; no automatic rewind was attempted."},
        ) from exc
    except OSError as exc:
        raise ReleaseUpdateError(
            "stage_io", "Source staging could not access or lock the deployment files. "
            "Check permissions and whether another deployment is in progress.",
            details={
                "source": source, "restarts_performed": False, "cause": str(exc),
                "failure": _release_failure(
                    "state_io", source_changed=None, stage="unknown",
                ),
            },
        ) from exc


def run_source_release(
    *,
    code_root: Path | str,
    bridge_home: Path | str,
    instance_id: str,
    api_port: int,
    runtime_command: Sequence[str],
    remote: str = "origin",
    branch: str = "main",
    timeout: float = 300.0,
    stage_only: bool = False,
) -> dict[str, Any]:
    """Preflight, fast-forward, re-exec updated coordinator, and require closure."""

    if stage_only:
        return run_staged_source_release(
            code_root=code_root, bridge_home=bridge_home, instance_id=instance_id,
            runtime_command=runtime_command, remote=remote, branch=branch, timeout=timeout,
        )

    root = Path(code_root).expanduser().resolve()
    home = Path(bridge_home).expanduser().resolve()
    current = _require_clean_checkout(root, phase="source_preflight")
    preflight_adapter = LocalReleaseAdapter(
        code_root=root,
        bridge_home=home,
        instance_id=instance_id,
        api_port=api_port,
        expected_commit=current,
        timeout=timeout,
        python_executable=str(runtime_command[0]),
    )
    preflight_adapter.preflight()
    source = fast_forward_checkout(root, remote=remote, branch=branch)

    _verify_release_source(root, home, instance_id, runtime_command)

    argv = _runtime_release_argv(
        runtime_command=runtime_command,
        code_root=root,
        release_home=home,
        instance_id=instance_id,
        api_port=api_port,
        expected_commit=str(source["target_commit"]),
        timeout=timeout,
    )
    _progress("continuing with the coordinator from the fetched commit")
    completed = _run(
        argv,
        cwd=root,
        phase="runtime_release",
        timeout=float(timeout) + 180.0,
        env=_release_environment(root, home, instance_id),
        check=False,
        stdout=subprocess.PIPE,
        stderr=None,
    )
    if not str(completed.stdout or "").strip():
        raise ReleaseUpdateError(
            "runtime_release",
            "updated release coordinator exited without a receipt",
            details={"exit_code": completed.returncode},
        )
    try:
        child_payload = json.loads(str(completed.stdout or "").strip())
    except json.JSONDecodeError as exc:
        raise ReleaseUpdateError(
            "runtime_release",
            "updated release coordinator returned invalid output",
            details=_bounded_output(completed.stdout),
        ) from exc
    if completed.returncode != 0 or child_payload.get("ok") is not True:
        raise ReleaseUpdateError(
            str(child_payload.get("phase") or "runtime_release"),
            str(child_payload.get("error") or "updated release coordinator failed"),
            details=child_payload.get("details"),
        )
    return {
        **child_payload,
        "source": source,
    }


def _deploy_command(args: argparse.Namespace) -> int:
    try:
        adapter = LocalReleaseAdapter(
            code_root=args.code_root,
            bridge_home=args.bridge_home,
            instance_id=args.instance_id,
            api_port=args.api_port,
            expected_commit=args.expected_commit,
            timeout=args.timeout,
        )
        result = complete_runtime_release(
            adapter,
            expected_commit=args.expected_commit,
            expected_instance=args.instance_id,
        )
    except ReleaseUpdateError as exc:
        print(json.dumps(exc.to_payload(), ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    commands = parser.add_subparsers(dest="command", required=True)
    deploy = commands.add_parser("deploy", add_help=False)
    deploy.add_argument("--code-root", type=Path, required=True)
    deploy.add_argument("--release-home", dest="bridge_home", type=Path, required=True)
    deploy.add_argument("--instance-id", required=True)
    deploy.add_argument("--api-port", type=int, required=True)
    deploy.add_argument("--expected-commit", required=True)
    deploy.add_argument("--timeout", type=float, default=300.0)
    stage = commands.add_parser("stage", add_help=False)
    stage.add_argument("--code-root", type=Path, required=True)
    stage.add_argument("--release-home", dest="bridge_home", type=Path, required=True)
    stage.add_argument("--instance-id", required=True)
    stage.add_argument("--expected-commit", required=True)
    stage.add_argument("--source-meta", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    if args.command == "deploy":
        return _deploy_command(args)
    if args.command == "stage":
        try:
            source_meta = json.loads(args.source_meta) if args.source_meta else None
        except ValueError:
            print(json.dumps({
                "ok": False, "phase": "stage_verify",
                "error": "source metadata is not valid JSON",
            }, ensure_ascii=False, sort_keys=True))
            return 1
        try:
            result = prepare_staged_release(
                code_root=args.code_root, bridge_home=args.bridge_home,
                instance_id=args.instance_id, expected_commit=args.expected_commit,
                source_meta=source_meta,
            )
        except ReleaseUpdateError as exc:
            print(json.dumps(exc.to_payload(), ensure_ascii=False, sort_keys=True))
            return 1
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
# HASHI-Fingerprint: hfp_82b4e0bed97a46b6bf71b830b1d13837
