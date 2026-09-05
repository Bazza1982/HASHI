from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import zipfile
from pathlib import Path

import yaml

from orchestrator.config import ConfigManager
from orchestrator.flexible_backend_manager import FlexibleBackendManager

ROOT = Path(__file__).resolve().parents[1]
PORTABLE = ROOT / "packaging" / "portable_windows"
TEMPLATES = PORTABLE / "templates"


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "hashi_portable_windows_builder", PORTABLE / "build.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_portable_profile_has_one_her_engine_and_configurable_regional_providers(
    tmp_path,
):
    shutil.copy2(TEMPLATES / "agents.json", tmp_path / "agents.json")
    workspace = tmp_path / "workspaces" / "portable"
    workspace.mkdir(parents=True)
    shutil.copy2(TEMPLATES / "agent.md", workspace / "agent.md")
    (tmp_path / "secrets.json").write_text(
        json.dumps(
            {
                "authorized_telegram_id": 0,
                "portable": "WORKBENCH_ONLY_NO_TOKEN",
                "deepseek_api_key": "test-key",
                "dashscope_api_key": "test-qwen-key",
            }
        ),
        encoding="utf-8",
    )

    global_config, [agent], secrets = ConfigManager(
        tmp_path / "agents.json",
        tmp_path / "secrets.json",
        bridge_home=tmp_path,
    ).load()
    manager = FlexibleBackendManager(agent, global_config, secrets)

    assert agent.active_backend == "her-v2"
    assert agent.default_mode == "fixed"
    assert agent.access_scope == "drive"
    assert global_config.canonical_audit["buffer_flush_timeout_seconds"] == 120
    assert [item["engine"] for item in agent.allowed_backends] == ["her-v2"]
    assert {item["engine"] for item in manager.get_her_v2_provider_options()} == {
        "deepseek-api",
        "openai-compatible-api",
    }
    providers = global_config.her_providers["providers"]
    assert providers["deepseek"]["base_url"] == "https://api.deepseek.com/v1"
    assert providers["qwen"]["base_url"] == (
        "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )


def test_portable_remote_is_one_click_with_seven_day_tokens():
    config = yaml.safe_load((TEMPLATES / "remote-config.yaml").read_text())

    assert config["security"]["lan_mode"] is False
    assert config["security"]["pairing_auto_approve"] is True
    assert config["security"]["pairing_token_ttl_seconds"] == 604800
    assert config["discovery"]["backend"] == "lan"
    assert config["lifecycle"] == {
        "remote_enabled": True,
        "remote_supervised": False,
    }


def test_portable_launcher_explicitly_grants_all_host_filesystem_drives():
    common = (TEMPLATES / "launcher" / "Common.ps1").read_text(encoding="utf-8")

    assert "Get-PSDrive -PSProvider FileSystem" in common
    assert "HASHI_ADDITIONAL_ACCESS_ROOTS" in common


def test_portable_launcher_allows_slow_usb_cold_start_and_reports_progress():
    common = (TEMPLATES / "launcher" / "Common.ps1").read_text(encoding="utf-8")

    assert "$script:HashiStartupTimeoutSeconds = 1800" in common
    assert "A slow PC or USB fallback can take several minutes" in common
    assert "Still starting HASHI..." in common
    assert "AddSeconds(75)" not in common


def test_portable_local_acceleration_is_admin_atomic_progressive_and_optional():
    common = (TEMPLATES / "launcher" / "Common.ps1").read_text(encoding="utf-8")
    installer = (TEMPLATES / "launcher" / "Install-LocalCache.ps1").read_text(
        encoding="utf-8"
    )
    uninstaller = (TEMPLATES / "launcher" / "Uninstall-LocalCache.ps1").read_text(
        encoding="utf-8"
    )

    assert "Ensure-LocalAccelerationCache" in common
    assert "CommonApplicationData" in common
    assert "-Verb RunAs" in common
    assert "HASHI_PORTABLE_SKIP_LOCAL_CACHE" in common
    assert "Continuing safely from the expanded USB copy" in common
    assert "SetEnvironmentVariable" not in common
    assert "setx" not in common.lower()

    assert "Test-IsAdministrator" in installer
    assert "Write-Progress" in installer
    assert "Get-Sha256WithProgress" in installer
    assert "Move-Item -LiteralPath $stage -Destination $finalRoot" in installer
    assert "HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall" in installer
    assert "AddMinutes(10)" not in installer
    assert "deadline" not in installer.lower()
    assert "usb:data" in installer

    assert "CommonApplicationData" in uninstaller
    assert "USB data" in uninstaller
    assert "HASHIPortableLocalAcceleration" in uninstaller
    assert "\\data" not in uninstaller.lower()


def test_builder_consolidates_small_cache_files_but_keeps_large_usb_sources(tmp_path):
    builder = _load_builder()
    small = tmp_path / "app" / "hashi" / "main.py"
    small.parent.mkdir(parents=True)
    small.write_text("print('portable')\n", encoding="utf-8")
    other = tmp_path / "runtime" / "python" / "python.exe"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"small-runtime")
    large = tmp_path / "runtime" / "bin" / "ffmpeg.exe"
    large.parent.mkdir(parents=True)
    large.write_bytes(b"x" * builder.LOCAL_CACHE_ARCHIVE_MAX_FILE_BYTES)

    manifest = builder.create_local_cache_payload(tmp_path)
    stored = json.loads(
        (tmp_path / "install" / builder.LOCAL_CACHE_MANIFEST).read_text(
            encoding="utf-8"
        )
    )
    records = {record["path"]: record for record in stored["files"]}
    with zipfile.ZipFile(tmp_path / stored["archive"]["path"]) as archive:
        archived = set(archive.namelist())

    assert manifest == stored
    assert records["app/hashi/main.py"]["delivery"] == "archive"
    assert records["runtime/python/python.exe"]["delivery"] == "archive"
    assert records["runtime/bin/ffmpeg.exe"]["delivery"] == "direct"
    assert "app/hashi/main.py" in archived
    assert "runtime/python/python.exe" in archived
    assert "runtime/bin/ffmpeg.exe" not in archived
    assert stored["expanded_usb_fallback"] is True
    assert stored["administrator_required"] is True
    assert stored["authoritative_data"] == "usb:data"
    assert len(stored["bundle_id"]) == 64
    assert stored["cache_key"] == stored["bundle_id"][:20]


