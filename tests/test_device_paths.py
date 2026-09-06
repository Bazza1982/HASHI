from __future__ import annotations

import pytest

from tools.device_paths import (
    DevicePathError,
    resolve_device_path,
    resolve_device_path_arguments,
    windows_to_wsl_path,
    wsl_to_windows_path,
)


def test_windows_wsl_round_trip_preserves_spaces_unicode_and_onedrive():
    windows = (
        r"C:\Users\thene\OneDrive - The University Of Newcastle"
        r"\个人资料\RCA evidence.pdf"
    )

    wsl = windows_to_wsl_path(windows)

    assert wsl == (
        "/mnt/c/Users/thene/OneDrive - The University Of Newcastle/"
        "个人资料/RCA evidence.pdf"
    )
    assert wsl_to_windows_path(wsl) == windows


def test_non_mounted_wsl_path_requires_and_uses_explicit_distribution(monkeypatch):
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    with pytest.raises(DevicePathError, match="distribution identity"):
        wsl_to_windows_path("/home/lily/output.txt", distro="")

    assert wsl_to_windows_path(
        "/home/lily/output.txt",
        distro="Ubuntu-24.04",
    ) == r"\\wsl.localhost\Ubuntu-24.04\home\lily\output.txt"


def test_resolver_accepts_file_outside_repo_when_authorized_root_contains_it():
    file_path = r"C:\Users\thene\OneDrive - University\资料\证据.pdf"

    resolved = resolve_device_path(
        file_path,
        authorized_roots=[r"C:\Users\thene\OneDrive - University"],
        target_platform="windows",
    )

    assert resolved.target_path == file_path
    assert resolved.authorized_root.endswith("OneDrive - University")


def test_resolver_rejects_traversal_outside_authorized_root():
    with pytest.raises(DevicePathError, match="outside the authorized roots"):
        resolve_device_path(
            r"C:\Users\thene\Projects\HASHI3\..\..\secret.txt",
            authorized_roots=[r"C:\Users\thene\Projects\HASHI3"],
            target_platform="windows",
        )


def test_argument_resolver_handles_nested_browser_session_uploads():
    result = resolve_device_path_arguments(
        {
            "steps": [
                {
                    "action": "upload",
                    "file_path": "/mnt/c/Users/thene/OneDrive/资料/input.txt",
                }
            ],
            "_authorized_roots": ["/mnt/c/Users/thene/OneDrive"],
        },
        target_platform="windows",
        require_inputs_exist=False,
    )

    assert result == {
        "steps": [
            {
                "action": "upload",
                "file_path": r"C:\Users\thene\OneDrive\资料\input.txt",
            }
        ]
    }


def test_unc_wsl_share_round_trip_is_distribution_scoped():
    unc = r"\\wsl.localhost\Ubuntu-22.04\home\lily\résumé.pdf"

    assert windows_to_wsl_path(unc, distro="Ubuntu-22.04") == (
        "/home/lily/résumé.pdf"
    )
    assert windows_to_wsl_path(unc, distro="Debian") is None
