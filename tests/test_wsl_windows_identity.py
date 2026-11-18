from __future__ import annotations

import os

import pytest

from orchestrator import wsl_windows_identity as identity


def test_mount_proof_uses_the_real_matching_longest_drive_mount(monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "fixture")
    base = r"1 0 0:1 / /mnt/c rw - 9p C:\134 rw,aname=drvfs;path=C:\;uid=1000"
    identity.assert_matching_drvfs_mount("/mnt/c/users/file.txt", base)
    with pytest.raises(identity.WslPathIdentityError, match="matching Windows drive"):
        identity.assert_matching_drvfs_mount("/mnt/c/users/file.txt", base.replace("C:\\134", "D:\\134"))
    overlay = base + "\n2 1 0:2 / /mnt/c/users rw - ext4 /dev/fake rw"
    with pytest.raises(identity.WslPathIdentityError, match="matching Windows drive"):
        identity.assert_matching_drvfs_mount("/mnt/c/users/file.txt", overlay)
    with pytest.raises(identity.WslPathIdentityError):
        identity.assert_matching_drvfs_mount("/home/other/file.txt", base)


@pytest.mark.parametrize("value", [r"\\server\share\x", r"C:\..\private", r"C:\a\..\b", "C:\\x\x00bad"])
def test_windows_mapping_rejects_unbound_or_traversal_path(value):
    with pytest.raises(identity.WslPathIdentityError):
        identity.windows_to_wsl(value)


def test_windows_mapping_keeps_unicode_and_metacharacters():
    assert identity.windows_to_wsl(r"C:\mixed\诗 & $' file.txt") == "/mnt/c/mixed/诗 & $' file.txt"


def _entry() -> dict:
    return {
        "attachment_id": "12345678-1234-1234-1234-123456789012",
        "windows_path": r"C:\picked.txt",
        "target_identity": {"platform": "win32", "device": "12", "inode": "34"},
    }


def test_target_accepts_original_and_in_place_edits_but_rejects_replacement(tmp_path, monkeypatch):
    file = tmp_path / "picked.txt"
    file.write_text("first")
    monkeypatch.setattr(identity, "windows_to_wsl", lambda _: str(file))
    monkeypatch.setattr(identity, "assert_matching_drvfs_mount", lambda _: None)
    monkeypatch.setattr(identity, "_wsl_to_windows", lambda _: r"C:\canonical.txt")
    monkeypatch.setattr(identity, "_windows_file_identities", lambda _: [
        {"device": "12", "inode": "34"}, {"device": "12", "inode": "34"},
    ])
    expected = [{"attachment_id": _entry()["attachment_id"], "accessible_path": str(file.resolve())}]
    assert identity.verify_windows_paths([_entry()]) == expected
    file.write_text("edited contents")
    assert identity.verify_windows_paths([_entry()]) == expected
    # The selected Windows path and the canonical WSL path must both refer to
    # the selected object; different symlink interpretation must not pass.
    monkeypatch.setattr(identity, "_windows_file_identities", lambda _: [
        {"device": "12", "inode": "34"}, {"device": "12", "inode": "35"},
    ])
    with pytest.raises(identity.WslPathIdentityError) as error:
        identity.verify_windows_paths([_entry()])
    assert error.value.code == "attachment_changed"


def test_target_detects_replacement_while_native_identity_is_queried(tmp_path, monkeypatch):
    file = tmp_path / "picked.txt"
    file.write_text("first")
    other = tmp_path / "other.txt"
    other.write_text("second")
    monkeypatch.setattr(identity, "windows_to_wsl", lambda _: str(file))
    monkeypatch.setattr(identity, "assert_matching_drvfs_mount", lambda _: None)
    monkeypatch.setattr(identity, "_wsl_to_windows", lambda _: r"C:\canonical.txt")

    def replace(_):
        os.replace(other, file)
        return [{"device": "12", "inode": "34"}, {"device": "12", "inode": "34"}]

    monkeypatch.setattr(identity, "_windows_file_identities", replace)
    with pytest.raises(identity.WslPathIdentityError) as error:
        identity.verify_windows_paths([_entry()])
    assert error.value.code == "attachment_changed"


