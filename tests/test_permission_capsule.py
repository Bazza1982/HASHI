"""Artifact integrity and private Runner instance binding, without starting native hosts."""
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.capsule import qualify_capsule
from orchestrator.permissions.runner import RunnerClient


def artifact(tmp_path):
    files = {
        "python/python.exe": b"qualified executable",
        "python/python313._pth": b"DLLs\nLib\nLib/site-packages\n../app\n",
        "python/Lib/dependency.py": b"VALUE = 1\n",
        "app/orchestrator/permissions/restricted_her_host.py": b"# fixed host\n",
    }
    for name, body in files.items():
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(body)
    manifest = {
        "version": 1, "source_commit": "a" * 40, "python_version": [3, 13, 0],
        "executable": "python/python.exe",
        "files": {name: {"sha256": sha256(body).hexdigest(), "size": len(body)} for name, body in files.items()},
    }
    source = tmp_path / "capsule.json"
    source.write_text(json.dumps(manifest), encoding="utf-8")
    return source, sha256(source.read_bytes()).hexdigest()


def test_capsule_checks_each_import_dependency_and_its_parent_acl(tmp_path):
    source, digest = artifact(tmp_path)
    observed = []
    def qualify(path, **kwargs):
        observed.append(path)
        assert kwargs == {"trusted_sids": ("safety",), "read_only_public": True}
    capsule = qualify_capsule(source, digest, trusted_sids=("safety",), qualifier=qualify)
    assert capsule.executable == tmp_path / "python/python.exe"
    assert tmp_path / "python/Lib" in observed
    assert tmp_path / "python/Lib/dependency.py" in observed
    (tmp_path / "python/Lib/dependency.py").write_bytes(b"VALUE = 2\n")
    with pytest.raises(PermissionError, match="content changed"):
        qualify_capsule(source, digest, trusted_sids=("safety",), qualifier=qualify)


def test_capsule_rejects_added_imports_missing_files_or_manifest_replacement(tmp_path):
    source, digest = artifact(tmp_path)
    def qualify(path, **kwargs):
        return True
    added = tmp_path / "python/Lib/extra.py"
    added.write_text("extra", encoding="utf-8")
    with pytest.raises(PermissionError, match="content changed"):
        qualify_capsule(source, digest, trusted_sids=(), qualifier=qualify)
    added.unlink()
    (tmp_path / "python/Lib/dependency.py").unlink()
    with pytest.raises(PermissionError, match="incomplete"):
        qualify_capsule(source, digest, trusted_sids=(), qualifier=qualify)
    source.write_text("{}", encoding="utf-8")
    with pytest.raises(PermissionError, match="manifest changed"):
        qualify_capsule(source, digest, trusted_sids=(), qualifier=qualify)


@pytest.mark.asyncio
async def test_runner_health_and_prepare_are_bound_to_the_installed_instance():
    calls = []
    class Client:
        async def call_async(self, method, arguments):
            calls.append(method)
            return {
                "state": "ready", "instance_id": "other",
                "identity": {"user_sid": "runner", "elevated": False, "administrator_member": False},
                "safety_sid": "safety", "worker_sid": "worker",
            }
    runner = RunnerClient(Client(), runner_sid="runner", safety_sid="safety", worker_sid="worker",
                          instance_id="instance")
    with pytest.raises(PermissionDenied, match="qualification"):
        await runner.qualify()
    with pytest.raises(PermissionDenied, match="not qualified"):
        await runner.prepare(SimpleNamespace(context=SimpleNamespace(instance_id="instance")))
    assert calls == ["health"]
