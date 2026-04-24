from __future__ import annotations

from pathlib import Path

from tools.browser_bridge_harness import (
    build_extension_bundle,
    create_harness_layout,
    write_chrome_launch_script,
    write_harness_config,
    write_native_host_manifest,
    write_wsl_host_wrapper,
)


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
    host_name: str = "com.hashi.browser_bridge.test",
    extension_name: str = "HASHI Browser Bridge Test",
    start_url: str = "about:blank",
) -> dict[str, str]:
    layout = create_harness_layout(root_dir)

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
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return layout
