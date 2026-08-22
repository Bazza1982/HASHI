from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from packaging.requirements import Requirement

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]


def _project_metadata() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]


def _requirement_key(value: str) -> tuple[str, str, str]:
    parsed = Requirement(value)
    return (
        parsed.name.lower().replace("_", "-"),
        str(parsed.specifier),
        str(parsed.marker or ""),
    )


def _keys(values: list[str]) -> set[tuple[str, str, str]]:
    return {_requirement_key(value) for value in values}


def _requirements_file(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "-r"))
    ]


def test_standard_and_all_profiles_are_exact_unions():
    project = _project_metadata()
    extras = project["optional-dependencies"]

    assert _keys(extras["standard"]) == _keys(
        extras["media"] + extras["remote"] + extras["tui"]
    )

    declared_feature_groups = (
        "standard",
        "whatsapp",
        "browser",
        "voice",
        "transcription",
        "ocr",
        "kubernetes",
        "postgres",
        "vector",
    )
    expected_all: list[str] = []
    for name in declared_feature_groups:
        expected_all.extend(extras[name])
    assert _keys(extras["all"]) == _keys(expected_all)


def test_standard_requirements_match_core_plus_standard_extra():
    project = _project_metadata()
    expected = _keys(
        project["dependencies"] + project["optional-dependencies"]["standard"]
    )
    actual = _keys(_requirements_file(ROOT / "requirements.txt"))

    assert actual == expected


def test_development_requirements_extend_standard_without_runtime_duplication():
    lines = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
    assert "-r requirements.txt" in lines
    dev_requirements = _requirements_file(ROOT / "requirements-dev.txt")
    test_extra = _project_metadata()["optional-dependencies"]["test"]

    assert _keys(dev_requirements) == _keys(test_extra)


def test_setup_py_is_metadata_free_compatibility_shim():
    setup_text = (ROOT / "setup.py").read_text(encoding="utf-8")

    assert "setup()" in setup_text
    assert "requirements.txt" not in setup_text
    assert "install_requires" not in setup_text
    assert "extras_require" not in setup_text


def test_core_imports_do_not_require_standard_optional_profiles():
    script = r'''
import importlib.abc
import sys

blocked = {
    "PIL", "fitz", "fastapi", "uvicorn", "zeroconf", "cryptography",
    "rich", "textual",
}

class BlockOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".", 1)[0]
        if root in blocked:
            raise ModuleNotFoundError(f"blocked optional dependency: {root}", name=root)
        return None

sys.meta_path.insert(0, BlockOptional())
import adapters.her_v2
import nagare.engine.runner
import orchestrator.flexible_agent_runtime
import orchestrator.scheduler
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
