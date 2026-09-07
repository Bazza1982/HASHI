"""The process kernel cannot regain product ownership through lazy imports."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from orchestrator.runtime_contract import CORE_SOURCE_PATHS

ROOT = Path(__file__).resolve().parents[1]


def test_product_owners_are_replaceable():
    from orchestrator.function_contract import is_function_module_name

    for module in (
        "orchestrator.flexible_backend_registry",
        "orchestrator.config",
        "orchestrator.model_catalog",
        "orchestrator.activity_digest",
        "orchestrator.ui_language",
        "orchestrator.api_gateway",
        "orchestrator.workbench_api",
        "orchestrator.scheduler",
        "orchestrator.her_v2.request_policy",
        "orchestrator.service_manager",
        "orchestrator.function_worker_host",
        "orchestrator.runtime_app",
    ):
        assert is_function_module_name(module), module


def test_core_import_closure_includes_lazy_dependencies():
    protected = set(CORE_SOURCE_PATHS)
    for relative in protected:
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                import importlib.util

                module = node.module or ""
                if node.level:
                    package = ".".join(Path(relative).parent.parts)
                    module = importlib.util.resolve_name(
                        "." * node.level + module, package
                    )
                modules = [module, *(f"{module}.{alias.name}" for alias in node.names)]
            for module in modules:
                for path in (
                    module.replace(".", "/") + ".py",
                    module.replace(".", "/") + "/__init__.py",
                ):
                    if (ROOT / path).is_file():
                        assert path in protected, (
                            f"{relative}:{node.lineno} imports {path}"
                        )


def test_entrypoint_does_not_load_product_code():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import main; from pathlib import Path; "
            "from orchestrator.runtime_contract import CORE_SOURCE_PATHS; "
            "root = Path.cwd(); "
            "local = {Path(m.__file__).resolve().relative_to(root).as_posix() "
            "for m in list(sys.modules.values()) if getattr(m, '__file__', None) "
            "and Path(m.__file__).resolve().is_relative_to(root)}; "
            "assert local <= set(CORE_SOURCE_PATHS), local - set(CORE_SOURCE_PATHS)",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_core_packages_cannot_smuggle_an_unprotected_initializer():
    protected = set(CORE_SOURCE_PATHS)
    for relative in protected:
        for parent in Path(relative).parents:
            initializer = (parent / "__init__.py").as_posix()
            if parent != Path(".") and (ROOT / initializer).is_file():
                assert initializer in protected, initializer


def test_only_verified_child_bootstrap_can_dynamically_import_product_code():
    for relative in CORE_SOURCE_PATHS:
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = ast.unparse(node.func)
            assert name not in {"exec", "eval", "__import__"}, (relative, node.lineno)
            if name.endswith(".import_module"):
                assert relative == "orchestrator/function_worker_bootstrap.py"
                assert isinstance(
                    node.args[0], ast.Name
                )  # receipt-bound entrypoint, never a hardcoded feature


def test_catalogue_and_ui_changes_do_not_change_core_fingerprint(tmp_path):
    import shutil
    from orchestrator.runtime_contract import core_source_digest

    for relative in CORE_SOURCE_PATHS:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    digest = core_source_digest(tmp_path)
    for relative in (
        "orchestrator/flexible_backend_registry.py",
        "orchestrator/ui_language.py",
    ):
        (tmp_path / relative).write_text("VALUE = 'new product choice'\n")
    assert core_source_digest(tmp_path) == digest
    (tmp_path / "main.py").write_text("CHANGED = True\n")
    assert core_source_digest(tmp_path) != digest
