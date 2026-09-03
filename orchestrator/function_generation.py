"""Transactional function-generation staging for HASHI hot reboot.

Core never mutates an active module object with ``importlib.reload``.  A
candidate is first imported in an isolated interpreter, then materialised as
fresh module objects.  Only a fully validated candidate can replace the
canonical module bindings and manager bundle.
"""

from __future__ import annotations

import argparse
import ast
import atexit
import asyncio
import builtins
import concurrent.futures
import contextlib
import hashlib
import importlib
import importlib.util
import io
import json
import multiprocessing.process
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import traceback
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from types import FunctionType, ModuleType
from typing import Any

if __name__ == "__main__" and not __package__:
    # ``python -I path/to/function_generation.py --probe`` intentionally drops
    # the working directory from sys.path. Add only the verified project root.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.hot_reload import (
    FUNCTION_MODULE_PREFIXES,
    HotReloadError,
    discover_loaded_project_modules,
    is_function_module_name,
    module_reload_key,
    validate_function_contract,
)
from orchestrator.runtime_contract import (
    RuntimeContractError,
    RuntimeFingerprint,
    compare_runtime_fingerprints,
    current_runtime_fingerprint,
    load_runtime_policy,
)

PROBE_RESULT_PREFIX = "HASHI_FUNCTION_PROBE_RESULT="
DEFAULT_PROBE_TIMEOUT_SECONDS = 120.0
_ROOT_PACKAGES = tuple(prefix[:-1] for prefix in FUNCTION_MODULE_PREFIXES)


class FunctionGenerationError(HotReloadError):
    """A candidate generation could not be prepared or committed safely."""


@dataclass(frozen=True)
class SourceEntry:
    module: str
    relative_path: str
    sha256: str


@dataclass(frozen=True)
class SourceManifest:
    generation_id: str
    entries: tuple[SourceEntry, ...]

    @property
    def module_names(self) -> tuple[str, ...]:
        return tuple(entry.module for entry in self.entries)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation_id": self.generation_id,
            "entries": [asdict(entry) for entry in self.entries],
        }


@dataclass(frozen=True)
class CandidateProbeReceipt:
    generation_id: str
    module_names: tuple[str, ...]
    runtime: RuntimeFingerprint
    probe_pid: int


@dataclass
class _LiveModuleSnapshot:
    modules: dict[str, ModuleType]
    module_dicts: dict[str, dict[str, Any]]


def _is_function_module(name: str) -> bool:
    return is_function_module_name(name)


def _module_path_from_name(name: str, code_root: Path) -> Path | None:
    relative = Path(*name.split("."))
    source = code_root / relative.with_suffix(".py")
    if source.is_file():
        return source.resolve()
    package_source = code_root / relative / "__init__.py"
    if package_source.is_file():
        return package_source.resolve()
    return None


