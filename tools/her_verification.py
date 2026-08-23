"""Read-only inspection and isolated test recipes for HER review stages."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from functools import lru_cache
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
_CREDENTIAL_FILE_NAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pypirc",
        "auth.json",
        "credentials.json",
        "secrets.json",
        "tokens.json",
    }
)
_PRIVATE_KEY_SUFFIXES = frozenset({".key", ".p12", ".pfx", ".pem"})
_PUBLIC_ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})
_MAX_OUTPUT_CHARS = 80_000
_DEFAULT_COPY_LIMIT_BYTES = 512 * 1024 * 1024


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
            if file_count > 50_000 or byte_count > _DEFAULT_COPY_LIMIT_BYTES:
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
            "description": "Compile all Python sources in the isolated copy.",
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


@lru_cache(maxsize=1)
def _bubblewrap_available() -> bool:
    executable = shutil.which("bwrap")
    if not executable:
        return False
    probe = subprocess.run(
        [
            executable,
            "--die-with-parent",
            "--unshare-net",
            "--ro-bind",
            "/usr",
            "/usr",
            "--symlink",
            "usr/bin",
            "/bin",
            "--symlink",
            "usr/lib",
            "/lib",
            "--symlink",
            "usr/lib64",
            "/lib64",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--",
            "/usr/bin/true",
        ],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


def _copy_size(root: Path, *, limit: int) -> int:
    total = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in _IGNORED_COPY_DIRS]
        for name in files:
            path = Path(current) / name
            if _is_credential_path(path.relative_to(root)):
                continue
            if path.is_symlink():
                continue
            try:
                total += path.stat().st_size
            except OSError:
                continue
            if total > limit:
                raise ValueError(
                    f"workspace copy exceeds configured isolation limit of {limit} bytes"
                )
    return total


def _is_credential_path(relative: Path) -> bool:
    """Reject common workspace-local credential material from the test copy."""

    parts = tuple(part.casefold() for part in relative.parts)
    if any(part in _IGNORED_COPY_DIRS for part in parts[:-1]):
        return True
    name = parts[-1] if parts else ""
    return (
        name == ".env"
        or (name.startswith(".env.") and name not in _PUBLIC_ENV_TEMPLATES)
        or name in _CREDENTIAL_FILE_NAMES
        or Path(name).suffix in _PRIVATE_KEY_SUFFIXES
    )


def _copy_workspace(source: Path, destination: Path, *, limit: int) -> int:
    size = _copy_size(source, limit=limit)

    def ignore(directory: str, names: list[str]) -> set[str]:
        current = Path(directory)
        return {
            name
            for name in names
            if name in _IGNORED_COPY_DIRS
            or name == ".git"
            or _is_credential_path((current / name).relative_to(source))
        }

    shutil.copytree(source, destination, symlinks=True, ignore=ignore)
    return size


def _parent_dir_args(paths: Sequence[Path]) -> list[str]:
    """Create each bind target parent once, shallowest first."""

    unique: set[Path] = set()
    for path in paths:
        unique.update(
            parent for parent in path.parents if str(parent) not in {".", "/"}
        )
    result: list[str] = []
    for parent in sorted(unique, key=lambda item: (len(item.parts), str(item))):
        result.extend(["--dir", str(parent)])
    return result


def _covered_by(path: Path, roots: Sequence[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _sandbox_command(*, work: Path, home: Path, argv: Sequence[str]) -> list[str]:
    executable = shutil.which("bwrap") or "bwrap"
    # Preserve both lexical and resolved roots. uv-managed virtual environments
    # commonly point at a version alias which is itself a symlink; mounting only
    # the resolved interpreter root leaves the venv's python entry point broken
    # inside the otherwise-correct sandbox.
    venv_root = Path(sys.prefix)
    python_root = Path(sys._base_executable).parent.parent
    # Do not expose host /etc: it may contain readable service credentials.
    # The sandbox needs the runtime under /usr, not host configuration.
    system_roots = tuple(Path(item) for item in ("/usr",) if Path(item).exists())
    extra_roots = tuple(
        path
        for path in dict.fromkeys(
            (venv_root, venv_root.resolve(), python_root, python_root.resolve())
        )
        if not _covered_by(path, system_roots)
    )
    command = [
        executable,
        "--die-with-parent",
        "--new-session",
        "--unshare-net",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
    ]
    for system_path in system_roots:
        command.extend(["--ro-bind", str(system_path), str(system_path)])
    for link_path in (Path("/bin"), Path("/lib"), Path("/lib64")):
        if link_path.is_symlink():
            command.extend(["--symlink", os.readlink(link_path), str(link_path)])
    command.extend(["--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"])
    command.extend(_parent_dir_args(extra_roots))
    for extra_root in extra_roots:
        command.extend(["--ro-bind", str(extra_root), str(extra_root)])
    command.extend(
        [
            "--dir",
            "/verification-home",
            "--bind",
            str(work),
            "/work",
            "--bind",
            str(home),
            "/verification-home",
            "--clearenv",
            "--setenv",
            "HOME",
            "/verification-home",
            "--setenv",
            "PATH",
            f"{venv_root / 'bin'}:/usr/bin:/bin",
            "--setenv",
            "LANG",
            "C.UTF-8",
            "--setenv",
            "LC_ALL",
            "C.UTF-8",
            "--setenv",
            "PYTHONNOUSERSITE",
            "1",
            "--chdir",
            "/work",
            "--",
            *[str(item) for item in argv],
        ]
    )
    return command


async def _run_isolated(
    command: Sequence[str], *, timeout_s: float
) -> tuple[int, bytes, bytes, bool]:
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        timed_out = True
        if proc.pid:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except asyncio.TimeoutError:
            if proc.pid:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            stdout, stderr = await proc.communicate()
    return int(proc.returncode or 0), stdout, stderr, timed_out


async def execute_verification_run(
    arguments: Mapping[str, Any],
    *,
    workspace_dir: Path,
    options: Mapping[str, Any] | None = None,
) -> BuiltinExecutionResult:
    """List or run an operator-registered recipe in an ephemeral no-network copy."""

    operation = str(arguments.get("operation") or "").strip().casefold()
    recipes = _recipe_catalog(options)
    if operation == "list":
        visible = {
            name: {
                "description": item["description"],
                "timeout_s": item["timeout_s"],
            }
            for name, item in sorted(recipes.items())
        }
        return _result(
            json.dumps(visible, ensure_ascii=False, sort_keys=True, indent=2),
            operation="list",
            recipe_count=len(visible),
        )
    if operation != "run":
        return _result("Error: operation must be list or run", operation=operation)

    recipe_name = str(arguments.get("recipe") or "").strip()
    recipe = recipes.get(recipe_name)
    if recipe is None:
        return _result(
            f"Error: unknown verification recipe {recipe_name!r}; call list first",
            operation="run",
            recipe=recipe_name,
            unavailable=True,
        )
    if not _bubblewrap_available():
        return _result(
            "Error: verification isolation is unavailable; refusing unsafe fallback",
            operation="run",
            recipe=recipe_name,
            unavailable=True,
            isolated=False,
        )

    source = Path(workspace_dir).resolve()
    try:
        copy_limit = int((options or {}).get("copy_limit_bytes", _DEFAULT_COPY_LIMIT_BYTES))
    except (TypeError, ValueError):
        copy_limit = _DEFAULT_COPY_LIMIT_BYTES
    if copy_limit <= 0:
        return _result(
            "Error: verification copy_limit_bytes must be positive",
            operation="run",
            recipe=recipe_name,
            unavailable=True,
        )

    final_output = ""
    final_details: dict[str, Any] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="hashi-verification-") as raw_temp:
            temp_root = Path(raw_temp)
            isolated_work = temp_root / "work"
            isolated_home = temp_root / "home"
            isolated_home.mkdir()
            copied_bytes = await asyncio.to_thread(
                _copy_workspace, source, isolated_work, limit=copy_limit
            )
            command = _sandbox_command(
                work=isolated_work,
                home=isolated_home,
                argv=recipe["argv"],
            )
            exit_code, stdout, stderr, timed_out = await _run_isolated(
                command, timeout_s=float(recipe["timeout_s"])
            )
            combined = _bounded_text(stdout + (b"\n" if stdout and stderr else b"") + stderr)
            final_output = combined or f"Recipe {recipe_name} exited with code {exit_code}."
            if timed_out:
                final_output = (
                    f"Error: verification recipe {recipe_name} timed out after "
                    f"{recipe['timeout_s']} seconds\n{final_output}"
                )
            elif exit_code != 0:
                final_output = (
                    f"Error: verification recipe {recipe_name} failed with exit code "
                    f"{exit_code}\n{final_output}"
                )
            final_details = {
                "operation": "run",
                "recipe": recipe_name,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "isolated": True,
                "network_disabled": True,
                "credentials_cleared": True,
                "workspace_copy_bytes": copied_bytes,
            }
    except Exception as exc:
        return _result(
            f"Error: verification isolation unavailable: {exc}",
            operation="run",
            recipe=recipe_name,
            unavailable=True,
            isolated=False,
        )
    final_details["temporary_workspace_destroyed"] = True
    return _result(final_output, **final_details)
