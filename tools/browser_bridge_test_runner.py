from __future__ import annotations

from pathlib import Path

from tools.browser_bridge_harness import (
    build_extension_bundle,
    create_harness_layout,
    validate_harness_artifacts,
    write_chrome_launch_script,
    write_harness_config,
    write_native_host_manifest,
    write_smoke_plan,
    write_wsl_host_wrapper,
)
from tools.browser_bridge_smoke_runner import write_smoke_command_plan


def materialize_option_d_test_harness(
    root_dir: Path,
    *,
    source_extension_dir: Path,
    chrome_exe: str,
    windows_user_data_dir: str,
    windows_extension_dir: str,
    windows_native_host_manifest_path: str,
    windows_host_command_path: str,
    repo_root: str,
    distro_name: str,
    socket_path: str,
    log_path: str,
    browser_action_log_path: str | None = None,
    host_name: str = "com.hashi.browser_bridge.test",
    extension_name: str = "HASHI Browser Bridge Test",
    start_url: str = "about:blank",
) -> dict[str, object]:
    layout = create_harness_layout(root_dir)
    browser_action_log_path = browser_action_log_path or str(root_dir / "logs" / "browser_action_audit.jsonl")

    build_extension_bundle(
        source_extension_dir,
        Path(layout["extension_dir"]),
        host_name=host_name,
        extension_name=extension_name,
    )
    write_native_host_manifest(
        Path(layout["native_host_dir"]) / f"{host_name}.json",
        host_name=host_name,
        host_command_path=windows_host_command_path,
        allowed_origins=[],
    )
    write_harness_config(
        Path(layout["state_dir"]) / "config.json",
        chrome_exe=chrome_exe,
        user_data_dir=windows_user_data_dir,
        extension_dir=windows_extension_dir,
        native_host_manifest_path=windows_native_host_manifest_path,
        socket_path=socket_path,
        log_path=log_path,
    )
    write_chrome_launch_script(
        root_dir / "launch_chrome_test.cmd",
        chrome_exe=chrome_exe,
        user_data_dir=windows_user_data_dir,
        extension_dir=windows_extension_dir,
        start_url=start_url,
    )
    write_wsl_host_wrapper(
        Path(layout["native_host_dir"]) / "hashi_browser_bridge_test_host.cmd",
        distro_name=distro_name,
        repo_root=repo_root,
        socket_path=socket_path,
        log_path=log_path,
    )
    write_smoke_plan(
        Path(layout["state_dir"]) / "smoke_plan.json",
        socket_path=socket_path,
        host_log_path=log_path,
        browser_action_log_path=browser_action_log_path,
        start_url=start_url,
    )
    (root_dir / "README.md").write_text(
        "\n".join(
            [
                "# Browser Bridge Test Harness",
                "",
                f"- Host name: `{host_name}`",
                f"- Source extension: `{source_extension_dir}`",
                f"- Chrome executable: `{chrome_exe}`",
                f"- Windows profile dir: `{windows_user_data_dir}`",
                f"- Socket path: `{socket_path}`",
                f"- Log path: `{log_path}`",
                f"- Browser action audit log: `{browser_action_log_path}`",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    smoke_commands = write_smoke_command_plan(root_dir, repo_root=Path(repo_root))
    return {
        "layout": layout,
        "validation": validate_harness_artifacts(root_dir),
        "smoke_commands": smoke_commands,
    }