def test_target_rejects_invalid_or_missing_identity_without_guesses(monkeypatch):
    monkeypatch.setattr(identity, "windows_to_wsl", lambda _: pytest.fail("must not inspect"))
    for item in ({**_entry(), "target_identity": None},
                 {**_entry(), "target_identity": {"platform": "linux", "device": "12", "inode": "34"}},
                 {**_entry(), "target_identity": {"platform": "win32", "device": "12", "inode": "0"}}):
        with pytest.raises(identity.WslPathIdentityError) as error:
            identity.verify_windows_paths([item])
        assert error.value.code == "attachment_manifest_invalid"
# HASHI-Fingerprint: hfp_68fa65ade56b48528a0cc16baa83004a



def test_wsl_capability_retries_failed_proof_and_uses_new_version(monkeypatch):
    import sys
    from orchestrator import host_identity

    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "linux")
        patch.setenv("WSL_DISTRO_NAME", "fixture")
        patch.setattr(host_identity, "_wsl_host_id", "")
        answers = iter(["", "a" * 64])
        patch.setattr(identity, "windows_host_id", lambda: next(answers))
        first = host_identity.storage_identity()
        second = host_identity.storage_identity()
        assert first["local_path_references"] is False
        assert first["identity_status"] == "unavailable"
        assert second == {
            "version": 2,
            "physical_host_id": "a" * 64,
            "path_namespace": "wsl-windows",
            "local_path_references": True,
            "path_reference_verification": "wsl_drvfs_v1",
            "identity_status": "ready",
        }
        assert host_identity.storage_identity() == second


@pytest.mark.asyncio
async def test_path_verification_api_authenticates_and_returns_only_proof(tmp_path, monkeypatch):
    import json
    import sys
    from types import SimpleNamespace

    from aiohttp.test_utils import TestClient, TestServer
    from orchestrator.workbench_api import WorkbenchApiServer

    config = tmp_path / "agents.json"
    config.write_text(json.dumps({"global": {}, "agents": []}), encoding="utf-8")
    settings = SimpleNamespace(bridge_home=tmp_path, project_root=tmp_path, instance_id="TEST",
                               deployment_profile="personal", authorized_id=1)
    server = WorkbenchApiServer(config_path=config, global_config=settings,
        secrets={"workbench_admin_token": "test-owner-token"}, reconcile_session_runs=False)
    calls = []
    monkeypatch.setattr(identity, "verify_windows_paths", lambda entries: calls.append(entries) or [
        {"attachment_id": entries[0]["attachment_id"], "accessible_path": "/mnt/c/work/file.txt"},
    ])
    async with TestClient(TestServer(server.app)) as client:
        url = "/api/v1/workbench-local-paths/verify"
        payload = {"attachments": [_entry()]}
        anonymous = await client.post(url, json=payload)
        assert anonymous.status == 401
        assert calls == []
        with monkeypatch.context() as patch:
            patch.setattr(sys, "platform", "linux")
            patch.setenv("WSL_DISTRO_NAME", "fixture")
            response = await client.post(url, json=payload, headers={"Authorization": "Bearer test-owner-token"})
            assert response.status == 200
            assert await response.json() == {"ok": True, "attachments": [
                {"attachment_id": _entry()["attachment_id"], "accessible_path": "/mnt/c/work/file.txt"},
            ]}
            invalid = await client.post(url, json={"attachments": [], "other": "ignored"},
                                        headers={"Authorization": "Bearer test-owner-token"})
            assert invalid.status == 400
            assert (await invalid.json())["error_code"] == "attachment_manifest_invalid"
        assert calls == [payload["attachments"]]
