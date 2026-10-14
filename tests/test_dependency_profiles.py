from __future__ import annotations

import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from scripts.build_python_release import build_python_release

ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def _project_metadata() -> dict:
    return _pyproject()["project"]


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


def test_uv_cannot_implicitly_manage_the_runtime_environment():
    assert _pyproject()["tool"]["uv"]["managed"] is False


def test_python_release_build_uses_an_isolated_committed_export(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    metadata = source / "hashi_bridge.egg-info" / "PKG-INFO"
    metadata.parent.mkdir()
    metadata.write_text(
        "Metadata-Version: 2.4\nName: hashi-bridge\nVersion: runtime-baseline\n",
        encoding="utf-8",
    )
    output = tmp_path / "dist"
    calls: list[tuple[tuple[str, ...], Path]] = []

    def run(command, *, cwd, check):
        command = tuple(str(part) for part in command)
        working_directory = Path(cwd).resolve()
        calls.append((command, working_directory))
        assert check is True
        if command[:3] == ("git", "archive", "--format=tar"):
            archive_path = Path(command[command.index("--output") + 1])
            payload = tmp_path / "tracked-pyproject.toml"
            payload.write_text("[build-system]\n", encoding="utf-8")
            with tarfile.open(archive_path, "w") as archive:
                archive.add(payload, arcname="pyproject.toml")
            return
        assert command[:3] == (sys.executable, "-m", "build")
        assert working_directory != source.resolve()
        generated = working_directory / "hashi_bridge.egg-info" / "PKG-INFO"
        generated.parent.mkdir()
        generated.write_text("Version: candidate\n", encoding="utf-8")
        output.mkdir(parents=True, exist_ok=True)
        (output / "hashi_bridge.whl").write_bytes(b"wheel")

    artifacts = build_python_release(source, output, runner=run)

    assert metadata.read_text(encoding="utf-8").endswith(
        "Version: runtime-baseline\n"
    )
    assert artifacts == (output / "hashi_bridge.whl",)
    assert calls[0][0][-1] == "HEAD"
    assert calls[1][1] != source.resolve()


def test_release_checklist_never_builds_python_packages_in_runtime_checkout():
    checklist = (ROOT / "docs" / "RELEASE_CHECKLIST.md").read_text(
        encoding="utf-8"
    )

    assert "python scripts/build_python_release.py" in checklist
    assert "`python -m build`" not in checklist


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


def test_transcription_native_packages_have_a_separate_pinned_runtime():
    helper_input = _keys(
        _requirements_file(ROOT / "requirements-transcription.txt")
    )
    helper_lock = _keys(
        _requirements_file(ROOT / "constraints" / "transcription-py312.lock")
    )
    standard_input = _keys(_requirements_file(ROOT / "requirements.txt"))
    standard_lock = _keys(
        _requirements_file(ROOT / "constraints" / "standard-py312.lock")
    )

    required = {
        _requirement_key("faster-whisper==1.2.1"),
        _requirement_key("ctranslate2==4.7.1"),
        _requirement_key("av==17.0.0"),
    }
    assert helper_input == required
    assert required <= helper_lock
    assert not {key[0] for key in required} & {key[0] for key in standard_input}
    assert not {key[0] for key in required} & {key[0] for key in standard_lock}
