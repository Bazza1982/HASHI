"""Read-only inspection and authoritative-workspace verification commands."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tools.builtins import BuiltinExecutionResult

_IGNORED_COPY_DIRS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        ".aws",
        ".gnupg",
        ".ssh",
        "__pycache__",
        "backend_state",
        "logs",
        "media",
        "node_modules",
        "private",
        "state",
        "tmp",
        "venv",
        "workspaces",
    }
)
_MAX_OUTPUT_CHARS = 80_000
_MAX_ARTIFACT_HASH_BYTES = 512 * 1024 * 1024
_HASHI_VERIFICATION_POLICY_ARGUMENT = "_hashi_verification_policy"
_DEFAULT_DIRECT_TIMEOUT_S = 1800.0
_DEFAULT_MINIMUM_TIMEOUT_S = 300.0
_DEFAULT_EXECUTION_TIMEOUT_MULTIPLIER = 1.5
_DEFAULT_TIMEOUT_GRACE_S = 300.0
_MIN_EXECUTION_TIMEOUT_MULTIPLIER = 1.0
_MIN_TIMEOUT_GRACE_S = 60.0


def _result(output: str, **details: Any) -> BuiltinExecutionResult:
    return BuiltinExecutionResult(str(output), details)


def _workspace_path(workspace_dir: Path, raw_path: Any = ".") -> Path:
    root = Path(workspace_dir).resolve()
    candidate = Path(str(raw_path or "."))
    candidate = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"inspection path {candidate} is outside review workspace {root}"
        ) from exc
    return candidate


def _run_read_only(argv: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(item) for item in argv],
        cwd=str(cwd),
        capture_output=True,
        check=False,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_snapshot(root: Path) -> tuple[str, dict[str, Any]] | None:
    top = _run_read_only(["git", "rev-parse", "--show-toplevel"], cwd=root)
    if top.returncode != 0:
        return None
    git_root = Path(top.stdout.decode("utf-8", errors="replace").strip()).resolve()
    try:
        relative_root = root.relative_to(git_root)
    except ValueError:
        return None

    pathspec = ["--", str(relative_root)] if str(relative_root) != "." else []
    head = _run_read_only(["git", "rev-parse", "HEAD"], cwd=git_root)
    status = _run_read_only(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all", *pathspec],
        cwd=git_root,
    )
    unstaged = _run_read_only(
        ["git", "diff", "--no-ext-diff", "--binary", *pathspec], cwd=git_root
    )
    staged = _run_read_only(
        ["git", "diff", "--cached", "--no-ext-diff", "--binary", *pathspec],
        cwd=git_root,
    )
    untracked = _run_read_only(
        ["git", "ls-files", "--others", "--exclude-standard", "-z", *pathspec],
        cwd=git_root,
    )
    commands = (head, status, unstaged, staged, untracked)
    if any(item.returncode != 0 for item in commands):
        return None

    digest = hashlib.sha256()
    for label, payload in (
        (b"head", head.stdout),
        (b"status", status.stdout),
        (b"unstaged", unstaged.stdout),
        (b"staged", staged.stdout),
    ):
        digest.update(label + b"\0" + payload + b"\0")
    untracked_count = 0
    for raw_name in untracked.stdout.split(b"\0"):
        if not raw_name:
            continue
        untracked_count += 1
        relative = Path(raw_name.decode("utf-8", errors="surrogateescape"))
        path = (git_root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            continue
        digest.update(b"untracked\0" + raw_name + b"\0")
        if path.is_file():
            digest.update(_sha256_file(path).encode("ascii"))
    return digest.hexdigest(), {
        "vcs": "git",
        "head": head.stdout.decode("utf-8", errors="replace").strip(),
        "dirty": bool(status.stdout),
        "untracked_files": untracked_count,
    }


def _filesystem_snapshot(root: Path) -> tuple[str, dict[str, Any]]:
    digest = hashlib.sha256()
    file_count = 0
    byte_count = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in _IGNORED_COPY_DIRS)
        current_path = Path(current)
        for name in sorted(files):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                digest.update(f"link\0{relative}\0{os.readlink(path)}\0".encode())
                continue
            if not path.is_file():
                continue
            stat = path.stat()
            file_count += 1
            byte_count += stat.st_size
            if file_count > 50_000 or byte_count > _MAX_ARTIFACT_HASH_BYTES:
                raise ValueError("workspace is too large for a bounded review snapshot")
            digest.update(f"file\0{relative}\0{stat.st_size}\0".encode())
            digest.update(_sha256_file(path).encode("ascii"))
    return digest.hexdigest(), {
        "vcs": "filesystem",
        "dirty": None,
        "file_count": file_count,
        "bytes_hashed": byte_count,
    }


def _workspace_snapshot(root: Path) -> tuple[str, dict[str, Any]]:
    git_value = _git_snapshot(root)
    if git_value is not None:
        return git_value
    return _filesystem_snapshot(root)


def _bounded_text(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    if len(text) > _MAX_OUTPUT_CHARS:
        return text[:_MAX_OUTPUT_CHARS] + "\n...[truncated]"
    return text


async def execute_workspace_inspect(
    arguments: Mapping[str, Any],
    *,
    workspace_dir: Path,
) -> BuiltinExecutionResult:
    """Perform one fixed read-only inspection operation inside the workzone."""

    operation = str(arguments.get("operation") or "").strip().casefold()
    root = Path(workspace_dir).resolve()
    if operation == "snapshot":
        try:
            digest, metadata = await asyncio.to_thread(_workspace_snapshot, root)
        except Exception as exc:
            return _result(
                f"Error: workspace snapshot unavailable: {exc}",
                operation=operation,
                unavailable=True,
            )
        payload = {"snapshot_sha256": digest, **metadata}
        return _result(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            operation=operation,
            snapshot_sha256=digest,
            **metadata,
        )

    try:
        target = _workspace_path(root, arguments.get("path", "."))
    except ValueError as exc:
        return _result(f"Error: {exc}", operation=operation)

    if operation == "status":
        completed = await asyncio.to_thread(
            _run_read_only,
            ["git", "status", "--short", "--branch", "--", str(target)],
            cwd=root,
        )
        if completed.returncode != 0:
            return _result(
                "Error: workspace status is unavailable: " + _bounded_text(completed.stderr),
                operation=operation,
                exit_code=completed.returncode,
            )
        return _result(
            _bounded_text(completed.stdout) or "Workspace status is clean.",
            operation=operation,
            exit_code=0,
        )

    if operation == "diff":
        cached = arguments.get("cached", False)
        if not isinstance(cached, bool):
            return _result("Error: cached must be a boolean", operation=operation)
        argv = ["git", "diff", "--no-ext-diff", "--stat", "--patch"]
        if cached:
            argv.append("--cached")
        argv.extend(["--", str(target)])
        completed = await asyncio.to_thread(_run_read_only, argv, cwd=root)
        if completed.returncode != 0:
            return _result(
                "Error: workspace diff is unavailable: " + _bounded_text(completed.stderr),
                operation=operation,
                exit_code=completed.returncode,
            )
        return _result(
            _bounded_text(completed.stdout) or "No diff.",
            operation=operation,
            exit_code=0,
            cached=cached,
        )

    if operation == "search":
        query = str(arguments.get("query") or "")
        if not query:
            return _result("Error: search requires query", operation=operation)
        regex = arguments.get("regex", False)
        if not isinstance(regex, bool):
            return _result("Error: regex must be a boolean", operation=operation)
        search_backend = "rg"
        executable = shutil.which("rg")
        if executable:
            argv = [
                executable,
                "--line-number",
                "--no-heading",
                "--color",
                "never",
            ]
            if not regex:
                argv.append("--fixed-strings")
        else:
            search_backend = "grep"
            executable = shutil.which("grep")
            if not executable:
                return _result(
                    "Error: workspace search is unavailable: neither rg nor grep "
                    "is installed",
                    operation=operation,
                    unavailable=True,
                )
            argv = [
                executable,
                "--recursive",
                "--line-number",
                "--binary-files=without-match",
                "--extended-regexp" if regex else "--fixed-strings",
            ]
        argv.extend(["--", query, str(target)])
        completed = await asyncio.to_thread(_run_read_only, argv, cwd=root)
        if completed.returncode not in {0, 1}:
            return _result(
                "Error: workspace search failed: " + _bounded_text(completed.stderr),
                operation=operation,
                exit_code=completed.returncode,
            )
        output = _bounded_text(completed.stdout)
        return _result(
            output or "No matches.",
            operation=operation,
            exit_code=completed.returncode,
            matches=0 if completed.returncode == 1 else len(output.splitlines()),
            search_backend=search_backend,
        )

    if operation in {"hash", "artifact"}:
        if not target.exists():
            return _result(f"Error: path not found: {target}", operation=operation)
        if target.is_file():
            digest = await asyncio.to_thread(_sha256_file, target)
            stat = target.stat()
            payload = {
                "path": str(target.relative_to(root)),
                "kind": "file",
                "size": stat.st_size,
                "sha256": digest,
            }
        elif target.is_dir():
            digest, metadata = await asyncio.to_thread(_filesystem_snapshot, target)
            payload = {
                "path": str(target.relative_to(root)),
                "kind": "directory",
                "sha256": digest,
                **metadata,
            }
        else:
            return _result(f"Error: unsupported artifact type: {target}", operation=operation)
        return _result(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            operation=operation,
            artifact_sha256=digest,
            path=payload["path"],
        )

    return _result(
        "Error: operation must be snapshot, status, diff, search, hash, or artifact",
        operation=operation,
    )


def _recipe_catalog(options: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    # Keep the virtual-environment entry point. Resolving the symlink would
    # silently bypass the environment's installed packages inside bwrap.
    python = str(Path(sys.executable))
    recipes: dict[str, dict[str, Any]] = {
        "pytest_core": {
            "description": "Curated deterministic pytest core gate.",
            "argv": [python, "-m", "pytest", "-q"],
            "timeout_s": 1800.0,
        },
        "pytest_offline": {
            "description": "All offline pytest inventory excluding contract, live, and platform tests.",
            "argv": [
                python,
                "-m",
                "pytest",
                "-q",
                "tests",
                "-m",
                "not contract and not live and not platform",
            ],
            "timeout_s": 3600.0,
        },
        "python_compile": {
            "description": "Compile all Python sources in the current workspace.",
            "argv": [python, "-m", "compileall", "-q", "."],
            "timeout_s": 900.0,
        },
    }
    raw_recipes = (options or {}).get("recipes")
    if not isinstance(raw_recipes, Mapping):
        return recipes
    for raw_name, raw in raw_recipes.items():
        name = str(raw_name or "").strip()
        if not name or not isinstance(raw, Mapping):
            continue
        argv = raw.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(item, str) or not item for item in argv)
        ):
            continue
        try:
            timeout_s = float(raw.get("timeout_s", 1800.0))
        except (TypeError, ValueError):
            continue
        if timeout_s <= 0:
            continue
        recipes[name] = {
            "description": str(raw.get("description") or "Configured verification recipe."),
            "argv": [python if item == "{python}" else item for item in argv],
            "timeout_s": timeout_s,
        }
    return recipes


def _finite_number(value: Any, *, default: float, minimum: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number) or number < minimum:
        return default
    return number


def _requested_timeout(arguments: Mapping[str, Any]) -> tuple[float | None, str | None]:
    if "timeout_s" not in arguments or arguments.get("timeout_s") is None:
        return None, None
    value = arguments.get("timeout_s")
    if isinstance(value, bool):
        return None, "timeout_s must be a positive number"
    try:
        timeout_s = float(value)
    except (TypeError, ValueError):
        return None, "timeout_s must be a positive number"
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        return None, "timeout_s must be a positive number"
    return timeout_s, None


def _timeout_policy(
    *,
    configured_timeout_s: float,
    requested_timeout_s: float | None,
    arguments: Mapping[str, Any],
    options: Mapping[str, Any] | None,
) -> dict[str, Any]:
    configured = dict(options or {})
    runtime_policy = arguments.get(_HASHI_VERIFICATION_POLICY_ARGUMENT)
    runtime_policy = dict(runtime_policy) if isinstance(runtime_policy, Mapping) else {}
    execution_elapsed_s = _finite_number(
        runtime_policy.get("execution_elapsed_s"), default=0.0, minimum=0.0
    )
    minimum_timeout_s = max(
        _DEFAULT_MINIMUM_TIMEOUT_S,
        _finite_number(
            configured.get("minimum_timeout_s"),
            default=_DEFAULT_MINIMUM_TIMEOUT_S,
            minimum=0.0,
        ),
    )
    multiplier = max(
        _MIN_EXECUTION_TIMEOUT_MULTIPLIER,
        _finite_number(
            configured.get("execution_timeout_multiplier"),
            default=_DEFAULT_EXECUTION_TIMEOUT_MULTIPLIER,
            minimum=0.0,
        ),
    )
    grace_s = max(
        _MIN_TIMEOUT_GRACE_S,
        _finite_number(
            configured.get("timeout_grace_s"),
            default=_DEFAULT_TIMEOUT_GRACE_S,
            minimum=0.0,
        ),
    )
    execution_floor_s = execution_elapsed_s * multiplier + grace_s
    candidates = [configured_timeout_s, minimum_timeout_s, execution_floor_s]
    if requested_timeout_s is not None:
        candidates.append(requested_timeout_s)
    effective_timeout_s = max(candidates)
    return {
        "configured_timeout_s": configured_timeout_s,
        "requested_timeout_s": requested_timeout_s,
        "execution_elapsed_s": execution_elapsed_s,
        "execution_timeout_multiplier": multiplier,
        "timeout_grace_s": grace_s,
        "minimum_timeout_s": minimum_timeout_s,
        "execution_floor_s": execution_floor_s,
        "effective_timeout_s": effective_timeout_s,
        "formula": (
            "max(configured, requested, minimum, "
            "execution_elapsed*multiplier+grace)"
        ),
    }


def _direct_argv(value: Any) -> list[str] | None:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
    ):
        return None
    python = str(Path(sys.executable))
    return [python if item == "{python}" else item for item in value]


def _workspace_authority(root: Path) -> dict[str, Any]:
    return {
        "process_authority": "inherited",
        "identity_policy": "inherited",
        "filesystem_policy": "inherited",
        "environment_policy": "inherited",
        "network_policy": "inherited",
        "home_policy": "inherited",
        "workspace_access": {
            "read": os.access(root, os.R_OK),
            "write": os.access(root, os.W_OK),
            "execute": os.access(root, os.X_OK),
        },
    }


async def _run_workspace_command(
    command: Sequence[str], *, cwd: Path, timeout_s: float
) -> tuple[int, bytes, bytes, bool, dict[str, Any]]:
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    communicate_task = asyncio.create_task(proc.communicate())
    try:
        done, _pending = await asyncio.wait({communicate_task}, timeout=timeout_s)
        if communicate_task in done:
            stdout, stderr = await communicate_task
            return (
                int(proc.returncode or 0),
                stdout,
                stderr,
                False,
                {
                    "status": "normal_completion",
                    "scope": "process_group" if os.name == "posix" else "process",
                    "forced": False,
                    "process_reaped": proc.returncode is not None,
                },
            )

        cleanup = await _stop_workspace_process(proc, communicate_task)
        stdout, stderr = cleanup.pop("stdout"), cleanup.pop("stderr")
        return int(proc.returncode or 0), stdout, stderr, True, cleanup
    except asyncio.CancelledError as exc:
        cleanup = await _stop_workspace_process(proc, communicate_task)
        cleanup.pop("stdout", None)
        cleanup.pop("stderr", None)
        setattr(exc, "hashi_tool_details", {"foreground_cleanup": cleanup})
        raise


async def _stop_workspace_process(
    proc: asyncio.subprocess.Process,
    communicate_task: asyncio.Task,
) -> dict[str, Any]:
    """Terminate and reap the whole validation process group."""

    errors: list[str] = []
    forced = False
    scope = "process_group" if os.name == "posix" else "process"
    if proc.returncode is None:
        try:
            if os.name == "posix" and proc.pid:
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
        except ProcessLookupError:
            pass
        except Exception as exc:
            errors.append(f"terminate:{type(exc).__name__}:{exc}")
    done, _pending = await asyncio.wait({communicate_task}, timeout=5.0)
    if communicate_task not in done:
        forced = True
        try:
            if os.name == "posix" and proc.pid:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        except Exception as exc:
            errors.append(f"kill:{type(exc).__name__}:{exc}")
    try:
        stdout, stderr = await communicate_task
    except Exception as exc:
        errors.append(f"communicate:{type(exc).__name__}:{exc}")
        stdout, stderr = b"", b""
    return {
        "status": "forced" if forced else "terminated",
        "scope": scope,
        "forced": forced,
        "process_reaped": proc.returncode is not None,
        "errors": errors,
        "stdout": stdout,
        "stderr": stderr,
    }


async def execute_verification_run(
    arguments: Mapping[str, Any],
    *,
    workspace_dir: Path,
    options: Mapping[str, Any] | None = None,
) -> BuiltinExecutionResult:
    """List recipes or run a direct argv validation in the real workspace."""

    operation = str(arguments.get("operation") or "").strip().casefold()
    recipes = _recipe_catalog(options)
    source = Path(workspace_dir).resolve()
    runtime_policy = arguments.get(_HASHI_VERIFICATION_POLICY_ARGUMENT)
    policy_arguments = {
        _HASHI_VERIFICATION_POLICY_ARGUMENT: (
            dict(runtime_policy) if isinstance(runtime_policy, Mapping) else {}
        )
    }
    if operation == "list":
        visible_recipes = {
            name: {
                "description": item["description"],
                "configured_timeout_s": item["timeout_s"],
                "effective_timeout_s": _timeout_policy(
                    configured_timeout_s=float(item["timeout_s"]),
                    requested_timeout_s=None,
                    arguments=policy_arguments,
                    options=options,
                )["effective_timeout_s"],
            }
            for name, item in sorted(recipes.items())
        }
        direct_policy = _timeout_policy(
            configured_timeout_s=_finite_number(
                (options or {}).get("direct_timeout_s"),
                default=_DEFAULT_DIRECT_TIMEOUT_S,
                minimum=0.000001,
            ),
            requested_timeout_s=None,
            arguments=policy_arguments,
            options=options,
        )
        visible = {
            "execution_scope": "authoritative_current_workspace",
            "workspace_copied": False,
            "direct_argv_supported": True,
            "shell": False,
            "authority": _workspace_authority(source),
            "timeout_policy": direct_policy,
            "recipes": visible_recipes,
        }
        return _result(
            json.dumps(visible, ensure_ascii=False, sort_keys=True, indent=2),
            operation="list",
            recipe_count=len(visible_recipes),
            execution_scope="workspace",
            workspace_copied=False,
            direct_argv_supported=True,
            effective_timeout_s=direct_policy["effective_timeout_s"],
        )
    if operation != "run":
        return _result("Error: operation must be list or run", operation=operation)
    if "command" in arguments:
        return _result(
            "Error: verification_run does not accept implicit-shell command text; "
            "use argv or a configured recipe",
            operation="run",
        )

    recipe_name = str(arguments.get("recipe") or "").strip()
    raw_argv = arguments.get("argv")
    if recipe_name and raw_argv is not None:
        return _result(
            "Error: verification_run accepts either recipe or argv, not both",
            operation="run",
            recipe=recipe_name,
        )
    recipe = recipes.get(recipe_name) if recipe_name else None
    argv_registered = recipe is not None
    if recipe_name and recipe is None:
        return _result(
            f"Error: unknown verification recipe {recipe_name!r}; call list first",
            operation="run",
            recipe=recipe_name,
            unavailable=True,
        )
    if recipe is not None:
        command = list(recipe["argv"])
        configured_timeout_s = float(recipe["timeout_s"])
        command_ref = recipe_name
    else:
        command = _direct_argv(raw_argv)
        if command is None:
            return _result(
                "Error: verification_run operation=run requires a registered recipe "
                "or a non-empty argv string array",
                operation="run",
                unavailable=True,
            )
        configured_timeout_s = _finite_number(
            (options or {}).get("direct_timeout_s"),
            default=_DEFAULT_DIRECT_TIMEOUT_S,
            minimum=0.000001,
        )
        command_ref = "direct_argv"

    requested_timeout_s, timeout_error = _requested_timeout(arguments)
    if timeout_error is not None:
        return _result(
            f"Error: verification {timeout_error}",
            operation="run",
            recipe=recipe_name or None,
        )
    timeout_policy = _timeout_policy(
        configured_timeout_s=configured_timeout_s,
        requested_timeout_s=requested_timeout_s,
        arguments=arguments,
        options=options,
    )
    if not source.is_dir():
        return _result(
            f"Error: verification workspace is unavailable: {source}",
            operation="run",
            recipe=recipe_name,
            unavailable=True,
        )

    started_at = time.monotonic()
    try:
        exit_code, stdout, stderr, timed_out, cleanup = await _run_workspace_command(
            command,
            cwd=source,
            timeout_s=float(timeout_policy["effective_timeout_s"]),
        )
    except Exception as exc:
        return _result(
            f"Error: verification command could not start: {exc}",
            operation="run",
            recipe=recipe_name or None,
            unavailable=True,
            execution_scope="workspace",
            workspace_copied=False,
            argv_registered=argv_registered,
            timeout_policy=timeout_policy,
        )

    combined = _bounded_text(stdout + (b"\n" if stdout and stderr else b"") + stderr)
    final_output = combined or f"Verification {command_ref} exited with code {exit_code}."
    if timed_out:
        final_output = (
            f"Error: verification {command_ref} timed out after "
            f"{timeout_policy['effective_timeout_s']:g} seconds\n{final_output}"
        )
    elif exit_code != 0:
        final_output = (
            f"Error: verification {command_ref} failed with exit code "
            f"{exit_code}\n{final_output}"
        )
    authority = _workspace_authority(source)
    return _result(
        final_output,
        operation="run",
        recipe=recipe_name or None,
        command_source="registered_recipe" if argv_registered else "direct_argv",
        exit_code=exit_code,
        timed_out=timed_out,
        elapsed_s=round(max(0.0, time.monotonic() - started_at), 6),
        timeout_s=timeout_policy["effective_timeout_s"],
        timeout_policy=timeout_policy,
        execution_scope="workspace",
        workspace_root=str(source),
        workspace_copied=False,
        argv_registered=argv_registered,
        argv_sha256=hashlib.sha256(
            json.dumps(command, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
        shell=False,
        process_isolated=False,
        foreground_cleanup=cleanup,
        **authority,
    )
