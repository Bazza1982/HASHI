from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
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
                "agent": "WORKBENCH_ONLY_NO_TOKEN",
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
    [profile] = json.loads((tmp_path / "agents.json").read_text())["agents"]

    assert agent.name == "agent"
    assert profile["display_name"] == "智能体"
    assert profile["emoji"] == "🤖"
    assert profile["telegram_token_key"] == "agent"
    assert secrets["agent"] == "WORKBENCH_ONLY_NO_TOKEN"
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


def test_portable_her_keeps_execution_reasoning_but_makes_front_door_stages_fast():
    config = json.loads((TEMPLATES / "agents.json").read_text(encoding="utf-8"))
    her = config["agents"][0]["allowed_backends"][0]["her_v2"]

    assert her["profiles"]["lightweight"]["reasoning"] == "high"
    assert her["stage_reasoning"] == {
        "immediate_response": "none",
        "triage": "none",
    }


def test_portable_launcher_explicitly_grants_all_host_filesystem_drives():
    common = (TEMPLATES / "launcher" / "Common.ps1").read_text(encoding="utf-8")

    assert "Get-PSDrive -PSProvider FileSystem" in common
    assert "HASHI_ADDITIONAL_ACCESS_ROOTS" in common


def test_portable_launcher_reports_real_startup_milestones_not_elapsed_time():
    common = (TEMPLATES / "launcher" / "Common.ps1").read_text(encoding="utf-8")

    assert "$script:HashiStartupTimeoutSeconds = 1800" in common
    assert "$script:WorkbenchStartupTimeoutSeconds = 300" in common
    assert "This may take a few minutes" in common
    assert "Get-HASHIStartupStage" in common
    assert "starting backend initialization" in common
    assert "Initializing HER v2" in common
    assert "Backend API listening on" in common
    assert "Local API is ready" in common
    assert "HASHI is still starting normally" not in common
    assert "仍在正常启动" not in common
    assert "AddSeconds(75)" not in common
    assert "AddSeconds(45)" not in common
    assert "HASHI_WORKBENCH_OBSERVABILITY_DIR" in common
    assert "HASHI_REMOTE_LIVE_ENDPOINTS_PATH" in common
    assert "HASHI_WORKBENCH_URL = \"http://127.0.0.1:$port\"" in common
    assert "HASHI_PORTABLE_STORAGE_PROFILE = 'removable'" not in common
    assert "HASHI_PORTABLE_EXECUTION_MODE = 'local-install'" in common
    assert "HASHI_WINDOWS_NATIVE_ONLY = '1'" in common
    assert "System32\\WindowsPowerShell\\v1.0" in common


def test_portable_launcher_uses_identity_bound_loopback_with_dynamic_ports():
    common = (TEMPLATES / "launcher" / "Common.ps1").read_text(encoding="utf-8")
    tui = (ROOT / "tui.py").read_text(encoding="utf-8")
    instances = (ROOT / "tui" / "instances.py").read_text(encoding="utf-8")

    assert "C:\\HASHI-Portable" in common
    assert "state\\local-endpoint.json" in common
    assert "HASHI Portable Local Endpoint" in common
    assert "Get-FreeLoopbackPort" in common
    assert "TcpListener" in common
    assert "LocalEndpoint.Port" in common
    assert "Set-LocalApiPort" in common
    assert "backend_start_ticks" in common
    assert "portable_instance_id" in common
    assert "launch_nonce" in common
    assert "HASHI_LOCAL_ENDPOINT_FILE" in common
    assert "load_local_endpoint" in tui
    assert "HASHI_WORKBENCH_URL does not match" in tui
    assert "api_host != \"127.0.0.1\"" in instances
    assert "172." not in common


def test_portable_stop_closes_only_owned_local_runtime_and_browser():
    stop = (TEMPLATES / "launcher" / "Stop-HASHI.ps1").read_text(
        encoding="utf-8"
    )

    assert "Get-PortableOwnedProcesses" in stop
    assert "browser-profile" in stop
    assert "Get-CimInstance Win32_Process" in stop
    assert "Get-VerifiedLocalEndpoint" in stop
    assert "Get-OwnedProcess" in stop
    assert "portable-local-stop" in stop
    assert "eject the USB" not in stop
    assert "弹出 USB" not in stop
    assert stop.index("$remaining.Count -gt 0") < stop.index(
        "HASHI has stopped"
    )