def _function_source_index(code_root: Path) -> dict[str, Path]:
    """Index function sources once instead of issuing thousands of path stats."""

    root = Path(code_root).resolve()
    indexed: dict[str, Path] = {}
    for package in _ROOT_PACKAGES:
        package_root = root / package
        if not package_root.is_dir():
            continue
        for source in package_root.rglob("*.py"):
            relative = source.relative_to(root)
            parts = list(relative.with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            if parts:
                indexed[".".join(parts)] = source
    return indexed


def _module_source_path(
    name: str,
    code_root: Path,
    modules: Mapping[str, ModuleType] | None = None,
    source_index: Mapping[str, Path] | None = None,
) -> Path | None:
    if source_index is not None:
        return source_index.get(name)
    loaded = modules if modules is not None else sys.modules
    module = loaded.get(name)
    raw_file = getattr(module, "__file__", None) if module is not None else None
    path: Path | None = None
    if raw_file:
        path = Path(raw_file)
        if path.suffix in {".pyc", ".pyo"}:
            source = Path(str(path)[:-1])
            if source.is_file():
                path = source
    if path is None or path.suffix != ".py" or not path.is_file():
        path = _module_path_from_name(name, code_root)
    if path is None:
        return None
    try:
        resolved = path.resolve()
        resolved.relative_to(code_root.resolve())
    except (OSError, ValueError):
        return None
    return resolved


def _declared_function_imports(
    module_name: str,
    source_path: Path,
    *,
    code_root: Path,
    source_index: Mapping[str, Path],
) -> set[str]:
    """Find statically declared function imports, including lazy branches.

    Ordering deliberately considers only module-scope imports, but generation
    membership must also include imports inside functions. Otherwise a module
    first imported after commit could silently enter the generation without
    probe, compilation, source hashing, or side-effect validation.
    """

    relative = source_path.relative_to(code_root).as_posix()
    package = (
        module_name
        if relative.endswith("/__init__.py")
        else module_name.rpartition(".")[0]
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    candidates: list[str] = []

    class AllImportVisitor(ast.NodeVisitor):
        def visit_Import(self, node: ast.Import) -> None:
            candidates.extend(alias.name for alias in node.names)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            base = node.module or ""
            if node.level:
                try:
                    base = importlib.util.resolve_name(
                        "." * node.level + base,
                        package,
                    )
                except (ImportError, ValueError):
                    return
            if base:
                candidates.append(base)
                candidates.extend(
                    f"{base}.{alias.name}"
                    for alias in node.names
                    if alias.name != "*"
                )

        def visit_Call(self, node: ast.Call) -> None:
            dynamic_name: str | None = None
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "importlib"
                and node.func.attr == "import_module"
            ):
                dynamic_name = (
                    node.args[0].value
                    if node.args and isinstance(node.args[0], ast.Constant)
                    else None
                )
            elif isinstance(node.func, ast.Name) and node.func.id == "__import__":
                dynamic_name = (
                    node.args[0].value
                    if node.args and isinstance(node.args[0], ast.Constant)
                    else None
                )
            if isinstance(dynamic_name, str):
                candidates.append(dynamic_name)
            self.generic_visit(node)

    AllImportVisitor().visit(tree)
    declared: set[str] = set()
    for candidate in candidates:
        probe = candidate
        while probe:
            if (
                _is_function_module(probe)
                and probe in source_index
            ):
                declared.add(probe)
                break
            probe = probe.rpartition(".")[0]
    return declared


def build_source_manifest(
    module_names: list[str] | tuple[str, ...],
    *,
    code_root: Path,
    modules: Mapping[str, ModuleType] | None = None,
) -> SourceManifest:
    root = Path(code_root).resolve()
    source_index = _function_source_index(root)
    entries: list[SourceEntry] = []
    missing: list[str] = []
    pending = sorted(set(module_names), key=module_reload_key)
    visited: set[str] = set()
    while pending:
        name = pending.pop(0)
        if name in visited:
            continue
        visited.add(name)
        if not _is_function_module(name):
            continue
        path = _module_source_path(
            name,
            root,
            modules,
            source_index,
        )
        if path is None:
            missing.append(name)
            continue
        data = path.read_bytes()
        # Compile without creating __pycache__; candidate staging must be
        # observational until the generation is committed.
        try:
            compile(data, str(path), "exec", dont_inherit=True)
        except SyntaxError as exc:
            raise FunctionGenerationError(
                f"Candidate source does not compile: {name}: {exc.msg} "
                f"(line {exc.lineno})"
            ) from exc
        entries.append(
            SourceEntry(
                module=name,
                relative_path=path.relative_to(root).as_posix(),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
        discovered = _declared_function_imports(
            name,
            path,
            code_root=root,
            source_index=source_index,
        )
        pending.extend(
            sorted(discovered - visited - set(pending), key=module_reload_key)
        )
    if missing:
        raise FunctionGenerationError(
            f"Candidate function modules have no project source: {sorted(missing)}"
        )
    entries = _order_source_entries(entries, root)
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(entry.module.encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry.relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry.sha256.encode("ascii"))
        digest.update(b"\n")
    return SourceManifest(
        generation_id="sha256:" + digest.hexdigest(),
        entries=tuple(entries),
    )


def _import_dependencies(
    entry: SourceEntry,
    *,
    code_root: Path,
    available: set[str],
) -> set[str]:
    source = code_root / entry.relative_path
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    package = (
        entry.module
        if entry.relative_path.endswith("/__init__.py")
        else entry.module.rpartition(".")[0]
    )
    candidates: list[str] = []

    class ModuleScopeImportVisitor(ast.NodeVisitor):
        """Collect imports executed while constructing a module.

        Imports inside function bodies are lazy runtime edges. Treating them
        as import-time dependencies creates artificial cycles and can force a
        consumer to bind the previous generation of its real provider.
        """

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            del node

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            del node

        def visit_Lambda(self, node: ast.Lambda) -> None:
            del node

        def visit_Import(self, node: ast.Import) -> None:
            candidates.extend(alias.name for alias in node.names)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            base = node.module or ""
            if node.level:
                try:
                    base = importlib.util.resolve_name(
                        "." * node.level + base,
                        package,
                    )
                except (ImportError, ValueError):
                    return
            if base:
                candidates.append(base)
                candidates.extend(
                    f"{base}.{alias.name}"
                    for alias in node.names
                    if alias.name != "*"
                )

        def visit_Call(self, node: ast.Call) -> None:
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "importlib"
                and node.func.attr == "import_module"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                candidates.append(node.args[0].value)
            self.generic_visit(node)

    ModuleScopeImportVisitor().visit(tree)

    dependencies: set[str] = set()
    for candidate in candidates:
        probe = candidate
        while probe:
            if probe in available and probe != entry.module:
                dependencies.add(probe)
                break
            probe = probe.rpartition(".")[0]
    return dependencies


def _order_source_entries(
    entries: list[SourceEntry], code_root: Path
) -> list[SourceEntry]:
    """Topologically order providers before consumers, with stable cycle ties."""

    by_name = {entry.module: entry for entry in entries}
    available = set(by_name)
    dependencies = {
        name: _import_dependencies(entry, code_root=code_root, available=available)
        for name, entry in by_name.items()
    }
    ordered: list[SourceEntry] = []
    remaining = set(by_name)
    while remaining:
        ready = [
            name for name in remaining if not (dependencies[name] & remaining)
        ]
        if not ready:
            # A genuine import cycle is already supported by Python's normal
            # cold importer. Preserve the explicit foundation order within the
            # cycle; the stale-binding validator remains the final authority.
            ready = [min(remaining, key=module_reload_key)]
        for name in sorted(ready, key=module_reload_key):
            if name not in remaining:
                continue
            ordered.append(by_name[name])
            remaining.remove(name)
    return ordered


def verify_source_manifest(manifest: SourceManifest, *, code_root: Path) -> None:
    current = build_source_manifest(manifest.module_names, code_root=code_root)
    if current != manifest:
        raise FunctionGenerationError(
            "Candidate source changed after verification; no generation was committed"
        )


def _project_modules(
    modules: Mapping[str, ModuleType], *, code_root: Path
) -> dict[str, ModuleType]:
    root = Path(code_root).resolve()
    selected: dict[str, ModuleType] = {}
    for name, module in list(modules.items()):
        if not (
            name in _ROOT_PACKAGES
            or any(name.startswith(prefix) for prefix in FUNCTION_MODULE_PREFIXES)
        ):
            continue
        raw_file = getattr(module, "__file__", None)
        if raw_file is None:
            # Namespace package roots are still project-owned parent bindings.
            search = getattr(module, "__path__", ())
            try:
                if any(Path(item).resolve() == root / name for item in search):
                    selected[name] = module
            except (OSError, TypeError, ValueError):
                pass
            continue
        path = Path(raw_file)
        if path.suffix in {".pyc", ".pyo"}:
            path = Path(str(path)[:-1])
        try:
            path.resolve().relative_to(root)
        except (OSError, ValueError):
            continue
        selected[name] = module
    return selected


def _capture_live_modules(code_root: Path) -> _LiveModuleSnapshot:
    modules = _project_modules(sys.modules, code_root=code_root)
    return _LiveModuleSnapshot(
        modules=modules,
        module_dicts={name: dict(module.__dict__) for name, module in modules.items()},
    )


def _restore_live_modules(snapshot: _LiveModuleSnapshot, *, code_root: Path) -> None:
    current = _project_modules(sys.modules, code_root=code_root)
    for name in set(current) - set(snapshot.modules):
        sys.modules.pop(name, None)
    for name, module in snapshot.modules.items():
        sys.modules[name] = module
    # Import machinery writes child attributes onto parent packages. Restore
    # all project module dictionaries so a rejected candidate leaves no stale
    # public binding behind.
    for name, before in snapshot.module_dicts.items():
        module = snapshot.modules[name]
        module.__dict__.clear()
        module.__dict__.update(before)


def _bind_parent(name: str, module: ModuleType) -> None:
    parent_name, separator, child = name.rpartition(".")
    if not separator:
        return
    parent = sys.modules.get(parent_name)
    if parent is not None:
        setattr(parent, child, module)


def _install_module_set(modules: Mapping[str, ModuleType]) -> None:
    for name in sorted(modules, key=lambda item: (item.count("."), item)):
        sys.modules[name] = modules[name]
    for name in sorted(modules, key=lambda item: (item.count("."), item)):
        _bind_parent(name, modules[name])


def _fresh_module(name: str, source_path: Path, previous: ModuleType) -> ModuleType:
    is_package = hasattr(previous, "__path__") or source_path.name == "__init__.py"
    search_locations = [str(source_path.parent)] if is_package else None
    spec = importlib.util.spec_from_file_location(
        name,
        source_path,
        submodule_search_locations=search_locations,
    )
    if spec is None or spec.loader is None:
        raise FunctionGenerationError(f"Cannot construct loader for {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    _bind_parent(name, module)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def _candidate_import_guard():
    """Block irreversible import-time actions while materialising a candidate."""

    originals: list[tuple[Any, str, Any]] = []
    staging_thread = threading.get_ident()

    def patch(owner: Any, attribute: str, replacement: Any) -> None:
        if hasattr(owner, attribute):
            originals.append((owner, attribute, getattr(owner, attribute)))
            setattr(owner, attribute, replacement)

    def patch_blocked(owner: Any, attribute: str, action: str) -> None:
        if not hasattr(owner, attribute):
            return
        original = getattr(owner, attribute)

        def guarded(*args, **kwargs):
            # The active generation may still have background threads. The
            # staging guard constrains only the candidate construction thread;
            # it must never sabotage unrelated live work process-wide.
            if threading.get_ident() == staging_thread:
                raise FunctionGenerationError(
                    "Function modules must be import-pure; blocked "
                    f"{action} during staging"
                )
            return original(*args, **kwargs)

        patch(owner, attribute, guarded)

    original_open = builtins.open

    def is_null_sink(file: Any) -> bool:
        return str(file).casefold() in {str(os.devnull).casefold(), "nul"}

    def guarded_open(file, mode="r", *args, **kwargs):
        if threading.get_ident() == staging_thread and any(
            flag in str(mode) for flag in ("w", "a", "x", "+")
        ) and not is_null_sink(file):
            raise FunctionGenerationError(
                f"Function modules must be import-pure; blocked file write: {file}"
            )
        return original_open(file, mode, *args, **kwargs)

    original_io_open = io.open

    def guarded_io_open(file, mode="r", *args, **kwargs):
        if threading.get_ident() == staging_thread and any(
            flag in str(mode) for flag in ("w", "a", "x", "+")
        ) and not is_null_sink(file):
            raise FunctionGenerationError(
                f"Function modules must be import-pure; blocked file write: {file}"
            )
        return original_io_open(file, mode, *args, **kwargs)

    original_os_open = os.open

    def guarded_os_open(path, flags, *args, **kwargs):
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        if (
            threading.get_ident() == staging_thread
            and flags & write_flags
            and not is_null_sink(path)
        ):
            raise FunctionGenerationError(
                f"Function modules must be import-pure; blocked file write: {path}"
            )
        return original_os_open(path, flags, *args, **kwargs)

    original_popen = subprocess.Popen

    def guarded_popen(*args, **kwargs):
        command = args[0] if args else kwargs.get("args")
        normalized = tuple(str(part) for part in command) if isinstance(
            command, (list, tuple)
        ) else ()
        # CPython's stdlib uses this exact read-only query to resolve libc while
        # importing ctypes-backed wheels such as ifaddr. It creates no process
        # state and is needed for a truthful cold-import probe.
        if normalized == ("/sbin/ldconfig", "-p"):
            return original_popen(*args, **kwargs)
        if threading.get_ident() == staging_thread:
            raise FunctionGenerationError(
                "Function modules must be import-pure; blocked "
                "subprocess.Popen during staging"
            )
        return original_popen(*args, **kwargs)

    old_dont_write_bytecode = sys.dont_write_bytecode
    patch(builtins, "open", guarded_open)
    patch(io, "open", guarded_io_open)
    patch(os, "open", guarded_os_open)
    for attribute in ("write_text", "write_bytes", "touch", "mkdir", "unlink"):
        patch_blocked(Path, attribute, f"Path.{attribute}")
    for attribute in ("rename", "replace"):
        patch_blocked(Path, attribute, f"Path.{attribute}")
    for attribute in (
        "mkdir",
        "makedirs",
        "remove",
        "unlink",
        "rename",
        "replace",
        "rmdir",
        "removedirs",
        "chdir",
        "system",
        "fork",
        "forkpty",
        "posix_spawn",
        "posix_spawnp",
        "putenv",
        "unsetenv",
    ):
        patch_blocked(os, attribute, f"os.{attribute}")
    for attribute in ("copy", "copy2", "copyfile", "copytree", "move", "rmtree"):
        patch_blocked(shutil, attribute, f"shutil.{attribute}")
    patch(subprocess, "Popen", guarded_popen)
    for attribute in ("run", "call", "check_call", "check_output"):
        patch_blocked(subprocess, attribute, f"subprocess.{attribute}")
    patch_blocked(multiprocessing.process.BaseProcess, "start", "process start")
    patch_blocked(threading.Thread, "start", "thread start")
    patch_blocked(asyncio, "create_task", "asyncio.create_task")
    patch_blocked(asyncio.BaseEventLoop, "create_task", "event-loop task creation")
    patch_blocked(
        concurrent.futures.ThreadPoolExecutor,
        "submit",
        "thread-pool submission",
    )
    patch_blocked(
        concurrent.futures.ProcessPoolExecutor,
        "submit",
        "process-pool submission",
    )
    patch_blocked(socket, "create_connection", "network connection")
    patch_blocked(signal, "signal", "signal handler mutation")
    patch_blocked(atexit, "register", "exit handler registration")
    patch_blocked(os._Environ, "__setitem__", "environment mutation")
    patch_blocked(os._Environ, "__delitem__", "environment mutation")
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = old_dont_write_bytecode
        for owner, attribute, original in reversed(originals):
            setattr(owner, attribute, original)


def validate_no_stale_bindings(candidates: Mapping[str, ModuleType]) -> None:
    """Reject direct references into an older copy of the same generation."""

    stale: list[str] = []
    for consumer_name, consumer in candidates.items():
        for attribute, value in list(consumer.__dict__.items()):
            provider_name: str | None = None
            provider_value: Any = None
            if isinstance(value, ModuleType):
                provider_name = value.__name__
                provider_value = candidates.get(provider_name)
            elif isinstance(value, (type, FunctionType)):
                provider_name = getattr(value, "__module__", None)
                provider = candidates.get(provider_name or "")
                if provider is not None:
                    provider_value = getattr(provider, getattr(value, "__name__", ""), None)
            if provider_name in candidates and provider_value is not value:
                stale.append(f"{consumer_name}.{attribute}->{provider_name}")
                if len(stale) >= 20:
                    break
        if len(stale) >= 20:
            break
    if stale:
        raise FunctionGenerationError(
            "Candidate generation retained bindings to an older module generation: "
            + ", ".join(stale)
        )


def _probe_payload(
    *,
    code_root: Path,
    module_names: tuple[str, ...],
    expected_runtime: RuntimeFingerprint,
) -> dict[str, Any]:
    return {
        "code_root": str(Path(code_root).resolve()),
        "module_names": list(module_names),
        "expected_runtime": expected_runtime.to_dict(),
    }


def run_candidate_probe(
    *,
    code_root: Path,
    module_names: tuple[str, ...],
    expected_runtime: RuntimeFingerprint,
    timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> CandidateProbeReceipt:
    payload = _probe_payload(
        code_root=code_root,
        module_names=module_names,
        expected_runtime=expected_runtime,
    )
    command = [sys.executable, "-I", str(Path(__file__).resolve()), "--probe"]
    try:
        completed = subprocess.run(
            command,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            cwd=Path(code_root).resolve(),
            timeout=timeout_seconds,
            check=False,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired as exc:
        raise FunctionGenerationError(
            f"Candidate staging worker exceeded {timeout_seconds:.1f}s"
        ) from exc
    result_line = next(
        (
            line[len(PROBE_RESULT_PREFIX) :]
            for line in reversed(completed.stdout.splitlines())
            if line.startswith(PROBE_RESULT_PREFIX)
        ),
        None,
    )
    if result_line is None:
        detail = (completed.stderr or completed.stdout or "no diagnostic output").strip()
        raise FunctionGenerationError(
            f"Candidate staging worker returned no receipt (exit={completed.returncode}): "
            f"{detail[-2000:]}"
        )
    try:
        result = json.loads(result_line)
    except json.JSONDecodeError as exc:
        raise FunctionGenerationError("Candidate staging worker returned invalid JSON") from exc
    if completed.returncode != 0 or not result.get("ok"):
        detail = str(result.get("error") or completed.stderr or "candidate rejected")
        probe_traceback = str(result.get("traceback") or "").strip()
        if probe_traceback:
            detail = f"{detail}\n{probe_traceback}"
        raise FunctionGenerationError(
            f"Candidate staging worker rejected generation: {detail}"
        )
    runtime = RuntimeFingerprint.from_mapping(result["runtime"])
    compare_runtime_fingerprints(expected_runtime, runtime)
    return CandidateProbeReceipt(
        generation_id=str(result["generation_id"]),
        module_names=tuple(str(name) for name in result["module_names"]),
        runtime=runtime,
        probe_pid=int(result["probe_pid"]),
    )


class PreparedFunctionGeneration:
    """A verified, inactive copy-on-write function generation."""

    def __init__(
        self,
        *,
        code_root: Path,
        manifest: SourceManifest,
        receipt: CandidateProbeReceipt,
        candidates: dict[str, ModuleType],
        previous_snapshot: _LiveModuleSnapshot,
        manager_bundle: dict[str, object],
        previous_managers: dict[str, object],
        previous_generation: Any,
    ) -> None:
        self.code_root = Path(code_root).resolve()
        self.manifest = manifest
        self.receipt = receipt
        self.candidates = candidates
        self.previous_snapshot = previous_snapshot
        self.manager_bundle = manager_bundle
        self.previous_managers = previous_managers
        self.previous_generation = previous_generation
        self.active = False

    def activate(self, kernel) -> None:
        if self.active:
            raise FunctionGenerationError("Candidate generation is already active")
        # Recompute the complete Core fingerprint immediately before commit.
        # This closes the window between the isolated probe and cutover: a
        # changed Core file, interpreter environment, dependency set, or ABI
        # invalidates the receipt instead of entering the live module map.
        policy = load_runtime_policy(self.code_root)
        live_runtime = current_runtime_fingerprint(policy, code_root=self.code_root)
        try:
            compare_runtime_fingerprints(self.receipt.runtime, live_runtime)
        except RuntimeContractError as exc:
            raise FunctionGenerationError(
                "Core runtime changed after candidate verification; no generation "
                f"was committed: {exc}"
            ) from exc
        verify_source_manifest(self.manifest, code_root=self.code_root)
        for name in self.manifest.module_names:
            expected = self.previous_snapshot.modules.get(name)
            if expected is not None and sys.modules.get(name) is not expected:
                raise FunctionGenerationError(
                    f"Live module generation changed while staging: {name}"
                )
        try:
            _install_module_set(self.candidates)
            registry = importlib.import_module("orchestrator.manager_registry")
            registry.install_hot_manager_bundle(kernel, self.manager_bundle)
            kernel.function_generation = {
                "generation_id": self.manifest.generation_id,
                "probe_pid": self.receipt.probe_pid,
                "module_count": len(self.manifest.entries),
                "runtime_id": self.receipt.runtime.runtime_id,
            }
        except Exception as exc:
            _restore_live_modules(self.previous_snapshot, code_root=self.code_root)
            for attribute, manager in self.previous_managers.items():
                setattr(kernel, attribute, manager)
            kernel.function_generation = self.previous_generation
            raise FunctionGenerationError(
                f"Candidate generation commit failed without changing the active generation: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        self.active = True

    def rollback(self, kernel) -> None:
        if not self.active:
            return
        _restore_live_modules(self.previous_snapshot, code_root=self.code_root)
        for attribute, manager in self.previous_managers.items():
            setattr(kernel, attribute, manager)
        kernel.function_generation = self.previous_generation
        self.active = False


def prepare_function_generation(
    kernel,
    console_handler,
    *,
    module_names: list[str] | tuple[str, ...] | None = None,
    probe_runner=run_candidate_probe,
) -> PreparedFunctionGeneration:
    code_root = Path(kernel.paths.code_root).resolve()
    expected_runtime = getattr(kernel, "runtime_fingerprint", None)
    if not isinstance(expected_runtime, RuntimeFingerprint):
        raise FunctionGenerationError(
            "Core runtime fingerprint is unavailable; function reboot is disabled"
        )
    initial_names = tuple(
        module_names
        if module_names is not None
        else discover_loaded_project_modules(code_root=code_root)
    )
    initial_manifest = build_source_manifest(initial_names, code_root=code_root)
    receipt = probe_runner(
        code_root=code_root,
        module_names=initial_manifest.module_names,
        expected_runtime=expected_runtime,
    )
    manifest = build_source_manifest(receipt.module_names, code_root=code_root)
    if manifest.generation_id != receipt.generation_id:
        raise FunctionGenerationError(
            "Candidate source differs between Core and staging worker"
        )

    snapshot = _capture_live_modules(code_root)
    candidates: dict[str, ModuleType] = {}
    try:
        with _candidate_import_guard():
            for entry in manifest.entries:
                previous = snapshot.modules.get(entry.module)
                if previous is None:
                    # A newly introduced transitive module was imported by the
                    # probe; materialise it from its declared source.
                    previous = ModuleType(entry.module)
                    if entry.relative_path.endswith("/__init__.py"):
                        previous.__path__ = [
                            str((code_root / entry.relative_path).parent)
                        ]
                source = code_root / entry.relative_path
                candidates[entry.module] = _fresh_module(
                    entry.module,
                    source,
                    previous,
                )
            # READY includes cross-module validation and construction of every
            # replacement Manager. Constructors therefore inherit the same
            # import-purity boundary as module execution: staging cannot write
            # files or launch operational work before Core commits it.
            validate_no_stale_bindings(candidates)
            validate_function_contract(
                lambda name: candidates.get(name) or importlib.import_module(name)
            )
            registry = importlib.import_module("orchestrator.manager_registry")
            previous_managers = {
                spec.attribute: getattr(kernel, spec.attribute)
                for spec in registry.HOT_MANAGER_SPECS
            }
            manager_bundle = registry.build_hot_manager_bundle(
                kernel,
                console_handler,
                module_loader=lambda name: candidates.get(name)
                or importlib.import_module(name),
            )
            verify_source_manifest(manifest, code_root=code_root)
    except Exception as exc:
        _restore_live_modules(snapshot, code_root=code_root)
        if isinstance(exc, FunctionGenerationError):
            raise
        if isinstance(exc, (HotReloadError, RuntimeContractError)):
            raise FunctionGenerationError(str(exc)) from exc
        raise FunctionGenerationError(
            f"Candidate materialisation failed: {type(exc).__name__}: {exc}"
        ) from exc
    _restore_live_modules(snapshot, code_root=code_root)
    return PreparedFunctionGeneration(
        code_root=code_root,
        manifest=manifest,
        receipt=receipt,
        candidates=candidates,
        previous_snapshot=snapshot,
        manager_bundle=manager_bundle,
        previous_managers=previous_managers,
        previous_generation=getattr(kernel, "function_generation", None),
    )


def _probe_main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        code_root = Path(payload["code_root"]).resolve()
        sys.path.insert(0, str(code_root))
        policy = load_runtime_policy(code_root)
        runtime = current_runtime_fingerprint(policy, code_root=code_root)
        expected = RuntimeFingerprint.from_mapping(payload["expected_runtime"])
        compare_runtime_fingerprints(expected, runtime)
        requested = tuple(str(name) for name in payload["module_names"])
        with _candidate_import_guard():
            for name in requested:
                importlib.import_module(name)
            validate_function_contract()
            registry = importlib.import_module("orchestrator.manager_registry")
            for spec in registry.HOT_MANAGER_SPECS:
                module = importlib.import_module(spec.module)
                manager_class = getattr(module, spec.class_name, None)
                if not isinstance(manager_class, type):
                    raise FunctionGenerationError(
                        f"Manager contract unavailable: {spec.module}.{spec.class_name}"
                    )
        expanded = tuple(discover_loaded_project_modules(code_root=code_root))
        manifest = build_source_manifest(expanded, code_root=code_root)
        result = {
            "ok": True,
            "generation_id": manifest.generation_id,
            "module_names": list(manifest.module_names),
            "runtime": runtime.to_dict(),
            "probe_pid": os.getpid(),
        }
        print(PROBE_RESULT_PREFIX + json.dumps(result, sort_keys=True), flush=True)
        return 0
    except Exception as exc:
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=20),
            "probe_pid": os.getpid(),
        }
        print(PROBE_RESULT_PREFIX + json.dumps(result, sort_keys=True), flush=True)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args(argv)
    if args.probe:
        return _probe_main()
    parser.error("function_generation is an internal Core module")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
