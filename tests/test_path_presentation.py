from __future__ import annotations

from orchestrator.path_presentation import (
    display_user_path,
    normalize_user_visible_paths,
    path_presentation_policy,
    user_facing_path_style,
)


def test_wsl_paths_are_presented_for_windows_explorer() -> None:
    assert display_user_path(
        "/mnt/c/Users/tester/project/report.md",
        platform_name="windows_wsl",
        distro_name="Ubuntu-22.04",
    ) == r"C:\Users\tester\project\report.md"
    assert display_user_path(
        "/home/tester/project/report.md",
        platform_name="windows_wsl",
        distro_name="Ubuntu-22.04",
    ) == r"\\wsl.localhost\Ubuntu-22.04\home\tester\project\report.md"


def test_native_linux_keeps_native_paths() -> None:
    path = "/home/tester/project/report.md"

    assert display_user_path(path, platform_name="linux") == path
    assert (
        display_user_path("/mnt/c/shared/report.md", platform_name="linux")
        == "/mnt/c/shared/report.md"
    )
    assert normalize_user_visible_paths(
        f"Saved to `{path}`.", platform_name="linux"
    ) == f"Saved to `{path}`."
    assert user_facing_path_style("linux") == "native_posix"


def test_native_windows_normalizes_drive_paths_for_explorer() -> None:
    assert user_facing_path_style("windows_native") == "windows_explorer"
    assert user_facing_path_style("windows_wsl") == "windows_explorer"
    assert user_facing_path_style("macos") == "native_posix"
    assert display_user_path(
        "C:/Users/tester/project/report.md",
        platform_name="windows_native",
    ) == r"C:\Users\tester\project\report.md"
    assert normalize_user_visible_paths(
        "File: `C:/Users/tester/project/report.md`.",
        platform_name="windows_native",
    ) == r"File: `C:\Users\tester\project\report.md`."


def test_visible_reply_rewrites_locations_but_preserves_wsl_commands() -> None:
    raw = """Created [report.md](/home/tester/project/report.md:12).

Output: `/mnt/c/Users/tester/project/result.csv`

File: /home/tester/project/plain.md.

```text
/home/tester/project/notes.md
```

```bash
cd /home/tester/project
python /home/tester/project/run.py
```
"""

    rendered = normalize_user_visible_paths(
        raw,
        platform_name="windows_wsl",
        distro_name="Ubuntu-22.04",
    )

    assert (
        r"`\\wsl.localhost\Ubuntu-22.04\home\tester\project\report.md` (line 12)"
        in rendered
    )
    assert r"`C:\Users\tester\project\result.csv`" in rendered
    assert (
        r"File: `\\wsl.localhost\Ubuntu-22.04\home\tester\project\plain.md`."
        in rendered
    )
    assert (
        r"\\wsl.localhost\Ubuntu-22.04\home\tester\project\notes.md"
        in rendered
    )
    assert "cd /home/tester/project" in rendered
    assert "python /home/tester/project/run.py" in rendered
    assert "](/home/tester/project/report.md" not in rendered


def test_wsl_prompt_contract_separates_execution_and_presentation_paths() -> None:
    policy = path_presentation_policy(
        platform_name="windows_wsl",
        distro_name="Ubuntu-22.04",
    )

    assert "Windows Explorer" in policy
    assert "Keep POSIX/WSL paths for tool calls" in policy
    assert r"\\wsl.localhost\Ubuntu-22.04" in policy
    assert "do not show a raw /home, /mnt" in policy
