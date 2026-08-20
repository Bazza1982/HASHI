from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
NAGARE_ROOT = ROOT / "nagare"

FORBIDDEN_IMPORT_ROOTS = {"flow", "hashi", "tools"}


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
