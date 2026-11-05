import pytest

from orchestrator.runtime_contract import RuntimeContractError, verify_runtime_files
from orchestrator.runtime_installation_builder import file_table
from orchestrator.runtime_release_capsule import _verify_sidecar_files


def test_sidecar_hooks_are_opaque_sealed_resources_and_grant_no_runtime_role(tmp_path):
    root = tmp_path / "sidecar"
    root.mkdir()
    hook = root / "distutils-precedence.pth"
    hook.write_text("import _distutils_hack\n", encoding="utf-8")
    table = file_table(root)
    _verify_sidecar_files(root, table)
    with pytest.raises(RuntimeContractError, match="inject site"):
        verify_runtime_files(root, table, manifest_path=tmp_path / "external.json")
    hook.write_text("import altered_module\n", encoding="utf-8")
    with pytest.raises(RuntimeContractError, match="hash mismatch"):
        _verify_sidecar_files(root, table)


@pytest.mark.parametrize("change", ["added", "removed"])
def test_sidecar_closure_rejects_added_or_removed_files(tmp_path, change):
    root = tmp_path / "sidecar"
    root.mkdir()
    payload = root / "model.bin"
    payload.write_bytes(b"frozen model")
    table = file_table(root)
    if change == "added":
        (root / "unexpected.pyc").write_bytes(b"unlisted code")
    else:
        payload.unlink()
    with pytest.raises(RuntimeContractError, match="closure"):
        _verify_sidecar_files(root, table)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/absolute", "a\\b", "./local"])
def test_sidecar_manifest_cannot_escape_its_own_root(tmp_path, name):
    with pytest.raises(RuntimeContractError, match="path"):
        _verify_sidecar_files(tmp_path, {name: "0" * 64})
# HASHI-Fingerprint: hfp_43480f5c39924faea918fa142830cb77
