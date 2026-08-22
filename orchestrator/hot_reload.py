from __future__ import annotations

import ast
import inspect
import py_compile
import sys
import tokenize
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

HOT_RELOAD_PREFIXES = ("adapters.", "tools.", "orchestrator.")

# These modules define identity objects already owned by the running process.
# They are not function-layer modules: changing one is incomplete until it has
# an explicit warm-handoff design.  /reboot must never claim that merely
# reloading the module replaced an already-held lock or path identity.
PROCESS_IDENTITY_MODULES = frozenset(
    {
        "orchestrator.instance_lock",
        "orchestrator.pathing",
    }
)

# Only dependency roots that are imported by many consumers belong here.
# The order is centralized so /reboot has one reload contract.  In particular,
# adapter protocol modules must be refreshed before adapters that import their
# constants/classes at module import time.  Otherwise a hot reload can combine
# new consumer source with the previous in-memory protocol module.
FOUNDATION_PHASES = {
    # The HER gateway context imports ToolRegistry at module scope.  Reload
    # schemas, then the registry, then the context so a hot restart cannot
    # retain the pre-change ToolRegistry class after its constructor evolves.
    "tools.schemas": 0,
    "tools.registry": 1,
    "tools.gateway.context": 2,
    "tools.gateway.mcp_stdio": 3,
    "adapters.stream_events": 0,
    "adapters.stream_io": 0,
    "orchestrator.flexible_backend_registry": 0,
    "orchestrator.command_specs": 0,
    # QueuedRequest is imported at module scope by both agent runtimes and
    # request-pipeline consumers. Reload it first so a hot reboot cannot bind
    # a new enqueue method to the previous dataclass constructor.
    "orchestrator.runtime_common": 0,
    "orchestrator.runtime_defaults": 0,
    "orchestrator.workspace_state": 0,
    # Context compaction owns new value types and must refresh before runtime
    # pipeline/command consumers bind its coordinator and exception classes.
    "orchestrator.context_compaction": 3,
    "adapters.base": 1,
    "adapters.her_persona": 1,
    "adapters.xai_oauth_credentials": 1,
    "orchestrator.model_catalog": 1,
    "orchestrator.manager_registry": 1,
    "orchestrator.ticket_manager": 1,
    "adapters.openrouter_api": 2,
    "adapters.xai_imagine": 2,
    # HER v2 dependency order. Reload value types first and the facade only
    # after the provider-neutral runtime graph is coherent.
    "orchestrator.her_v2.models": 0,
    "orchestrator.her_v2.audit": 0,
    "orchestrator.her_v2.progress": 0,
    "orchestrator.her_v2.config": 1,
    "orchestrator.her_v2.retry": 1,
    "orchestrator.her_v2.runtime_configuration": 2,
    "orchestrator.her_v2.lifecycle": 1,
    "orchestrator.her_v2.policy": 1,
    "orchestrator.her_v2.prompt_catalog": 1,
    "orchestrator.her_v2.prompts": 2,
    "orchestrator.her_v2.interfaces": 2,
    "orchestrator.her_v2.ledger": 3,
    "orchestrator.her_v2.learning": 3,
    "orchestrator.her_v2.presentation": 3,
    "orchestrator.her_v2.structured": 3,
    "orchestrator.her_v2.commentary": 4,
    "orchestrator.her_v2.runtime_support": 4,
    "orchestrator.her_v2.runtime_invocation": 5,
    "orchestrator.her_v2.runtime": 6,
    "orchestrator.her_v2": 7,
    "adapters.her_v2_provider": 7,
    "adapters.her_v2": 8,
}


class HotReloadError(RuntimeError):
    pass


def _module_source_path(module: ModuleType) -> Path | None:
    raw_file = getattr(module, "__file__", None)
    if not raw_file:
        return None
    path = Path(raw_file)
    if path.suffix in {".pyc", ".pyo"}:
        source_path = Path(str(path)[:-1])
        if source_path.exists():
            path = source_path
    return path if path.suffix == ".py" and path.is_file() else None


def _ast_parameter_shape(arguments: ast.arguments) -> tuple[tuple[str, str, bool], ...]:
    positional = [*arguments.posonlyargs, *arguments.args]
    first_default = len(positional) - len(arguments.defaults)
    result: list[tuple[str, str, bool]] = []
    for index, argument in enumerate(arguments.posonlyargs):
        result.append(("POSITIONAL_ONLY", argument.arg, index >= first_default))
    for offset, argument in enumerate(arguments.args, start=len(arguments.posonlyargs)):
        result.append(
            ("POSITIONAL_OR_KEYWORD", argument.arg, offset >= first_default)
        )
    if arguments.vararg is not None:
        result.append(("VAR_POSITIONAL", arguments.vararg.arg, False))
    for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults):
        result.append(("KEYWORD_ONLY", argument.arg, default is not None))
    if arguments.kwarg is not None:
        result.append(("VAR_KEYWORD", arguments.kwarg.arg, False))
    return tuple(result)


