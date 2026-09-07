"""Immutable Function Worker generation qualification.

HASHI Core never imports a candidate into its active module space. This module
compiles and hashes the complete Agent-function closure plus its static assets,
validates that closure in a disposable interpreter, and returns an immutable
receipt which a persistent per-Agent Worker must re-verify.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import traceback
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.function_contract import (
    FUNCTION_MODULE_PREFIXES,
    FunctionContractError,
    discover_loaded_function_modules,
    function_module_order_key,
    is_function_module_name,
    validate_function_contract,
)
from orchestrator.kernel_artifact import manifest_digest
from orchestrator.kernel_import_guard import candidate_import_guard as stable_import_guard
from orchestrator.manager_registry import FUNCTION_MANAGER_SPECS
from orchestrator.post_turn_registry import declared_observer_modules
from orchestrator.runtime_contract import (
    RuntimeFingerprint,
    compare_runtime_fingerprints,
    current_runtime_fingerprint,
    load_runtime_policy,
)

PROBE_RESULT_PREFIX = "HASHI_FUNCTION_PROBE_RESULT="
DEFAULT_PROBE_TIMEOUT_SECONDS = 180.0
FUNCTION_GENERATION_SCHEMA_VERSION = 2
_ROOT_PACKAGES = tuple(prefix[:-1] for prefix in FUNCTION_MODULE_PREFIXES)

# A cold Core deliberately need not import Agent modules. These roots define
# the operational surface; static closure and the probe expand them.
FUNCTION_GENERATION_ENTRYPOINTS = (
    "orchestrator.runtime_app_host",
    "orchestrator.runtime_release",
    "orchestrator.function_worker_host",
    "orchestrator.admin_local_testing",
    "orchestrator.flexible_agent_runtime",
    "orchestrator.runtime_command_binding",
    "orchestrator.voice_manager",
    "tools.registry",
    "transports.chat_router",
    "transports.whatsapp",
)

_ASSET_EXCLUDED_PREFIXES = ("flow/runs/",)
_ASSET_EXCLUDED_PARTS = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)


class FunctionGenerationError(FunctionContractError):
    """A candidate function generation cannot safely enter a Worker."""


@dataclass(frozen=True)
class SourceEntry:
    module: str
    relative_path: str
    sha256: str


@dataclass(frozen=True)
class AssetEntry:
    relative_path: str
    sha256: str
    executable: bool


@dataclass(frozen=True)
class SourceManifest:
    generation_id: str
    entries: tuple[SourceEntry, ...]
    assets: tuple[AssetEntry, ...] = ()

    @property
    def module_names(self) -> tuple[str, ...]:
        return tuple(entry.module for entry in self.entries)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": FUNCTION_GENERATION_SCHEMA_VERSION,
            "generation_id": self.generation_id,
            "entries": [asdict(entry) for entry in self.entries],
            "assets": [asdict(asset) for asset in self.assets],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> SourceManifest:
        try:
            if int(value["schema_version"]) != FUNCTION_GENERATION_SCHEMA_VERSION:
                raise FunctionGenerationError(
                    "Serialized function manifest schema is unsupported"
                )
            entries = tuple(
                SourceEntry(
                    module=str(entry["module"]),
                    relative_path=str(entry["relative_path"]),
                    sha256=str(entry["sha256"]),
                )
                for entry in value.get("entries", ())
            )
            assets = tuple(
                AssetEntry(
                    relative_path=str(asset["relative_path"]),
                    sha256=str(asset["sha256"]),
                    executable=bool(asset["executable"]),
                )
                for asset in value.get("assets", ())
            )
            manifest = cls(
                generation_id=str(value["generation_id"]),
                entries=entries,
                assets=assets,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FunctionGenerationError(
                "Serialized function manifest is malformed"
            ) from exc
        if manifest != build_source_manifest_from_entries(entries, assets=assets):
            raise FunctionGenerationError(
                "Serialized function manifest has an invalid generation digest"
            )
        return manifest


@dataclass(frozen=True)
class CandidateProbeReceipt:
    generation_id: str
    module_names: tuple[str, ...]
    runtime: RuntimeFingerprint
    probe_pid: int


@dataclass(frozen=True)
class VerifiedFunctionGeneration:
    """Disposable-probe receipt for exact source and asset bytes."""

    code_root: Path
    manifest: SourceManifest
    receipt: CandidateProbeReceipt

    def verify(self, expected_runtime: RuntimeFingerprint) -> None:
        policy = load_runtime_policy(self.code_root)
        live_runtime = current_runtime_fingerprint(policy, code_root=self.code_root)
        compare_runtime_fingerprints(expected_runtime, live_runtime)
        compare_runtime_fingerprints(self.receipt.runtime, live_runtime)
        verify_source_manifest(self.manifest, code_root=self.code_root)

    def verify_qualified_source(self, expected_runtime: RuntimeFingerprint) -> None:
        """Revalidate exact bytes already accepted by the isolated probe.

        ``verify()`` rebuilds and recompiles the complete static dependency
        closure.  That work is required while qualifying a *new* generation,
        but repeating it for every Worker adds no evidence once the probe has
        accepted this immutable manifest.  At that point an exact source and
        asset byte comparison, plus the unchanged Core runtime fingerprint,
        proves that the qualified generation is still the one being used.
        """

        policy = load_runtime_policy(self.code_root)
        live_runtime = current_runtime_fingerprint(policy, code_root=self.code_root)
        compare_runtime_fingerprints(expected_runtime, live_runtime)
        compare_runtime_fingerprints(self.receipt.runtime, live_runtime)
        verify_qualified_manifest_bytes(self.manifest, code_root=self.code_root)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code_root": str(self.code_root),
            "manifest": self.manifest.to_dict(),
            "receipt": {
                "generation_id": self.receipt.generation_id,
                "module_names": list(self.receipt.module_names),
                "runtime": self.receipt.runtime.to_dict(),
                "probe_pid": self.receipt.probe_pid,
            },
        }


def _function_source_index(code_root: Path) -> dict[str, Path]:
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
                indexed[".".join(parts)] = source.resolve()
    return indexed


def _module_source_path(
    name: str,
    code_root: Path,
    modules: Mapping[str, ModuleType] | None,
    source_index: Mapping[str, Path],
) -> Path | None:
    indexed = source_index.get(name)
    if indexed is not None:
        return indexed
    module = (modules or {}).get(name)
    raw_file = getattr(module, "__file__", None) if module is not None else None
    if not raw_file:
        return None
    path = Path(raw_file)
    if path.suffix in {".pyc", ".pyo"}:
        path = Path(str(path)[:-1])
    try:
        resolved = path.resolve()
        resolved.relative_to(Path(code_root).resolve())
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() and resolved.suffix == ".py" else None


def _declared_function_imports(
    module_name: str,
    source_path: Path,
    *,
    code_root: Path,
    source_index: Mapping[str, Path],
) -> set[str]:
    """Return function imports anywhere in the AST, including lazy branches."""

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
            if is_function_module_name(probe) and probe in source_index:
                declared.add(probe)
                break
            probe = probe.rpartition(".")[0]
    return declared


def _module_scope_dependencies(
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

    class ModuleImportVisitor(ast.NodeVisitor):
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

    ModuleImportVisitor().visit(tree)
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
    entries: list[SourceEntry],
    code_root: Path,
) -> list[SourceEntry]:
    by_name = {entry.module: entry for entry in entries}
    available = set(by_name)
    dependencies = {
        name: _module_scope_dependencies(
            entry,
            code_root=code_root,
            available=available,
        )
        for name, entry in by_name.items()
    }
    ordered: list[SourceEntry] = []
    remaining = set(by_name)
    while remaining:
        ready = [name for name in remaining if not (dependencies[name] & remaining)]
        if not ready:
            ready = [min(remaining, key=function_module_order_key)]
        for name in sorted(ready, key=function_module_order_key):
            if name in remaining:
                ordered.append(by_name[name])
                remaining.remove(name)
    return ordered


def _asset_entries(code_root: Path) -> tuple[AssetEntry, ...]:
    root = Path(code_root).resolve()
    assets: list[AssetEntry] = []
    for package in (*_ROOT_PACKAGES, "locales"):
        package_root = root / package
        if not package_root.is_dir():
            continue
        for source in package_root.rglob("*"):
            if not source.is_file():
                continue
            relative = source.relative_to(root)
            relative_text = relative.as_posix()
            if source.suffix in {".py", ".pyc", ".pyo"}:
                continue
            if any(part in _ASSET_EXCLUDED_PARTS for part in relative.parts):
                continue
            if relative_text.startswith(_ASSET_EXCLUDED_PREFIXES):
                continue
            if source.is_symlink():
                raise FunctionGenerationError(
                    f"Function asset must not be a symbolic link: {relative_text}"
                )
            mode = source.stat().st_mode
            assets.append(
                AssetEntry(
                    relative_path=relative_text,
                    sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                    executable=bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)),
                )
            )
    return tuple(sorted(assets, key=lambda item: item.relative_path))


def build_source_manifest_from_entries(
    entries: tuple[SourceEntry, ...] | list[SourceEntry],
    *,
    assets: tuple[AssetEntry, ...] | list[AssetEntry] = (),
) -> SourceManifest:
    canonical_entries = tuple(entries)
    canonical_assets = tuple(assets)
    return SourceManifest(
        generation_id=manifest_digest([asdict(e) for e in canonical_entries], [asdict(a) for a in canonical_assets]),
        entries=canonical_entries,
        assets=canonical_assets,
    )


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
    pending = sorted(set(module_names), key=function_module_order_key)
    visited: set[str] = set()
    while pending:
        name = pending.pop(0)
        if name in visited:
            continue
        visited.add(name)
        if not is_function_module_name(name):
            continue
        path = _module_source_path(name, root, modules, source_index)
        if path is None:
            missing.append(name)
            continue
        data = path.read_bytes()
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
            sorted(
                discovered - visited - set(pending),
                key=function_module_order_key,
            )
        )
    if missing:
        raise FunctionGenerationError(
            f"Candidate function modules have no project source: {sorted(missing)}"
        )
    return build_source_manifest_from_entries(
        _order_source_entries(entries, root),
        assets=_asset_entries(root),
    )


def verify_source_manifest(manifest: SourceManifest, *, code_root: Path) -> None:
    current = build_source_manifest(manifest.module_names, code_root=code_root)
    if current != manifest:
        raise FunctionGenerationError(
            "Candidate source or asset changed after verification; "
            "no Function Worker was committed"
        )


def _verified_manifest_path(code_root: Path, relative_path: str) -> Path:
    root = Path(code_root).resolve()
    relative = PurePosixPath(str(relative_path))
    if (
        relative.is_absolute()
        or not relative.parts
        or "\\" in str(relative_path)
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise FunctionGenerationError(
            f"Function manifest path escapes its generation root: {relative_path}"
        )
    candidate = root.joinpath(*relative.parts)
    if not candidate.is_file() or candidate.is_symlink():
        raise FunctionGenerationError(
            f"Function manifest file is missing or unsafe: {relative_path}"
        )
    return candidate


def verify_qualified_manifest_bytes(
    manifest: SourceManifest,
    *,
    code_root: Path,
) -> None:
    """Verify an already-qualified generation without rebuilding its AST graph.

    Every source byte that could have changed the dependency closure is hashed.
    Assets are rediscovered as a set so additions, removals, executable-bit
    changes, and content changes are also rejected.  This is deliberately only
    for manifests carrying a prior isolated-probe receipt; initial generation
    qualification continues to use :func:`verify_source_manifest`.
    """

    root = Path(code_root).resolve()
    try:
        parent_paths = {
            parent
            for item in (*manifest.entries, *manifest.assets)
            for relative in (PurePosixPath(item.relative_path),)
            for parent in relative.parents
            if parent.parts
        }
        for relative in parent_paths:
            if root.joinpath(*relative.parts).is_symlink():
                raise FunctionGenerationError(str(relative))
        for entry in manifest.entries:
            source = _verified_manifest_path(root, entry.relative_path)
            if hashlib.sha256(source.read_bytes()).hexdigest() != entry.sha256:
                raise FunctionGenerationError(entry.relative_path)
        if _asset_entries(root) != manifest.assets:
            raise FunctionGenerationError("asset set")
    except (FunctionGenerationError, OSError) as exc:
        raise FunctionGenerationError(
            "Candidate source or asset changed after verification; "
            "no Function Worker was committed"
        ) from exc


def candidate_import_guard():
    return stable_import_guard(error_type=FunctionGenerationError)


_candidate_import_guard = candidate_import_guard


def run_candidate_probe(
    *,
    code_root: Path,
    module_names: tuple[str, ...],
    expected_runtime: RuntimeFingerprint,
    timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> CandidateProbeReceipt:
    payload = {
        "code_root": str(Path(code_root).resolve()),
        "module_names": list(module_names),
        "expected_runtime": expected_runtime.to_dict(),
    }
    command = [sys.executable, "-I", str(Path(code_root).resolve() / "orchestrator" / "function_generation.py"), "--probe"]
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
            f"Candidate staging Worker exceeded {timeout_seconds:.1f}s"
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
            "Candidate staging Worker returned no receipt "
            f"(exit={completed.returncode}): {detail[-2000:]}"
        )
    try:
        result = json.loads(result_line)
    except json.JSONDecodeError as exc:
        raise FunctionGenerationError(
            "Candidate staging Worker returned invalid JSON"
        ) from exc
    if completed.returncode != 0 or not result.get("ok"):
        detail = str(result.get("error") or completed.stderr or "candidate rejected")
        probe_traceback = str(result.get("traceback") or "").strip()
        if probe_traceback:
            detail = f"{detail}\n{probe_traceback}"
        raise FunctionGenerationError(
            f"Candidate staging Worker rejected generation: {detail}"
        )
    runtime = RuntimeFingerprint.from_mapping(result["runtime"])
    compare_runtime_fingerprints(expected_runtime, runtime)
    return CandidateProbeReceipt(
        generation_id=str(result["generation_id"]),
        module_names=tuple(str(name) for name in result["module_names"]),
        runtime=runtime,
        probe_pid=int(result["probe_pid"]),
    )


def configured_observers_are_qualified(kernel: Any, generation: VerifiedFunctionGeneration) -> bool:
    return set(declared_observer_modules(kernel.paths)) <= set(generation.manifest.module_names)


def probe_function_generation(
    kernel: Any,
    *,
    module_names: list[str] | tuple[str, ...] | None = None,
    probe_runner=run_candidate_probe,
) -> VerifiedFunctionGeneration:
    """Qualify exact source/assets without importing them into HASHI Core."""

    code_root = Path(kernel.paths.code_root).resolve()
    expected_runtime = getattr(kernel, "runtime_fingerprint", None)
    if not isinstance(expected_runtime, RuntimeFingerprint):
        raise FunctionGenerationError(
            "Core runtime fingerprint is unavailable; Function Worker reboot is disabled"
        )
    requested = set(FUNCTION_GENERATION_ENTRYPOINTS)
    requested.update(spec.module for spec in FUNCTION_MANAGER_SPECS)
    for module in declared_observer_modules(kernel.paths):
        if not is_function_module_name(module):
            raise FunctionGenerationError(f"Observer is outside project Functions: {module}")
        requested.add(module)
    # The shared process's live ``sys.modules`` set is timing-dependent: after Workbench
    # starts it contains shared-service modules that are absent during cold
    # bootstrap. Seeding a Function generation from that set makes /reboot
    # absorb unrelated loaded modules and produces a different artifact from a
    # cold start. Explicit roots plus static closure and the isolated probe are
    # the single deterministic qualification path for both cases.
    if module_names is not None:
        requested.update(module_names)
    initial_manifest = build_source_manifest(
        tuple(sorted(requested, key=function_module_order_key)),
        code_root=code_root,
    )
    receipt = probe_runner(
        code_root=code_root,
        module_names=initial_manifest.module_names,
        expected_runtime=expected_runtime,
    )
    manifest = build_source_manifest(receipt.module_names, code_root=code_root)
    if manifest.generation_id != receipt.generation_id:
        raise FunctionGenerationError(
            "Candidate source/assets differ between Core and staging Worker"
        )
    generation = VerifiedFunctionGeneration(
        code_root=code_root,
        manifest=manifest,
        receipt=receipt,
    )
    generation.verify(expected_runtime)
    return generation


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
        with candidate_import_guard():
            for name in requested:
                importlib.import_module(name)
            validate_function_contract()
        expanded = tuple(discover_loaded_function_modules(code_root=code_root))
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
    parser.error("function_generation is an internal Functions module")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
