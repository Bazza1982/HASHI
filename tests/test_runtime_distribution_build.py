from pathlib import Path
import json

import pytest

from scripts import compile_enterprise_functions as enterprise
from scripts import prepare_runtime_bootstrap as bootstrap

ROOT = Path(__file__).resolve().parents[1]


def test_bootstrap_refuses_an_existing_environment_and_preserves_it(tmp_path):
    destination = tmp_path / "existing"
    destination.mkdir()
    witness = destination / "preserve.txt"
    witness.write_text("durable witness", encoding="utf-8")
    with pytest.raises(ValueError, match="fresh"):
        bootstrap.prepare_bootstrap(destination)
    assert witness.read_text(encoding="utf-8") == "durable witness"


def test_bootstrap_cannot_nest_under_an_existing_venv(tmp_path):
    environment = tmp_path / "environment"
    environment.mkdir()
    (environment / "pyvenv.cfg").write_text("include-system-site-packages = false\n", encoding="utf-8")
    with pytest.raises(ValueError, match="existing Python"):
        bootstrap.prepare_bootstrap(environment / "candidate")
    assert not (environment / "candidate").exists()


def test_enterprise_default_lock_keeps_the_exact_canonical_hashes(tmp_path):
    output = tmp_path / "functions.lock"
    assert enterprise.compile_profile("", output) == output
    assert output.read_bytes() == (ROOT / "constraints/functions-py312.hashes.lock").read_bytes()


@pytest.mark.parametrize("extra", ["missing-extra", "test", "../voice"])
def test_enterprise_rejects_unknown_or_nonruntime_profiles_before_resolution(tmp_path, extra):
    output = tmp_path / "functions.lock"
    with pytest.raises(ValueError, match="Unknown"):
        enterprise.compile_profile(extra, output)
    assert not output.exists()


def test_package_declares_role_bootstrap_selection_and_publication_inputs():
    package = json.loads((ROOT / "package.json").read_bytes())
    assert {
        "FUNCTION_SOURCE.json", "constraints/functions-py312.hashes.lock",
        "scripts/prepare_runtime_roles.py", "scripts/prepare_runtime_bootstrap.py",
        "scripts/check_runtime_selection.py", "scripts/resolve_launch_runtime.py",
        "scripts/export_function_source.py",
    } <= set(package["files"])

def test_installer_cannot_return_a_core_command_after_native_import_failure(monkeypatch):
    from types import SimpleNamespace
    from scripts import check_runtime_selection as selection

    observed = []

    def checked(_root):
        observed.append("qualified")
        return SimpleNamespace(
            core=SimpleNamespace(fingerprint=SimpleNamespace(executable="qualified-core")),
            functions=SimpleNamespace(fingerprint=object()))

    def failed_import(_root, _runtime):
        observed.append("native-import")
        raise RuntimeError("native import failed")

    monkeypatch.setattr(selection, "checked_selection", checked)
    monkeypatch.setattr(selection, "check_function_imports", failed_import)
    with pytest.raises(RuntimeError, match="native import failed"):
        selection.main(["--core-command", "--import-functions"])
    assert observed == ["qualified", "native-import"]
# HASHI-Fingerprint: hfp_a34a2356edf643a9970c1063ccbf313d