def test_portable_full_local_install_is_admin_atomic_verified_and_idempotent():
    common = (TEMPLATES / "launcher" / "Common.ps1").read_text(encoding="utf-8")
    installer = (TEMPLATES / "launcher" / "Install-To-PC.ps1").read_text(
        encoding="utf-8"
    )
    bootstrap = (TEMPLATES / "launcher" / "Bootstrap-Elevated.ps1").read_text(
        encoding="utf-8"
    )
    elevated_entry = (TEMPLATES / "launcher" / "Elevated-Entry.ps1").read_text(
        encoding="utf-8"
    )
    uninstaller = (TEMPLATES / "launcher" / "Uninstall-From-PC.ps1").read_text(
        encoding="utf-8"
    )

    assert "C:\\HASHI-Portable" in common
    assert "-Verb RunAs" in bootstrap
    assert "-Wait" not in bootstrap
    assert "Test-IsAdministrator" in common
    assert "must be started with administrator privileges" in common
    assert "Install-To-PC.ps1" in elevated_entry
    assert "Start-TUI.ps1" in elevated_entry
    assert "-FailureHandledByEntry" in elevated_entry
    assert "Install-LocalCache.ps1" not in elevated_entry
    assert "Use-ExistingLocalCache" not in common
    assert "CommonApplicationData" not in common
    assert "SetEnvironmentVariable" not in common
    assert "setx" not in common.lower()

    assert "Test-IsAdministrator" in installer
    assert "Write-Progress" in installer
    assert "Get-Sha256" in installer
    assert "SHA256SUMS.txt" in installer
    assert "Assert-StaticManifestCoverage" in installer
    assert "C:\\.HASHI-Portable.installing." in installer
    assert "Move-Item -LiteralPath $script:StageRoot -Destination $script:InstallRoot" in installer
    assert ".hashi-local-install.json" in installer
    assert "authoritative_data = 'local:data'" in installer
    assert "complete_copy = $true" in installer
    assert "HASHI is already installed. No files were copied." in installer
    assert "exit 10" in installer
    assert "CreateShortcut" in installer
    assert "启动 HASHI（聊天界面）.lnk" in installer
    assert "启动 HASHI（工作台）.lnk" in installer
    assert "停止 HASHI.lnk" in installer
    assert "Get-InstalledLauncherPath" in installer
    assert "Start_HASHI_TUI.bat" in installer
    assert "Start_HASHI_Workbench.bat" in installer
    assert "Stop_HASHI.bat" in installer
    assert installer.index("Remove-DesktopShortcuts") < installer.index(
        "$shell = New-Object -ComObject WScript.Shell"
    )
    assert "CommonApplicationData" not in installer
    assert "CurrentVersion\\Uninstall" not in installer

    assert "C:\\HASHI-Portable" in uninstaller
    assert "Assert-OwnedLocalInstallation" in uninstaller
    assert "Type REMOVE to continue" in uninstaller
    assert "portable-instance.json" in uninstaller
    assert "authoritative_data" in uninstaller
    assert "ReparsePoint" in uninstaller
    assert "Stop-HASHI.ps1" in uninstaller
    assert "C:\\.HASHI-Portable.removing." in uninstaller
    assert "Move-Item -LiteralPath $script:InstallRoot -Destination $removalRoot" in uninstaller
    assert "Remove-Item -LiteralPath $removalRoot -Recurse -Force" in uninstaller
    assert uninstaller.index(
        "Move-Item -LiteralPath $script:InstallRoot -Destination $removalRoot"
    ) < uninstaller.index("\n    Remove-DesktopShortcuts\n")
    assert "Move-Item -LiteralPath $removalRoot -Destination $script:InstallRoot" not in uninstaller
    assert "Remove-Item -LiteralPath $script:SourceRoot -Recurse" not in uninstaller
    assert "CommonApplicationData" not in uninstaller