def test_portable_dependency_lock_keeps_requested_compact_capabilities():
    lock = (PORTABLE / "requirements.lock").read_text(encoding="utf-8")
    for required in (
        "playwright==1.58.0",
        "psutil==7.2.2",
        "pymupdf==1.27.2.2",
        "textual==8.1.1",
        "zeroconf==0.148.0",
    ):
        assert required in lock
    for excluded in (
        "faster-whisper==",
        "numpy==",
        "onnxruntime==",
        "paddleocr==",
        "paddlepaddle==",
    ):
        assert excluded not in lock


def test_portable_launchers_are_drive_relative_and_gateway_stays_disabled():
    for name in (
        "Start_HASHI_TUI.bat",
        "Start_HASHI_Workbench.bat",
        "Install_HASHI_On_This_PC.bat",
        "Uninstall_HASHI_From_This_PC.bat",
        "Stop_HASHI.bat",
        "Diagnose_HASHI.bat",
    ):
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "%~dp0" in source
        assert "E:\\" not in source
    common = (TEMPLATES / "launcher" / "Common.ps1").read_text(encoding="utf-8")
    assert "$env:HASHI_TUI_ENABLE_API_GATEWAY = '0'" in common
    assert "$env:BRIDGE_HOME = $script:DataRoot" in common
    assert "$env:HASHI_REMOTE_ROOT = $script:DataRoot" in common
    assert "$env:HASHI_OCR_MODEL_ROOT" in common


def test_builder_enforces_capacity_and_prunes_cli_adaptors():
    builder = _load_builder()

    assert builder.MAX_IMAGE_BYTES == 957_000_000
    assert builder.CAPACITY_CHECK_CLUSTER_BYTES == 32 * 1024
    assert builder.PAIRING_TOKEN_TTL_SECONDS == 604800
    assert builder.LOCAL_CACHE_ARCHIVE_MAX_FILE_BYTES == 512 * 1024
    assert set(builder.ROOT_SOURCE_FILES) == {"main.py", "tui.py", "LICENSE"}
    assert "exp/loader.py" in builder.ROOT_PACKAGE_FILES
    assert "veritas" in builder.SOURCE_DIRS
    for name in ("codex", "claude", "gemini", "grok"):
        assert f"adapters/{name}_cli.py" in builder.PRUNED_SOURCE_PATHS
    for name in ("codex", "claude", "gemini"):
        assert f"skills/{name}" in builder.PRUNED_SOURCE_PATHS

    remote_config = (TEMPLATES / "remote-config.yaml").read_text(encoding="utf-8")
    assert "lan_mode: false" in remote_config
    assert "pairing_auto_approve: true" in remote_config

    builder_source = (PORTABLE / "build.py").read_text(encoding="utf-8")
    assert "createRequire(import.meta.url)" in builder_source
