import os
import subprocess

import pytest

from orchestrator.config_json import write_config_json
from tools.private_files import protect_private_file


def test_private_file_replaces_broad_access_and_directories_remain_usable(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir()
    path = directory / "evidence.jsonl"
    path.write_text("synthetic evidence\n")
    if os.name == "nt":
        for target in (directory, path):
            subprocess.run(
                ["icacls.exe", str(target), "/grant", "*S-1-1-0:(R)"],
                capture_output=True, check=True,
            )
    protect_private_file(directory)
    protect_private_file(path)
    child = directory / "next.jsonl"
    child.write_text("next record\n")
    assert path.read_text() == "synthetic evidence\n"
    if os.name == "nt":
        for target in (directory, path, child):
            acl = subprocess.run(
                ["icacls.exe", str(target)], capture_output=True,
                text=True, check=True,
            ).stdout
            assert "Everyone:" not in acl and "BUILTIN\\Users:" not in acl
            if target != child:
                assert "(I)" not in acl
    else:
        assert directory.stat().st_mode & 0o777 == 0o700
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL contract")
def test_private_file_allows_an_explicit_distinct_runtime_principal(tmp_path):
    path = tmp_path / "secrets.json"
    path.write_text("{}\n", encoding="utf-8")

    protect_private_file(
        path,
        additional_full_control_sids=("S-1-5-19",),
    )

    acl = subprocess.run(
        ["icacls.exe", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "NT AUTHORITY\\LOCAL SERVICE:(F)" in acl
    assert "Everyone:" not in acl and "BUILTIN\\Users:" not in acl


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL contract")
def test_config_writer_binds_a_cross_account_runtime_principal_on_creation(tmp_path):
    path = tmp_path / "secrets.json"

    write_config_json(
        path,
        {"hashi_remote_shared_token": "test-only"},
        private_full_control_sids=("S-1-5-19",),
    )

    acl = subprocess.run(
        ["icacls.exe", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "NT AUTHORITY\\LOCAL SERVICE:(F)" in acl
    assert "Everyone:" not in acl and "BUILTIN\\Users:" not in acl
