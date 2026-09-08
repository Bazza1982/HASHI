from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "mac" / "prepare_usb.sh"


def test_macos_portable_builder_has_a_fail_closed_publication_boundary() -> None:
    source = BUILDER.read_text(encoding="utf-8")

    assert "git -C \"$SOURCE_ROOT\" archive --format=tar HEAD" in source
    assert "status --porcelain=v1 --untracked-files=normal" in source
    assert "[ ! -e \"$TARGET\" ]" in source
    assert "shasum -a 256" in source
    assert "--proto '=https'" in source
    assert "--only-binary=:all:" in source
    assert "scripts/check_runtime_contract.py" in source
    assert '"$(uname -s)" = "Darwin"' in source

    assert "rsync" not in source
    assert "HASHI9" not in source
    assert "pip install --upgrade" not in source
    assert "workspaces/hashiko" not in source


def test_macos_portable_builder_pins_the_official_apple_silicon_python_asset() -> None:
    source = BUILDER.read_text(encoding="utf-8")

    assert (
        'PYTHON_SHA256_AARCH64="'
        "377234f346fce41b6d3112b5ead89cb6af2d5596244f9edc1a739065770dde1f"
        '"'
    ) in source
    assert "aarch64-apple-darwin-install_only_stripped.tar.gz" not in source
    assert 'pbs_arch="aarch64"' in source
    assert "portable image currently supports Apple Silicon only" in source
    assert 'pbs_arch="x86_64"' not in source


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is unavailable")
def test_macos_portable_builder_has_valid_bash_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", BUILDER.relative_to(ROOT).as_posix()],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
