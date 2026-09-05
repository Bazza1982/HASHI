from __future__ import annotations

import importlib.util
import json
import shutil
import sys
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
    assert [item["engine"] for item in agent.allowed_backends] == ["her-v2"]
    assert {
        item["engine"] for item in manager.get_her_v2_provider_options()
    } == {"deepseek-api", "openai-compatible-api"}
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