def test_portable_local_install_and_uninstall_are_scoped_to_one_random_identity(
    tmp_path,
):
    builder = _load_builder()
    secrets_path = tmp_path / "source-secrets.json"
    secrets_path.write_text(
        json.dumps({"deepseek_api_key": "test-key"}), encoding="utf-8"
    )
    builder.configure_data(tmp_path, secrets_path, allow_missing_key=False)

    identity = json.loads(
        (tmp_path / "data" / "portable-instance.json").read_text(encoding="utf-8")
    )
    assert identity["schema_version"] == 1
    assert identity["product"] == "HASHI Portable Windows x64"
    assert re.fullmatch(r"[0-9a-f]{32}", identity["portable_instance_id"])

    common = (TEMPLATES / "launcher" / "Common.ps1").read_text(encoding="utf-8")
    installer = (TEMPLATES / "launcher" / "Install-To-PC.ps1").read_text(
        encoding="utf-8"
    )
    uninstaller = (TEMPLATES / "launcher" / "Uninstall-From-PC.ps1").read_text(
        encoding="utf-8"
    )
    for source in (common, installer, uninstaller):
        assert "portable-instance.json" in source
        assert "C:\\HASHI-Portable" in source
    assert ".hashi-local-install.json" in installer
    assert "portable_instance_id = $sourceInstanceId" in installer
    assert ".hashi-local-install.json" in uninstaller
    assert "Get-CimInstance Win32_Process" in uninstaller
    assert "Test-PathInsideRoot" in uninstaller
    assert "install_transaction_id" in installer
    assert "bundle_id" in installer
    assert "bundle_id" in uninstaller


def test_portable_setup_guidance_is_bilingual_plain_language_and_actionable(
    tmp_path,
):
    builder = _load_builder()
    common = (TEMPLATES / "launcher" / "Common.ps1").read_text(encoding="utf-8")
    installer = (TEMPLATES / "launcher" / "Install-To-PC.ps1").read_text(
        encoding="utf-8"
    )
    tui = (TEMPLATES / "launcher" / "Start-TUI.ps1").read_text(encoding="utf-8")
    workbench = (TEMPLATES / "launcher" / "Start-Workbench.ps1").read_text(
        encoding="utf-8"
    )
    diagnose = (TEMPLATES / "launcher" / "Diagnose-HASHI.ps1").read_text(
        encoding="utf-8"
    )
    elevated_entry = (TEMPLATES / "launcher" / "Elevated-Entry.ps1").read_text(
        encoding="utf-8"
    )
    install_batch = (TEMPLATES / "安装_HASHI_到本机.bat").read_text(
        encoding="utf-8"
    )

    for english, chinese in (
        ("Checking system requirements", "正在检查系统要求"),
        ("Copying HASHI to the local PC", "正在将 HASHI 复制到本机"),
        ("Verifying the complete local copy", "正在验证完整的本机副本"),
        ("Finishing setup", "正在完成安装"),
    ):
        assert english in installer
        assert chinese in installer
    assert '"[$bounded%]' in installer
    assert "MB of $totalMiB MB" in installer
    assert "$verifyIndex of $($records.Count)" in installer
    assert "HASHI Setup / HASHI 安装" in installer
    assert "Installation log:" in installer
    assert "安装日志：" in installer

    assert "HASHI is ready. Opening the terminal interface" in tui
    assert "HASHI 已就绪。正在打开终端界面" in tui
    assert "HASHI is ready. Opening Workbench" in workbench
    assert "HASHI 已就绪。正在打开 Workbench" in workbench
    assert "HASHI Portable system check" in diagnose
    assert "HASHI Portable 系统检查" in diagnose
    assert "All required checks passed" in diagnose
    assert "所有必要检查均已通过" in diagnose
    assert "Start-HASHIBackend" not in installer
    assert "Press any key to launch HASHI." in elevated_entry
    assert "按任意键启动 HASHI。" in elevated_entry
    assert "Installation failed." in elevated_entry
    assert "HASHI startup failed." in elevated_entry
    assert elevated_entry.index("Wait-ForLaunchKey") < elevated_entry.index(
        "$launcherName = if"
    )
    assert "choice /c" not in install_batch.lower()
    assert "pause" not in install_batch.lower()
    assert "Bootstrap-Elevated.ps1" in install_batch
    assert "-Action Install -Surface TUI" in install_batch

    user_visible = f"{common}\n{installer}"
    for internal_wording in (
        "Installation has no fixed 10-minute cutoff",
        "HASHI local acceleration installer",
        "Extracting small program files",
        "ProgramData",
    ):
        assert internal_wording not in user_visible

    for name in builder.USER_LAUNCHER_FILES:
        assert "chcp 65001" in (TEMPLATES / name).read_text(encoding="utf-8")

    builder.copy_launchers(tmp_path)
    for script in (tmp_path / "launcher").glob("*.ps1"):
        assert script.read_bytes().startswith(b"\xef\xbb\xbf")

    readme_path = tmp_path / builder.README_FILENAME
    assert readme_path.read_bytes().startswith(b"\xef\xbb\xbf")
    readme = readme_path.read_text(encoding="utf-8-sig")
    assert "第一次使用" in readme
    assert "桌面会出现三个中文快捷方式" in readme
    assert "安装_HASHI_到本机.bat" in readme
    assert "从本机卸载_HASHI.bat" in readme
    assert "不需要经过 WSL" in readme or "不会经过 WSL" in readme

    assert set(builder.USER_LAUNCHER_FILES) == {
        "安装_HASHI_到本机.bat",
        "启动_HASHI_聊天界面.bat",
        "启动_HASHI_工作台.bat",
        "停止_HASHI.bat",
        "诊断_HASHI.bat",
        "从本机卸载_HASHI.bat",
    }
    for legacy_name in builder.LEGACY_USER_FILES:
        assert not (tmp_path / legacy_name).exists()


