from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
NAGARE_ROOT = ROOT / "nagare"
README = ROOT / "README.md"
PYPROJECT = ROOT / "pyproject.toml"
PACKAGE_JSON = ROOT / "nagare-viz" / "package.json"

REQUIRED_DOCS = [
    ROOT / "docs" / "MIGRATION_FROM_HASHI.md",
    ROOT / "docs" / "HANDLER_GUIDE.md",
    ROOT / "docs" / "ADAPTER_GUIDE.md",
    ROOT / "docs" / "LOGGING.md",
    ROOT / "docs" / "ROUND_TRIP_CONTRACT.md",
    ROOT / "docs" / "NAGARE_RELEASE_CHECKLIST.md",
    ROOT / "docs" / "NAGARE_KNOWN_LIMITATIONS.md",
]

FORBIDDEN_IMPORT_ROOTS = {"flow", "hashi", "tools"}


def test_phase8_required_docs_exist_and_are_non_empty() -> None:
    for path in REQUIRED_DOCS:
        assert path.exists(), f"Missing release doc: {path}"
        assert path.read_text(encoding="utf-8").strip(), f"Empty release doc: {path}"


def test_nagare_package_has_no_forbidden_runtime_imports() -> None:
    for path in NAGARE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots = {node.module.split(".")[0]}
            else:
                continue
            forbidden = roots & FORBIDDEN_IMPORT_ROOTS
            assert not forbidden, f"{path} imports forbidden runtime module(s): {sorted(forbidden)}"


def test_release_metadata_exposes_nagare_cli_and_editor_scripts() -> None:
    pyproject_text = PYPROJECT.read_text(encoding="utf-8")
    package_json_text = PACKAGE_JSON.read_text(encoding="utf-8")
    readme_text = README.read_text(encoding="utf-8")

    assert 'nagare = "nagare.cli:main"' in pyproject_text
    assert '"build": "tsc -b && vite build"' in package_json_text
    assert '"test": "vitest run"' in package_json_text
    assert "Nagare Core And Editor" in readme_text


def test_python_module_and_cli_help_resolve() -> None:
    import_result = subprocess.run(
        [sys.executable, "-c", "import nagare; print(nagare.__all__)"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert import_result.returncode == 0, import_result.stderr
    assert "FlowRunner" in import_result.stdout

    cli_result = subprocess.run(
        [sys.executable, "-m", "nagare.cli", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert cli_result.returncode == 0, cli_result.stderr
    assert "run" in cli_result.stdout
    assert "status" in cli_result.stdout
    assert "api" in cli_result.stdout
