import os
import subprocess

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