def test_builder_has_one_expanded_bundle_without_split_runtime_payload():
    builder = _load_builder()
    builder_source = (PORTABLE / "build.py").read_text(encoding="utf-8")

    assert not hasattr(builder, "create_local_cache_payload")
    assert "create local acceleration payload" not in builder_source
    assert not (TEMPLATES / "launcher" / "Install-LocalCache.ps1").exists()
    assert not (TEMPLATES / "launcher" / "Uninstall-LocalCache.ps1").exists()
    assert not (TEMPLATES / "launcher" / "Compile-LocalCache.py").exists()
    assert (TEMPLATES / "launcher" / "Install-To-PC.ps1").is_file()
    assert (TEMPLATES / "launcher" / "Uninstall-From-PC.ps1").is_file()


def test_builder_copies_only_git_tracked_allowlisted_source(tmp_path, monkeypatch):
    builder = _load_builder()
    source = tmp_path / "source"
    destination = tmp_path / "portable-app"
    tracked = {
        "main.py": "print('main')\n",
        "tui.py": "print('tui')\n",
        "LICENSE": "MIT\n",
        "exp/__init__.py": "",
        "exp/loader.py": "",
        "exp/asset-packs.json": "{}\n",
        "orchestrator/tracked.py": "TRACKED = True\n",
        "adapters/codex_cli.py": "must be pruned\n",
        "superloops/recordings/tracked-run/state.json": "must be ignored\n",
    }
    for relative, content in tracked.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q", source], check=True)
    subprocess.run(["git", "-C", source, "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            source,
            "-c",
            "user.name=Portable Test",
            "-c",
            "user.email=portable@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )

    untracked = (
        source / "orchestrator" / "local-state.json",
        source / "scripts" / "secrets.json",
        source / "superloops" / "loops" / "private-run" / "state.json",
    )
    for path in untracked:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private\n", encoding="utf-8")

    monkeypatch.setattr(builder, "HASHI_ROOT", source)
    builder.copy_hashi_source(destination)

    assert (destination / "orchestrator" / "tracked.py").is_file()
    assert not (destination / "adapters" / "codex_cli.py").exists()
    assert not (destination / "superloops" / "recordings").exists()
    for path in untracked:
        assert not (destination / path.relative_to(source)).exists()

    builder.require_clean_tracked_worktree(source, label="fixture")
    (source / "orchestrator" / "tracked.py").write_text(
        "TRACKED = False\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="uncommitted tracked changes"):
        builder.require_clean_tracked_worktree(source, label="fixture")


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
    builder = _load_builder()
    for name in builder.USER_LAUNCHER_FILES:
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "%~dp0" in source
        assert "E:\\" not in source
    common = (TEMPLATES / "launcher" / "Common.ps1").read_text(encoding="utf-8")
    assert "$env:HASHI_TUI_ENABLE_API_GATEWAY = '0'" in common
    assert "$env:BRIDGE_HOME = $script:DataRoot" in common
    assert "$env:HASHI_REMOTE_ROOT = $script:DataRoot" in common
    assert "$env:HASHI_OCR_MODEL_ROOT" in common
    assert "'--agents'" in common
    assert "'agent'" in common
    assert "'portable'" not in common


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
    assert "app/hashi/tui/assets/sounds/soft_chat_send.wav" in builder_source
    assert "app/hashi/tui/assets/sounds/soft_chat_receive.wav" in builder_source
    assert '"soft_chat_message_sounds": True' in builder_source
    assert '"windows_native_only": True' in builder_source
    assert '"chinese_user_facing_files": True' in builder_source
