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


def write_smoke_plan(
    plan_path: Path,
    *,
    socket_path: str,
    host_log_path: str,
    browser_action_log_path: str,
    start_url: str,
    startup_wait_s: float = 3.0,
) -> dict[str, object]:
    plan = {
        "checks": [
            "extension_loaded",
            "service_worker_started",
            "native_host_handshake",
            "socket_alive",
            "ping",
            "active_tab",
            "get_text",
            "screenshot",
        ],
        "artifacts": {
            "socket_path": socket_path,
            "host_log_path": host_log_path,
            "browser_action_log_path": browser_action_log_path,
        },
        "start_url": start_url,
        "startup_wait_s": startup_wait_s,
    }
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    return plan


def validate_harness_artifacts(root_dir: Path) -> dict[str, object]:
    native_host_manifest = next((root_dir / "native_host").glob("*.json"), None)
    required_paths = {
        "extension_manifest": root_dir / "extension" / "manifest.json",
        "extension_service_worker": root_dir / "extension" / "service_worker.js",
        "native_host_manifest": native_host_manifest or (root_dir / "native_host" / "missing.json"),
        "native_host_wrapper": root_dir / "native_host" / "hashi_browser_bridge_test_host.cmd",
        "launch_script": root_dir / "launch_chrome_test.cmd",
        "config": root_dir / "state" / "config.json",
        "smoke_plan": root_dir / "state" / "smoke_plan.json",
        "readme": root_dir / "README.md",
    }

    missing = [name for name, path in required_paths.items() if not path.exists()]
    config = {}
    smoke_plan = {}
    if not missing:
        config = json.loads(required_paths["config"].read_text(encoding="utf-8"))
        smoke_plan = json.loads(required_paths["smoke_plan"].read_text(encoding="utf-8"))

    return {
        "ok": not missing,
        "missing": missing,
        "files": {name: str(path) for name, path in required_paths.items()},
        "config": config,
        "smoke_plan": smoke_plan,
    }
