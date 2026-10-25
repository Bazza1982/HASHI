"""Closed-loop source release coordinator for a registered HASHI instance.

A source release is successful only after the fetched commit is running in Core,
Functions, every Worker, and the independently supervised Remote process.
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
        ["git", *args],
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


def _require_clean_checkout(root: Path, *, phase: str) -> str:
    if not root.is_dir():
        raise ReleaseUpdateError(phase, f"source root does not exist: {root}")
    inside = _git_text(root, "rev-parse", "--is-inside-work-tree", phase=phase)
    if inside.casefold() != "true":
        raise ReleaseUpdateError(phase, "selected instance is not a Git checkout")
    status = _git_text(
        root,
        "status",
        "--porcelain",
        "--untracked-files=normal",
        phase=phase,
    )
    if status:
        raise ReleaseUpdateError(
            phase,
            "source checkout must be clean before release",
            details=_bounded_output(status),
        )
    head = _git_text(root, "rev-parse", "HEAD", phase=phase).casefold()
    if not _HEX_COMMIT.fullmatch(head):
        raise ReleaseUpdateError(phase, "Git HEAD is not an exact commit")
    return head


def fast_forward_checkout(
    code_root: Path | str,
    *,
    remote: str = "origin",
    branch: str = "main",
) -> dict[str, Any]:
    """Fetch one branch and advance the selected checkout by fast-forward only."""

    root = Path(code_root).expanduser().resolve()
    selected_remote, selected_branch = _validate_ref_inputs(remote, branch)
    old_commit = _require_clean_checkout(root, phase="source_preflight")
    _git(root, "remote", "get-url", selected_remote, phase="source_preflight")
    _git(
        root,
        "check-ref-format",
        "--branch",
        selected_branch,
        phase="source_preflight",
    )

    _progress(f"fetching {selected_remote}/{selected_branch}")
    _git(
        root,
        "fetch",
        "--no-tags",
        selected_remote,
        selected_branch,
        phase="source_fetch",
        timeout=300.0,
    )
    target_commit = _git_text(
        root, "rev-parse", "--verify", "FETCH_HEAD^{commit}", phase="source_fetch"
    ).casefold()
    if not _HEX_COMMIT.fullmatch(target_commit):
        raise ReleaseUpdateError("source_fetch", "fetched target is not a commit")

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
            details={"current": old_commit, "target": target_commit},
        )
    if ancestry.returncode != 0:
        raise ReleaseUpdateError(
            "source_fast_forward",
            "could not verify source ancestry",
            details=_bounded_output(ancestry.stderr),
        )

    changed = target_commit != old_commit
    if changed:
        _progress(f"advancing source to {target_commit[:12]}")
        _git(
            root,
            "merge",
            "--ff-only",
            target_commit,
            phase="source_fast_forward",
        )
    adopted = _require_clean_checkout(root, phase="source_verify")
    if adopted != target_commit:
        raise ReleaseUpdateError(
            "source_verify",
            "source did not adopt the exact fetched commit",
            details={"expected": target_commit, "actual": adopted},
        )
    return {
        "old_commit": old_commit,
        "target_commit": target_commit,
        "changed": changed,
        "remote": selected_remote,
        "branch": selected_branch,
    }


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _provenance_commit(value: Any) -> str:
    return str(
        _mapping(_mapping(value).get("provenance")).get("commit") or ""
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
    if _provenance_commit(running.get("functions")) != target:
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
            if _provenance_commit(worker) != target:
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
    remote_commit = _provenance_commit(remote)
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
) -> dict[str, Any]:
    """Preflight, fast-forward, re-exec updated coordinator, and require closure."""

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

    protected_check = root / "scripts" / "check_protected_core_changes.py"
    if not protected_check.is_file():
        raise ReleaseUpdateError(
            "source_verify", "protected-Core release gate is missing"
        )
    _run(
        [*runtime_command, str(protected_check)],
        cwd=root,
        phase="protected_core_gate",
        timeout=120.0,
        env=_release_environment(root, home, instance_id),
    )

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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    if args.command == "deploy":
        return _deploy_command(args)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
