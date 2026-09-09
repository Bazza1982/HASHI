#!/usr/bin/env python3
"""Stable npm-facing HASHI command and multi-instance control surface."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

CODE_ROOT = Path(__file__).resolve().parent.parent
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from tools.instance_registry import (  # noqa: E402
    InstanceRegistry,
    InstanceRegistryError,
    InstanceSelectionError,
    configured_identity,
)

EXIT_USAGE = 64
EXIT_NOT_READY = 69
EXIT_BUSY = 75
EXIT_RUNTIME = 78
DEFAULT_START_TIMEOUT_SECONDS = 45.0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hashi",
        description="One HASHI program, with isolated named instances.",
    )
    parser.add_argument("--instance", help="Select an exact registered instance name.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output.")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("start", help="Start the selected instance in the background.")
    commands.add_parser("tui", help="Open the terminal UI for the selected instance.")
    commands.add_parser("status", help="Show selected instance and live process state.")
    commands.add_parser("stop", help="Gracefully stop an idle selected instance.")
    commands.add_parser("onboard", help=argparse.SUPPRESS)
    commands.add_parser("ui", help="Open an installed external HASHI UI.")
    commands.add_parser("help", help="Show this help.")

    instance = commands.add_parser("instance", help="Manage isolated instances.")
    instance_commands = instance.add_subparsers(dest="instance_command", required=True)

    create = instance_commands.add_parser("create", help="Create or register an instance.")
    create.add_argument("name")
    create.add_argument(
        "--from",
        dest="source_root",
        type=Path,
        help="Register an existing HASHI Git/program root without changing it.",
    )
    create.add_argument(
        "--home",
        type=Path,
        help="Existing bridge home (requires --from; defaults to that code root).",
    )
    create.add_argument(
        "--bind",
        type=Path,
        help="Bind this directory (and descendants) to the instance.",
    )
    create.add_argument("--default", action="store_true", help="Make it the default.")

    instance_commands.add_parser("list", help="List registered instances.")
    default = instance_commands.add_parser("default", help="Set the default instance.")
    default.add_argument("name")
    bind = instance_commands.add_parser("bind", help="Bind a directory to an instance.")
    bind.add_argument("name")
    bind.add_argument("path", nargs="?", type=Path, default=Path.cwd())
    remove = instance_commands.add_parser(
        "remove", help="Unregister and retain recoverable data by default."
    )
    remove.add_argument("name")
    remove.add_argument("--purge", action="store_true")
    remove.add_argument("--confirm")
    restore = instance_commands.add_parser(
        "restore", help="Restore the newest recoverable removal."
    )
    restore.add_argument("name")
    adopt = instance_commands.add_parser(
        "adopt", help="Explicitly adopt the installed program version."
    )
    adopt.add_argument("name")
    return parser


def _emit(value: Any, *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    elif isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _validate_environment_path(path: Path) -> None:
    rendered = str(path)
    if sys.platform == "win32" and rendered.casefold().startswith(
        ("\\\\wsl$\\", "\\\\wsl.localhost\\")
    ):
        raise InstanceRegistryError(
            "A Windows HASHI command cannot launch a WSL checkout. Run hashi inside WSL."
        )
    if sys.platform != "win32" and path.suffix.casefold() == ".exe":
        raise InstanceRegistryError(
            "A POSIX/WSL HASHI command cannot adopt a Windows executable runtime."
        )


def _runtime_candidates(code_root: Path) -> list[list[str]]:
    values: list[list[str]] = []
    explicit = str(os.environ.get("HASHI_PYTHON") or "").strip()
    if explicit:
        values.append([explicit])
    if sys.platform == "win32":
        values.extend(
            [
                [str(code_root / "python" / "python.exe")],
                [str(code_root / ".venv" / "Scripts" / "python.exe")],
            ]
        )
    else:
        values.extend(
            [
                [str(code_root / "python" / "bin" / "python3")],
                [str(code_root / ".venv-wsl" / "bin" / "python")],
                [str(code_root / ".venv" / "bin" / "python")],
            ]
        )
    values.append([sys.executable])
    values.append(["python3.12"])
    if sys.platform == "win32":
        values.append(["py", "-3.12"])
    values.extend([["python3"], ["python"]])
    unique: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for command in values:
        key = tuple(command)
        if key not in seen:
            unique.append(command)
            seen.add(key)
    return unique


def _select_runtime(code_root: Path, *, full: bool = True) -> list[str]:
    _validate_environment_path(code_root)
    check = code_root / "scripts" / "check_runtime_contract.py"
    if not check.is_file():
        raise InstanceRegistryError(f"Runtime contract checker is missing: {check}")
    for command in _runtime_candidates(code_root):
        executable = command[0]
        if ("/" in executable or "\\" in executable) and not Path(executable).is_file():
            continue
        try:
            _validate_environment_path(Path(executable))
            args = [*command, str(check), "--code-root", str(code_root)]
            if not full:
                args.append("--runtime-only")
            checked = subprocess.run(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, InstanceRegistryError):
            continue
        if checked.returncode == 0:
            return command
    kind = "runtime and dependency lock" if full else "runtime"
    raise InstanceRegistryError(
        f"No approved CPython 3.12.13 {kind} is available for {code_root}."
    )


def _config_path(record: dict[str, Any], code_root: Path) -> Path:
    home = Path(record["bridge_home"])
    local = home / "agents.json"
    return local if local.is_file() else code_root / "agents.json"


def _is_provisioned(record: dict[str, Any], code_root: Path) -> bool:
    try:
        payload = json.loads(_config_path(record, code_root).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and bool(payload.get("agents"))


def _instance_paths(record: dict[str, Any]) -> tuple[Path, Path, Path]:
    home = Path(record["bridge_home"]).resolve()
    runtime_dir = home / "state" / "instance"
    return runtime_dir, runtime_dir / "process.lock", runtime_dir / "process.pid"


def _lock_is_held(path: Path) -> bool:
    if not path.is_file():
        return False
    handle = None
    try:
        handle = path.open("r+b")
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    except (OSError, IOError):
        return True
    finally:
        if handle is not None:
            handle.close()


def _read_pid(path: Path) -> int | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(value) if value.isdigit() and int(value) > 0 else None


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _http_json(
    method: str,
    port: int,
    path: str,
    *,
    token: str = "",
    body: dict[str, Any] | None = None,
    timeout: float = 3.0,
) -> tuple[int, dict[str, Any]]:
    connection = http.client.HTTPConnection("127.0.0.1", int(port), timeout=timeout)
    headers = {"Accept": "application/json"}
    payload = None
    if body is not None:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Workbench-Token"] = token
    try:
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise InstanceRegistryError("Local HASHI API response exceeded 1 MiB.")
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
        if not isinstance(parsed, dict):
            raise InstanceRegistryError("Local HASHI API returned non-object JSON.")
        return int(response.status), parsed
    finally:
        connection.close()


def _admin_token(record: dict[str, Any], code_root: Path) -> str:
    home = Path(record["bridge_home"])
    path = home / "secrets.json"
    if not path.is_file():
        path = code_root / "secrets.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    return str(payload.get("workbench_admin_token") or "") if isinstance(payload, dict) else ""


def inspect_instance(record: dict[str, Any], code_root: Path) -> dict[str, Any]:
    runtime_dir, lock_path, pid_path = _instance_paths(record)
    runtime_id, api_port, gateway_port = configured_identity(
        code_root, Path(record["bridge_home"])
    )
    lock_held = _lock_is_held(lock_path)
    health: dict[str, Any] | None = None
    health_error: str | None = None
    try:
        status, candidate = _http_json("GET", api_port, "/api/health")
        if status == 200 and candidate.get("ok") is True:
            health = candidate
        else:
            health_error = f"HTTP {status}"
    except Exception as exc:
        health_error = type(exc).__name__
    endpoint_matches = bool(
        health
        and str(health.get("instance_id") or "") == runtime_id
        and "workbench_port" in health
        and type(health.get("workbench_port")) is int
        and health.get("workbench_port") == api_port
    )
    agents: list[dict[str, Any]] = []
    agents_verified = False
    if endpoint_matches:
        try:
            status, payload = _http_json("GET", api_port, "/api/agents")
            raw_agents = payload.get("agents")
            if (
                status == 200
                and payload.get("ok") is True
                and isinstance(raw_agents, list)
            ):
                agents = [
                    dict(item)
                    for item in raw_agents
                    if isinstance(item, dict)
                ]
                agents_verified = all(
                    isinstance(item, dict)
                    and str(item.get("name") or item.get("id") or "").strip()
                    and isinstance(item.get("is_generating"), bool)
                    and type(item.get("queue_depth")) is int
                    and item.get("queue_depth") >= 0
                    and isinstance(item.get("active_transfer"), bool)
                    for item in raw_agents
                )
        except Exception:
            agents = []
    background_jobs: list[dict[str, Any]] = []
    background_jobs_verified = False
    if endpoint_matches:
        try:
            status, payload = _http_json(
                "GET",
                api_port,
                "/api/background-jobs?state=created,starting,running,cancel_requested&limit=200",
            )
            raw_jobs = payload.get("jobs")
            if (
                status == 200
                and payload.get("ok") is True
                and isinstance(raw_jobs, list)
            ):
                background_jobs = [
                    {
                        "job_id": str(item.get("job_id") or "unknown"),
                        "state": str(item.get("state") or "unknown"),
                        "agent": str(item.get("agent") or "unknown"),
                    }
                    for item in raw_jobs
                    if isinstance(item, dict)
                ]
                background_jobs_verified = all(
                    isinstance(item, dict)
                    and str(item.get("job_id") or "").strip()
                    and str(item.get("state") or "").strip()
                    for item in raw_jobs
                )
        except Exception:
            background_jobs = []
    busy_agents = [
        str(item.get("name") or item.get("id") or "unknown")
        for item in agents
        if item.get("is_generating") is True
        or _nonnegative_int(item.get("queue_depth")) > 0
        or item.get("active_transfer") is True
    ]
    agent_summaries = [
        {
            "name": str(item.get("name") or item.get("id") or "unknown"),
            "status": str(item.get("status") or "unknown"),
            "is_generating": bool(item.get("is_generating", False)),
            "queue_depth": _nonnegative_int(item.get("queue_depth")),
            "active_transfer": bool(item.get("active_transfer", False)),
            "worker_pid": _nonnegative_int(item.get("worker_pid")) or None,
            "generation_id": str(item.get("generation_id") or "") or None,
        }
        for item in agents
    ]
    busy_background_jobs = [item["job_id"] for item in background_jobs]
    activity_verified = agents_verified and background_jobs_verified
    return {
        "name": record["name"],
        "instance_id": runtime_id,
        "bridge_home": str(Path(record["bridge_home"]).resolve()),
        "code_root": str(code_root.resolve()),
        "api_port": api_port,
        "gateway_port": gateway_port,
        "runtime_dir": str(runtime_dir),
        "pid": _read_pid(pid_path),
        "lock_held": lock_held,
        "running": endpoint_matches and lock_held,
        "healthy": endpoint_matches,
        "foreign_endpoint": bool(health and not endpoint_matches),
        "health_error": health_error,
        "ready": health.get("ready") is True if endpoint_matches else False,
        "busy": bool(busy_agents or busy_background_jobs),
        "busy_agents": busy_agents,
        "busy_background_jobs": busy_background_jobs,
        "activity_verified": activity_verified,
        "agents": agent_summaries,
        "background_jobs": background_jobs,
    }


def _launch_environment(record: dict[str, Any], code_root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "BRIDGE_HOME": str(Path(record["bridge_home"]).resolve()),
            "BRIDGE_CODE_ROOT": str(code_root.resolve()),
            "PYTHONUNBUFFERED": "1",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return environment


def run_onboarding(record: dict[str, Any], code_root: Path) -> int:
    runtime = _select_runtime(code_root, full=True)
    environment = _launch_environment(record, code_root)
    environment["HASHI_ONBOARD_NO_LAUNCH"] = "1"
    return subprocess.run(
        [*runtime, "-m", "onboarding.onboarding_main"],
        cwd=code_root,
        env=environment,
        check=False,
    ).returncode


def start_instance(
    registry: InstanceRegistry,
    record: dict[str, Any],
    *,
    quiet: bool = False,
) -> int:
    code_root = registry.resolved_code_root(record)
    snapshot = inspect_instance(record, code_root)
    if snapshot["running"]:
        if not quiet:
            print(f"HASHI instance {record['name']} is already running (PID {snapshot['pid']}).")
        return 0
    if (
        snapshot["lock_held"]
        or snapshot.get("healthy")
        or snapshot["foreign_endpoint"]
    ):
        print(
            "Cannot start: the instance lock or configured API port belongs to an "
            "unverified process.",
            file=sys.stderr,
        )
        return EXIT_BUSY
    if record.get("managed") and record.get("adopted_program_version") != registry.program_version:
        print(
            f"Program {registry.program_version} is installed but instance {record['name']} "
            "has not adopted it. Run `hashi instance adopt "
            f"{record['name']}` while the instance is stopped.",
            file=sys.stderr,
        )
        return EXIT_NOT_READY
    if not _is_provisioned(record, code_root):
        if not sys.stdin.isatty():
            print(
                f"Instance {record['name']} needs onboarding; run an interactive "
                f"`hashi --instance {record['name']} onboard`.",
                file=sys.stderr,
            )
            return EXIT_NOT_READY
        result = run_onboarding(record, code_root)
        if result != 0 or not _is_provisioned(record, code_root):
            return result or EXIT_NOT_READY

    try:
        runtime = _select_runtime(code_root, full=True)
    except InstanceRegistryError as exc:
        print(f"HASHI runtime is incomplete: {exc}", file=sys.stderr)
        return EXIT_RUNTIME
    home = Path(record["bridge_home"]).resolve()
    log_path = home / "logs" / "hashi-cli.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("ab", buffering=0)
    command = [*runtime, str(code_root / "main.py"), "--bridge-home", str(home)]
    options: dict[str, Any] = {
        "cwd": code_root,
        "env": _launch_environment(record, code_root),
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
    }
    if sys.platform == "win32":
        options["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        options["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **options)
    except OSError as exc:
        log_handle.close()
        print(f"Failed to start HASHI: {exc}", file=sys.stderr)
        return EXIT_NOT_READY
    finally:
        log_handle.close()

    try:
        timeout = float(
            os.environ.get("HASHI_CLI_START_TIMEOUT")
            or DEFAULT_START_TIMEOUT_SECONDS
        )
    except ValueError:
        timeout = DEFAULT_START_TIMEOUT_SECONDS
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        snapshot = inspect_instance(record, code_root)
        if snapshot["running"] and snapshot["ready"]:
            if not quiet:
                print(
                    f"Started HASHI instance {record['name']} (PID {snapshot['pid']}, "
                    f"API 127.0.0.1:{snapshot['api_port']})."
                )
            return 0
        if process.poll() is not None:
            print(
                f"HASHI exited during startup with code {process.returncode}; see {log_path}.",
                file=sys.stderr,
            )
            return EXIT_NOT_READY
        time.sleep(0.25)
    if process.poll() is None:
        process.terminate()
    print(f"HASHI did not become ready; see {log_path}.", file=sys.stderr)
    return EXIT_NOT_READY


def stop_instance(registry: InstanceRegistry, record: dict[str, Any]) -> int:
    code_root = registry.resolved_code_root(record)
    snapshot = inspect_instance(record, code_root)
    if not snapshot["lock_held"] and not snapshot["healthy"]:
        print(f"HASHI instance {record['name']} is already stopped.")
        return 0
    if not snapshot["running"]:
        print(
            "Refusing to stop: the process is locked but its identity and idle state "
            "cannot be verified through the local API.",
            file=sys.stderr,
        )
        return EXIT_BUSY
    if not snapshot["activity_verified"]:
        print(
            "Refusing to stop: live work/queue state could not be verified; no "
            "shutdown or force kill was attempted.",
            file=sys.stderr,
        )
        return EXIT_BUSY
    if snapshot["busy"]:
        reasons = [
            *(f"agent:{name}" for name in snapshot["busy_agents"]),
            *(
                f"background-job:{job_id}"
                for job_id in snapshot.get("busy_background_jobs", [])
            ),
        ]
        print(
            f"Instance {record['name']} is busy ({', '.join(reasons)}); "
            "stop was not sent and no "
            "work was cancelled.",
            file=sys.stderr,
        )
        return EXIT_BUSY
    token = _admin_token(record, code_root)
    try:
        status, payload = _http_json(
            "POST",
            snapshot["api_port"],
            "/api/admin/shutdown",
            token=token,
            body={"reason": "hashi-cli"},
            timeout=5.0,
        )
    except Exception as exc:
        print(f"Graceful stop was not accepted: {type(exc).__name__}.", file=sys.stderr)
        return EXIT_BUSY
    if status != 200 or payload.get("ok") is not True:
        print(
            f"Graceful stop was rejected (HTTP {status}); no force kill was attempted.",
            file=sys.stderr,
        )
        return EXIT_BUSY
    lock_path = Path(_instance_paths(record)[1])
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if not _lock_is_held(lock_path):
            print(f"Stopped HASHI instance {record['name']} gracefully.")
            return 0
        time.sleep(0.25)
    print(
        "Shutdown was accepted but the instance is still running; no force kill was attempted.",
        file=sys.stderr,
    )
    return EXIT_BUSY


def _selected_record(
    registry: InstanceRegistry,
    args: argparse.Namespace,
    *,
    invocation_cwd: Path,
) -> tuple[dict[str, Any], str]:
    return registry.select(
        explicit=args.instance,
        cwd=invocation_cwd,
        interactive=sys.stdin.isatty(),
    )


def _status_output(
    registry: InstanceRegistry,
    record: dict[str, Any],
    source: str,
    *,
    as_json: bool,
) -> int:
    code_root = registry.resolved_code_root(record)
    snapshot = inspect_instance(record, code_root)
    snapshot["selection_source"] = source
    snapshot["managed"] = bool(record.get("managed"))
    snapshot["adopted_program_version"] = record.get("adopted_program_version")
    snapshot["installed_program_version"] = registry.program_version
    snapshot["update_pending"] = bool(
        record.get("managed")
        and record.get("adopted_program_version") != registry.program_version
    )
    if as_json:
        _emit(snapshot, as_json=True)
    else:
        if snapshot["running"]:
            state = "running/ready" if snapshot["ready"] else "running/not-ready"
        elif snapshot["lock_held"]:
            state = "unverified/locked"
        elif snapshot["healthy"]:
            state = "unverified/api-without-lock"
        else:
            state = "stopped"
        print(f"Instance: {record['name']} ({snapshot['instance_id']})")
        print(f"Selected by: {source}")
        print(f"State: {state}")
        print(f"PID: {snapshot['pid'] or '-'}")
        print(f"Backend API: 127.0.0.1:{snapshot['api_port']}")
        print(f"Busy: {'yes' if snapshot['busy'] else 'no'}")
        if snapshot["running"] and not snapshot["activity_verified"]:
            print("Busy state: unverified (stop will fail closed)")
        if snapshot["update_pending"]:
            print(
                "Program update pending explicit adoption: "
                f"{record.get('adopted_program_version')} -> {registry.program_version}"
            )
    return 0


def _run_tui(registry: InstanceRegistry, record: dict[str, Any]) -> int:
    started = start_instance(registry, record, quiet=True)
    if started != 0:
        return started
    code_root = registry.resolved_code_root(record)
    runtime = _select_runtime(code_root, full=True)
    return subprocess.run(
        [*runtime, str(code_root / "tui.py")],
        cwd=code_root,
        env=_launch_environment(record, code_root),
        check=False,
    ).returncode


def _run_external_ui(registry: InstanceRegistry, record: dict[str, Any]) -> int:
    executable = str(os.environ.get("HASHI_UI_EXECUTABLE") or "").strip()
    selected = executable if executable and Path(executable).is_file() else shutil.which("hashi-ui")
    if not selected:
        print(
            "No external HASHI UI is installed. The retired Workbench is not bundled; "
            "install a compatible `hashi-ui` program first.",
            file=sys.stderr,
        )
        return EXIT_NOT_READY
    _validate_environment_path(Path(selected))
    started = start_instance(registry, record, quiet=True)
    if started != 0:
        return started
    code_root = registry.resolved_code_root(record)
    _, api_port, _ = configured_identity(code_root, Path(record["bridge_home"]))
    environment = _launch_environment(record, code_root)
    environment["HASHI_API_URL"] = f"http://127.0.0.1:{api_port}"
    return subprocess.run([selected], env=environment, check=False).returncode


def _handle_instance_command(
    registry: InstanceRegistry,
    args: argparse.Namespace,
    *,
    invocation_cwd: Path,
) -> int:
    command = args.instance_command
    if command == "create":
        record = registry.create(
            args.name,
            source_root=args.source_root,
            bridge_home=args.home,
            bind_path=args.bind,
            make_default=args.default,
        )
        _emit(record, as_json=args.json)
        return 0
    if command == "list":
        records = registry.records()
        if args.json:
            _emit({"instances": records}, as_json=True)
        elif not records:
            print("No HASHI instances are registered.")
        else:
            for record in records:
                flags = []
                if record["default"]:
                    flags.append("default")
                flags.append("managed" if record["managed"] else record["source_kind"])
                if record["update_pending"]:
                    flags.append("update-pending")
                print(f"{record['name']}\t{record['instance_id']}\t{','.join(flags)}")
        return 0
    if command == "default":
        record = registry.set_default(args.name)
        print(f"Default HASHI instance is now {record['name']}.")
        return 0
    if command == "bind":
        record = registry.bind(args.name, args.path or invocation_cwd)
        print(f"Bound {Path(args.path or invocation_cwd).resolve()} to {record['name']}.")
        return 0
    if command == "restore":
        record = registry.restore(args.name)
        print(f"Restored HASHI instance {record['name']} and its retained data.")
        return 0
    if command in {"remove", "adopt"}:
        record = registry.get(args.name)
        code_root = registry.resolved_code_root(record)
        snapshot = inspect_instance(record, code_root)
        if snapshot["lock_held"] or snapshot["healthy"]:
            print(
                f"Instance {record['name']} must be stopped before {command}.",
                file=sys.stderr,
            )
            return EXIT_BUSY
        if command == "adopt":
            adopted = registry.adopt(args.name)
            print(
                f"Instance {adopted['name']} adopted program "
                f"{adopted['adopted_program_version']}."
            )
            return 0
        confirmation = args.confirm
        if args.purge and not confirmation and sys.stdin.isatty():
            confirmation = input(
                f"Type the exact instance name {record['name']!r} to permanently purge: "
            )
        removed = registry.remove(
            args.name,
            purge=args.purge,
            confirmation=confirmation,
        )
        if removed.get("purged_at"):
            print(f"Permanently purged managed instance {record['name']}.")
        else:
            print(
                f"Removed instance {record['name']} recoverably "
                f"(recovery id {removed['removal_id']})."
            )
        return 0
    raise InstanceRegistryError(f"Unsupported instance command: {command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "help":
        parser.print_help()
        return 0
    program_root = Path(
        os.environ.get("HASHI_PROGRAM_ROOT") or CODE_ROOT
    ).expanduser().resolve()
    program_version = str(
        os.environ.get("HASHI_PROGRAM_VERSION") or "development"
    ).strip()
    invocation_cwd = Path(
        os.environ.get("HASHI_INVOCATION_CWD") or Path.cwd()
    ).expanduser().resolve()
    registry = InstanceRegistry(
        program_root=program_root,
        program_version=program_version,
    )
    try:
        if args.command == "instance":
            return _handle_instance_command(
                registry, args, invocation_cwd=invocation_cwd
            )

        records = registry.records()
        if not records:
            if args.command == "status" and not args.instance:
                _emit({"instances": []}, as_json=args.json)
                return 0
            can_bootstrap = args.command in {None, "start", "tui", "onboard"}
            if not can_bootstrap or not sys.stdin.isatty():
                raise InstanceSelectionError(
                    "No HASHI instance exists. Run interactive `hashi` or "
                    "`hashi instance create <name>`."
                )
            registry.create("default", make_default=True, bind_path=invocation_cwd)
        record, source = _selected_record(
            registry, args, invocation_cwd=invocation_cwd
        )
        command = args.command or "tui"
        if command == "status":
            return _status_output(
                registry, record, source, as_json=args.json
            )
        if command == "stop":
            return stop_instance(registry, record)
        if command == "onboard":
            if record.get("managed") and record.get(
                "adopted_program_version"
            ) != registry.program_version:
                raise InstanceRegistryError(
                    f"Instance {record['name']} must explicitly adopt program "
                    f"{registry.program_version} before onboarding."
                )
            return run_onboarding(record, registry.resolved_code_root(record))
        if command == "start":
            return start_instance(registry, record)
        if command == "tui":
            return _run_tui(registry, record)
        if command == "ui":
            return _run_external_ui(registry, record)
        parser.error(f"unsupported command: {command}")
    except (InstanceRegistryError, InstanceSelectionError) as exc:
        print(f"HASHI: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
