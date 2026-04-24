from __future__ import annotations

import json
import re
import shutil
from pathlib import Path


HOST_NAME_PATTERN = re.compile(r'const HOST_NAME = "[^"]+";')
EXTENSION_NAME_PATTERN = re.compile(r'"name":\s*"[^"]+"')


def rewrite_service_worker_host_name(content: str, host_name: str) -> str:
    return HOST_NAME_PATTERN.sub(f'const HOST_NAME = "{host_name}";', content, count=1)


def rewrite_manifest_name(content: str, extension_name: str) -> str:
    return EXTENSION_NAME_PATTERN.sub(f'"name": "{extension_name}"', content, count=1)


def build_extension_bundle(
    source_dir: Path,
    target_dir: Path,
    *,
    host_name: str,
    extension_name: str,
) -> dict[str, str]:
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest_content = (source_dir / "manifest.json").read_text(encoding="utf-8")
    service_worker_content = (source_dir / "service_worker.js").read_text(encoding="utf-8")

    (target_dir / "manifest.json").write_text(
        rewrite_manifest_name(manifest_content, extension_name) + "\n",
        encoding="utf-8",
    )
    (target_dir / "service_worker.js").write_text(
        rewrite_service_worker_host_name(service_worker_content, host_name),
        encoding="utf-8",
    )

    return {
        "manifest": str(target_dir / "manifest.json"),
        "service_worker": str(target_dir / "service_worker.js"),
    }


def write_native_host_manifest(
    manifest_path: Path,
    *,
    host_name: str,
    host_command_path: str,
    allowed_origins: list[str],
) -> dict[str, object]:
    manifest = {
        "name": host_name,
        "description": f"{host_name} native host",
        "path": host_command_path,
        "type": "stdio",
        "allowed_origins": allowed_origins,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=4) + "\n", encoding="utf-8")
    return manifest


def create_harness_layout(root_dir: Path) -> dict[str, str]:
    if root_dir.exists():
        shutil.rmtree(root_dir)
    (root_dir / "extension").mkdir(parents=True, exist_ok=True)
    (root_dir / "native_host").mkdir(parents=True, exist_ok=True)
    (root_dir / "logs").mkdir(parents=True, exist_ok=True)
    (root_dir / "state").mkdir(parents=True, exist_ok=True)
    return {
        "root": str(root_dir),
        "extension_dir": str(root_dir / "extension"),
        "native_host_dir": str(root_dir / "native_host"),
        "logs_dir": str(root_dir / "logs"),
        "state_dir": str(root_dir / "state"),
    }


def write_harness_config(
    config_path: Path,
    *,
    chrome_exe: str,
    user_data_dir: str,
    extension_dir: str,
    native_host_manifest_path: str,
    socket_path: str,
    log_path: str,
) -> dict[str, str]:
    config = {
        "chrome_exe": chrome_exe,
        "user_data_dir": user_data_dir,
        "extension_dir": extension_dir,
        "native_host_manifest_path": native_host_manifest_path,
        "socket_path": socket_path,
        "log_path": log_path,
    }
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return config


def write_chrome_launch_script(
    script_path: Path,
    *,
    chrome_exe: str,
    user_data_dir: str,
    extension_dir: str,
    start_url: str = "about:blank",
) -> str:
    script = (
        "@echo off\n"
        f"\"{chrome_exe}\" "
        f"--user-data-dir=\"{user_data_dir}\" "
        "--no-first-run --no-default-browser-check "
        f"--load-extension=\"{extension_dir}\" "
        f"\"{start_url}\"\n"
    )
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script, encoding="utf-8")
    return script


def write_wsl_host_wrapper(
    script_path: Path,
    *,
    distro_name: str,
    repo_root: str,
    socket_path: str,
    log_path: str,
) -> str:
    script = (
        "@echo off\n"
        "C:\\Windows\\System32\\wsl.exe "
        f"-d {distro_name} bash -lc "
        f"\"cd '{repo_root}' && /usr/bin/env python3 "
        f"'{repo_root}/tools/browser_native_host.py' --stdio "
        f"--socket {socket_path} --log-file '{log_path}'\"\n"
    )
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script, encoding="utf-8")
    return script