def _ast_property_accessor(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> str | None:
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Name) and decorator.id == "property":
            return "fget"
        if isinstance(decorator, ast.Attribute):
            accessor = {
                "getter": "fget",
                "setter": "fset",
                "deleter": "fdel",
            }.get(decorator.attr)
            if accessor is not None:
                return accessor
    return None


def _loaded_parameter_shape(value: object) -> tuple[tuple[str, str, bool], ...] | None:
    if isinstance(value, (classmethod, staticmethod)):
        value = value.__func__
    elif isinstance(value, property):
        value = value.fget
    if value is None or not callable(value):
        return None
    try:
        signature = inspect.signature(value, follow_wrapped=False)
    except (TypeError, ValueError):
        return None
    return tuple(
        (
            parameter.kind.name,
            parameter.name,
            parameter.default is not inspect.Parameter.empty,
        )
        for parameter in signature.parameters.values()
    )


def _assigned_names(node: ast.Assign | ast.AnnAssign) -> set[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return {target.id for target in targets if isinstance(target, ast.Name)}


def _is_private_implementation_name(name: str) -> bool:
    """Return whether *name* is private but not a Python data-model hook."""

    return name.startswith("_") and not (
        name.startswith("__") and name.endswith("__")
    )


def detect_loaded_class_interface_changes(
    module_names: list[str],
    *,
    modules: Mapping[str, ModuleType] | None = None,
) -> list[str]:
    """Compare loaded class interfaces with the source about to be reloaded.

    ``importlib.reload`` mutates a process-global module dictionary.  Methods on
    an old live instance keep their old function body but resolve globals from
    that newly populated dictionary.  A targeted reboot is therefore unsafe
    when current source adds a public class member, changes a public callable
    signature, or adds a public dataclass field: a non-target Agent can combine
    old instances with new consumers.  Detect that boundary before any Agent
    is stopped so the caller can widen the restart transaction.

    Function-body-only edits and underscore-private implementation details do
    not count as cross-generation interfaces. Python data-model hooks such as
    ``__init__`` and ``__call__`` remain protected even though they start with
    underscores.
    """

    loaded = modules if modules is not None else sys.modules
    changes: list[str] = []
    for module_name in module_names:
        module = loaded.get(module_name)
        if module is None:
            continue
        source_path = _module_source_path(module)
        if source_path is None:
            continue
        try:
            with tokenize.open(source_path) as source_file:
                tree = ast.parse(source_file.read(), filename=str(source_path))
        except (OSError, SyntaxError, UnicodeError) as exc:
            raise HotReloadError(
                "Hot reload class-interface preflight failed before any Agent "
                f"was stopped: {module_name}: {type(exc).__name__}: {exc}"
            ) from exc

        for class_node in (
            node for node in tree.body if isinstance(node, ast.ClassDef)
        ):
            if _is_private_implementation_name(class_node.name):
                continue
            class_label = f"{module_name}.{class_node.name}"
            loaded_class = getattr(module, class_node.name, None)
            if not inspect.isclass(loaded_class):
                changes.append(f"{class_label} (new class)")
                continue

            source_methods: dict[str, tuple[tuple[str, str, bool], ...]] = {}
            source_properties: dict[
                str, dict[str, tuple[tuple[str, str, bool], ...]]
            ] = {}
            for node in class_node.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                source_shape = _ast_parameter_shape(node.args)
                property_accessor = _ast_property_accessor(node)
                if property_accessor is None:
                    source_methods[node.name] = source_shape
                    continue
                source_properties.setdefault(node.name, {})[
                    property_accessor
                ] = source_shape
            source_fields: set[str] = set()
            for node in class_node.body:
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    source_fields.update(_assigned_names(node))

            dataclass_fields = getattr(loaded_class, "__dataclass_fields__", {})
            class_annotations = getattr(loaded_class, "__annotations__", {})
            for field_name in sorted(source_fields):
                if _is_private_implementation_name(field_name):
                    continue
                if (
                    field_name in loaded_class.__dict__
                    or field_name in dataclass_fields
                    or field_name in class_annotations
                ):
                    continue
                changes.append(f"{class_label}.{field_name} (new field)")

            for method_name, source_shape in source_methods.items():
                if _is_private_implementation_name(method_name):
                    continue
                try:
                    loaded_member = inspect.getattr_static(loaded_class, method_name)
                except AttributeError:
                    changes.append(f"{class_label}.{method_name} (new method)")
                    continue
                if isinstance(loaded_member, property):
                    changes.append(
                        f"{class_label}.{method_name} (descriptor changed)"
                    )
                    continue
                loaded_shape = _loaded_parameter_shape(loaded_member)
                if loaded_shape is not None and loaded_shape != source_shape:
                    changes.append(
                        f"{class_label}.{method_name} (signature changed)"
                    )

            accessor_labels = {
                "fget": "getter",
                "fset": "setter",
                "fdel": "deleter",
            }
            for property_name, source_accessors in source_properties.items():
                if _is_private_implementation_name(property_name):
                    continue
                try:
                    loaded_member = inspect.getattr_static(
                        loaded_class, property_name
                    )
                except AttributeError:
                    changes.append(f"{class_label}.{property_name} (new property)")
                    continue
                if not isinstance(loaded_member, property):
                    changes.append(
                        f"{class_label}.{property_name} (descriptor changed)"
                    )
                    continue
                for accessor_name, source_shape in source_accessors.items():
                    accessor_label = accessor_labels[accessor_name]
                    loaded_accessor = getattr(loaded_member, accessor_name)
                    if loaded_accessor is None:
                        changes.append(
                            f"{class_label}.{property_name}.{accessor_label} "
                            "(new accessor)"
                        )
                        continue
                    loaded_shape = _loaded_parameter_shape(loaded_accessor)
                    if loaded_shape is not None and loaded_shape != source_shape:
                        changes.append(
                            f"{class_label}.{property_name}.{accessor_label} "
                            "(signature changed)"
                        )
    return changes


def module_reload_key(name: str) -> tuple[int, str]:
    if name in FOUNDATION_PHASES:
        return (FOUNDATION_PHASES[name], name)
    if name.startswith(("adapters.", "tools.")):
        return (3, name)
    if "_runtime" in name:
        return (5, name)
    return (4, name)


def discover_loaded_project_modules(
    modules: Mapping[str, ModuleType] | None = None,
    *,
    code_root: Path | None = None,
) -> list[str]:
    loaded = modules if modules is not None else sys.modules
    root = Path(code_root).resolve() if code_root is not None else None

    def is_reloadable_project_module(name: str) -> bool:
        if name in PROCESS_IDENTITY_MODULES:
            return False
        if not any(name.startswith(prefix) for prefix in HOT_RELOAD_PREFIXES):
            return False
        if root is None:
            return True
        module = loaded.get(name)
        raw_file = getattr(module, "__file__", None) if module is not None else None
        if not raw_file:
            return False
        path = Path(raw_file)
        if path.suffix in {".pyc", ".pyo"}:
            path = Path(str(path)[:-1])
        # A long-running process may still retain a module from a branch that
        # has since been switched away. importlib.reload() cannot reload that
        # stale object once its source file is gone, so exclude it up front.
        if not path.is_file():
            return False
        try:
            path.resolve().relative_to(root)
        except (OSError, ValueError):
            return False
        return True

    return sorted(
        (name for name in list(loaded) if is_reloadable_project_module(name)),
        key=module_reload_key,
    )


def preflight_module_sources(
    module_names: list[str],
    *,
    code_root: Path,
    modules: Mapping[str, ModuleType] | None = None,
) -> list[Path]:
    """Compile every loaded project source before any live module is mutated."""
    loaded = modules if modules is not None else sys.modules
    root = Path(code_root).resolve()
    checked: list[Path] = []
    failures: list[str] = []
    seen: set[Path] = set()
    for name in module_names:
        module = loaded.get(name)
        raw_file = getattr(module, "__file__", None) if module is not None else None
        if not raw_file:
            continue
        path = Path(raw_file)
        if path.suffix in {".pyc", ".pyo"}:
            source_path = Path(str(path)[:-1])
            if source_path.exists():
                path = source_path
        if path.suffix != ".py" or not path.exists():
            continue
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            py_compile.compile(str(resolved), doraise=True)
            checked.append(resolved)
        except py_compile.PyCompileError as exc:
            failures.append(f"{name}: {exc.msg}")
    if failures:
        raise HotReloadError(
            "Hot reload preflight failed; no agents were stopped:\n"
            + "\n".join(failures)
        )
    return checked
