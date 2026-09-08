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
from orchestrator.runtime_contract import (
    CORE_SOURCE_PATHS,
    core_source_digest,
    load_runtime_policy,
    locked_standard_dependencies,
)

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


def test_portable_builder_uses_the_canonical_python_runtime_distribution():
    builder = _load_builder()
    policy = load_runtime_policy(ROOT)
    python_asset = builder.ASSETS["python"]

    assert builder.PYTHON_VERSION == policy.python_text
    assert builder.PYTHON_BUILD_DATE == policy.portable_build_date
    assert "astral-sh/python-build-standalone" in python_asset.url
    assert policy.python_text in python_asset.filename
    assert policy.portable_build_date in python_asset.filename
    assert python_asset.filename.endswith("install_only_stripped.tar.gz")
    assert "python.org/ftp/python" not in python_asset.url
    assert "node" not in builder.ASSETS


def test_portable_builder_uses_host_7zip_when_available(tmp_path, monkeypatch):
    builder = _load_builder()
    host_7zip = tmp_path / "host" / "7z.exe"
    monkeypatch.setattr(
        builder.shutil,
        "which",
        lambda command: str(host_7zip) if command == "7z" else None,
    )
    monkeypatch.setattr(
        builder,
        "download",
        lambda *_args, **_kwargs: pytest.fail("host 7-Zip must avoid download"),
    )

    assert builder.resolve_7zip(tmp_path / "cache", tmp_path / "build") == host_7zip


def test_portable_builder_bootstraps_pinned_7zip_on_windows(tmp_path, monkeypatch):
    builder = _load_builder()
    installer = tmp_path / builder.ASSETS["7zip"].filename
    installer.write_bytes(b"pinned-msi")
    commands = []

    monkeypatch.setattr(builder.shutil, "which", lambda _command: None)
    monkeypatch.setattr(builder.sys, "platform", "win32")
    monkeypatch.setattr(builder, "download", lambda asset, _cache: installer)

    def fake_run(command):
        commands.append(command)
        target = Path(command[-1].removeprefix("TARGETDIR="))
        executable = target / "Files" / "7-Zip" / "7z.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"7z")

    monkeypatch.setattr(builder, "run", fake_run)

    executable = builder.resolve_7zip(tmp_path / "cache", tmp_path / "build")

    assert executable == tmp_path / "build" / "7zip" / "Files" / "7-Zip" / "7z.exe"
    assert commands == [
        [
            "msiexec.exe",
            "/a",
            str(installer),
            "/qn",
            f"TARGETDIR={tmp_path / 'build' / '7zip'}",
        ]
    ]
    assert builder.ASSETS["7zip"].url.startswith(
        "https://github.com/ip7z/7zip/releases/download/26.03/"
    )
    assert re.fullmatch(r"[0-9a-f]{64}", builder.ASSETS["7zip"].sha256)


def test_bundled_runtime_check_cannot_add_source_bytecode(tmp_path, monkeypatch):
    builder = _load_builder()
    runtime = tmp_path / "runtime"
    app_hashi = tmp_path / "app" / "hashi"
    commands = []
    monkeypatch.setattr(builder, "run", lambda command: commands.append(command))

    builder.validate_bundled_runtime_contract(runtime, app_hashi)

    assert commands == [
        [
            str(runtime / "python.exe"),
            "-B",
            str(app_hashi / "scripts" / "check_runtime_contract.py"),
            "--code-root",
            str(app_hashi),
            "--json",
        ]
    ]

    bytecode = app_hashi / "orchestrator" / "__pycache__" / "runtime_contract.pyc"
    bytecode.parent.mkdir(parents=True)
    bytecode.write_bytes(b"generated")
    with pytest.raises(RuntimeError, match="forbidden source bytecode"):
        builder.validate_bundled_runtime_contract(runtime, app_hashi)


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
    assert "HASHI_REMOTE_LIVE_ENDPOINTS_PATH" in common
    assert 'HASHI_WORKBENCH_URL = "http://127.0.0.1:$port"' in common
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
    assert 'api_host != "127.0.0.1"' in instances
    assert "172." not in common


def test_portable_stop_closes_only_owned_local_runtime_and_browser():
    stop = (TEMPLATES / "launcher" / "Stop-HASHI.ps1").read_text(encoding="utf-8")

    assert "Get-PortableOwnedProcesses" in stop
    assert "browser-profile" in stop
    assert "Get-CimInstance Win32_Process" in stop
    assert "Get-VerifiedLocalEndpoint" in stop
    assert "Get-OwnedProcess" in stop
    assert "portable-local-stop" in stop
    assert "eject the USB" not in stop
    assert "弹出 USB" not in stop
    assert stop.index("$remaining.Count -gt 0") < stop.index("HASHI has stopped")


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
    assert (
        "Move-Item -LiteralPath $script:StageRoot -Destination $script:InstallRoot"
        in installer
    )
    assert ".hashi-local-install.json" in installer
    assert "authoritative_data = 'local:data'" in installer
    assert "complete_copy = $true" in installer
    assert "The same bundle is already installed. No files were copied." in installer
    assert "exit 10" in installer
    assert "CreateShortcut" in installer
    assert "Start HASHI.lnk" in installer
    assert "Stop HASHI.lnk" in installer
    assert "Start HASHI Workbench.lnk" not in installer
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
    assert (
        "Move-Item -LiteralPath $script:InstallRoot -Destination $removalRoot"
        in uninstaller
    )
    assert "Remove-Item -LiteralPath $removalRoot -Recurse -Force" in uninstaller
    assert uninstaller.index(
        "Move-Item -LiteralPath $script:InstallRoot -Destination $removalRoot"
    ) < uninstaller.index("\n    Remove-DesktopShortcuts\n")
    assert (
        "Move-Item -LiteralPath $removalRoot -Destination $script:InstallRoot"
        not in uninstaller
    )
    assert "Remove-Item -LiteralPath $script:SourceRoot -Recurse" not in uninstaller
    assert "CommonApplicationData" not in uninstaller


def test_portable_local_install_and_uninstall_are_scoped_to_one_random_identity(
    tmp_path,
):
    builder = _load_builder()
    builder.configure_data(tmp_path, private_deepseek_key="test-key")

    identity = json.loads(
        (tmp_path / "data" / "portable-instance.json").read_text(encoding="utf-8")
    )
    assert identity["schema_version"] == 2
    assert identity["product"] == "HASHI Portable Windows x64"
    assert identity["provisioning_state"] == "unprovisioned"
    assert identity["portable_instance_id"] is None
    assert identity["identity_lineage_id"] is None

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
    assert "portable_instance_id = [string]$stagedIdentity.portable_instance_id" in installer
    assert "identity_lineage_id = [string]$stagedIdentity.identity_lineage_id" in installer
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
    diagnose = (TEMPLATES / "launcher" / "Diagnose-HASHI.ps1").read_text(
        encoding="utf-8"
    )
    elevated_entry = (TEMPLATES / "launcher" / "Elevated-Entry.ps1").read_text(
        encoding="utf-8"
    )
    install_batch = (TEMPLATES / "Install_HASHI_On_This_PC.bat").read_text(
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
        "$launcherName = 'Start-TUI.ps1'"
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

    for name in (
        "Start_HASHI_TUI.bat",
        "Install_HASHI_On_This_PC.bat",
        "Uninstall_HASHI_From_This_PC.bat",
        "Stop_HASHI.bat",
        "Diagnose_HASHI.bat",
    ):
        assert "chcp 65001" in (TEMPLATES / name).read_text(encoding="utf-8")

    source_scripts = list((TEMPLATES / "launcher").glob("*.ps1"))
    assert source_scripts
    assert all(
        script.read_bytes().startswith(b"\xef\xbb\xbf") for script in source_scripts
    )

    builder.copy_launchers(tmp_path)
    for script in (tmp_path / "launcher").glob("*.ps1"):
        assert script.read_bytes().startswith(b"\xef\xbb\xbf")

    readme_path = tmp_path / "PORTABLE_README.txt"
    assert readme_path.read_bytes().startswith(b"\xef\xbb\xbf")
    readme = readme_path.read_text(encoding="utf-8-sig")
    assert "Quick start / 快速开始" in readme
    assert "If setup cannot complete / 如果安装无法完成" in readme


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
        "superloops/loops/tracked-run/state.json": "must be pruned\n",
        "flow/workflows/library/builtin.yaml": "must ship\n",
        "skills/library-pick/SKILL.md": "must be pruned\n",
        "scripts/hashi_remote_watchdog.py": "HASHI2 must not ship\n",
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
    assert not (destination / "superloops" / "loops").exists()
    assert (destination / "flow/workflows/library/builtin.yaml").is_file()
    assert not (destination / "skills" / "library-pick").exists()
    assert not (destination / "scripts" / "hashi_remote_watchdog.py").exists()
    for path in untracked:
        assert not (destination / path.relative_to(source)).exists()

    builder.require_clean_tracked_worktree(source, label="fixture")
    (source / "orchestrator" / "tracked.py").write_text(
        "TRACKED = False\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="uncommitted tracked changes"):
        builder.require_clean_tracked_worktree(source, label="fixture")


def test_builder_copies_the_real_runtime_contract_inputs(tmp_path):
    builder = _load_builder()
    destination = tmp_path / "portable-app"

    builder.copy_hashi_source(destination)

    source_policy = load_runtime_policy(ROOT)
    copied_policy = load_runtime_policy(destination)
    assert copied_policy == source_policy
    required = tuple(
        dict.fromkeys(
            (
                "__main__.py",
                "pyproject.toml",
                source_policy.standard_lock,
                *CORE_SOURCE_PATHS,
            )
        )
    )
    for relative in required:
        assert (destination / relative).read_bytes() == (ROOT / relative).read_bytes()
    assert core_source_digest(destination) == core_source_digest(ROOT)
    builder.validate_portable_runtime_inputs(destination)


def test_portable_dependency_generation_matches_the_runtime_standard_lock():
    builder = _load_builder()

    builder.validate_portable_dependency_generation(
        source_root=ROOT,
        portable_root=PORTABLE,
    )


def test_portable_dependency_generation_rejects_a_drifted_lock(tmp_path):
    builder = _load_builder()
    source = tmp_path / "source"
    portable = source / "packaging" / "portable_windows"
    standard = source / "constraints" / "standard-py312.lock"
    portable.mkdir(parents=True)
    standard.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "pyproject.toml", source / "pyproject.toml")
    shutil.copy2(ROOT / "requirements.txt", source / "requirements.txt")
    shutil.copy2(ROOT / "constraints" / "standard-py312.lock", standard)
    shutil.copy2(PORTABLE / "requirements.in", portable / "requirements.in")
    lock = (PORTABLE / "requirements.lock").read_text(encoding="utf-8")
    policy = load_runtime_policy(source)
    expected = locked_standard_dependencies(source, policy)["aiohttp"]
    lock = lock.replace(f"aiohttp=={expected}", "aiohttp==0.0.0", 1)
    (portable / "requirements.lock").write_text(lock, encoding="utf-8")

    with pytest.raises(
        RuntimeError,
        match=rf"aiohttp: portable=0\.0\.0, standard={re.escape(expected)}",
    ):
        builder.validate_portable_dependency_generation(
            source_root=source,
            portable_root=portable,
        )


def test_portable_dependency_generation_rejects_a_drifted_direct_input(tmp_path):
    builder = _load_builder()
    source = tmp_path / "source"
    portable = source / "packaging" / "portable_windows"
    standard = source / "constraints" / "standard-py312.lock"
    portable.mkdir(parents=True)
    standard.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "pyproject.toml", source / "pyproject.toml")
    shutil.copy2(ROOT / "requirements.txt", source / "requirements.txt")
    shutil.copy2(ROOT / "constraints" / "standard-py312.lock", standard)
    requirements = (PORTABLE / "requirements.in").read_text(encoding="utf-8")
    requirements = requirements.replace("edge-tts==7.2.7", "edge-tts==0.0.0", 1)
    (portable / "requirements.in").write_text(requirements, encoding="utf-8")
    shutil.copy2(PORTABLE / "requirements.lock", portable / "requirements.lock")

    with pytest.raises(
        RuntimeError,
        match=r"edge-tts: lock=7\.2\.7, input=0\.0\.0",
    ):
        builder.validate_portable_dependency_generation(
            source_root=source,
            portable_root=portable,
        )


def test_builder_rejects_wrong_or_changed_source_identity(tmp_path):
    builder = _load_builder()
    source = tmp_path / "source"
    source.mkdir()
    (source / "tracked.txt").write_text("first\n", encoding="utf-8")
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
            "first",
        ],
        check=True,
    )

    initial = builder.require_expected_source_identity(source)
    assert (
        builder.require_expected_source_identity(
            source,
            expected_revision=initial.revision,
            expected_tree=initial.tree,
        )
        == initial
    )
    with pytest.raises(RuntimeError, match="expected revision"):
        builder.require_expected_source_identity(
            source,
            expected_revision="0" * 40,
            expected_tree=initial.tree,
        )
    with pytest.raises(RuntimeError, match="expected tree"):
        builder.require_expected_source_identity(
            source,
            expected_revision=initial.revision,
            expected_tree="0" * 40,
        )

    (source / "tracked.txt").write_text("second\n", encoding="utf-8")
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
            "second",
        ],
        check=True,
    )
    with pytest.raises(RuntimeError, match="changed while the image was building"):
        builder.require_unchanged_source_identity(source, initial)


def test_portable_dependency_lock_keeps_requested_compact_capabilities():
    lock = (PORTABLE / "requirements.lock").read_text(encoding="utf-8")
    policy = load_runtime_policy(ROOT)
    standard = locked_standard_dependencies(ROOT, policy)
    for name in ("pymupdf", "textual", "zeroconf"):
        assert f"{name}=={standard[name]}" in lock
    for required in (
        "edge-tts==7.2.7",
        "playwright==1.58.0",
        "psutil==7.2.2",
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
    assert "'--agents'" in common
    assert "'agent'" in common
    assert "'portable'" not in common


def test_builder_enforces_capacity_and_prunes_cli_adaptors():
    builder = _load_builder()

    assert builder.MAX_IMAGE_BYTES == 957_000_000
    assert builder.CAPACITY_CHECK_CLUSTER_BYTES == 32 * 1024
    assert builder.PAIRING_TOKEN_TTL_SECONDS == 604800
    assert set(builder.ROOT_SOURCE_FILES) == {
        "__main__.py",
        "main.py",
        "tui.py",
        "pyproject.toml",
        "LICENSE",
    }
    assert builder.RUNTIME_POLICY_FILES == (load_runtime_policy(ROOT).standard_lock,)
    assert "exp/loader.py" in builder.ROOT_PACKAGE_FILES
    assert "veritas" in builder.SOURCE_DIRS
    for name in ("codex", "claude", "gemini", "grok"):
        assert f"adapters/{name}_cli.py" in builder.PRUNED_SOURCE_PATHS
    for name in ("codex", "claude", "gemini"):
        assert f"skills/{name}" in builder.PRUNED_SOURCE_PATHS
    for name in (
        "dual_brain_context.py",
        "generate_agent_behavior_audit.py",
        "gitwatch.py",
        "hashi_remote_watchdog.py",
        "monitor_hashi1.py",
        "patrol_errors.py",
        "wiki_organise.py",
    ):
        assert f"scripts/{name}" in builder.PRUNED_SOURCE_PATHS

    remote_config = (TEMPLATES / "remote-config.yaml").read_text(encoding="utf-8")
    assert "lan_mode: false" in remote_config
    assert "pairing_auto_approve: true" in remote_config

    builder_source = (PORTABLE / "build.py").read_text(encoding="utf-8")
    assert "build_workbench" not in builder_source
    assert "--workbench-root" not in builder_source
    assert "runtime/node" in builder_source
    assert "app/workbench" in builder_source
    assert "app/hashi/tui/assets/sounds/soft_chat_send.wav" in builder_source
    assert "app/hashi/tui/assets/sounds/soft_chat_receive.wav" in builder_source
    assert '"soft_chat_message_sounds": True' in builder_source
    assert '"windows_native_only": True' in builder_source


def test_portable_bundle_does_not_ship_or_launch_retired_workbench():
    builder = _load_builder()
    common = (TEMPLATES / "launcher" / "Common.ps1").read_text(encoding="utf-8")
    installer = (TEMPLATES / "launcher" / "Install-To-PC.ps1").read_text(
        encoding="utf-8"
    )
    readme = (PORTABLE / "README.md").read_text(encoding="utf-8")

    assert not hasattr(builder, "build_workbench")
    assert not hasattr(builder, "install_node")
    assert not any((PORTABLE / "sharp_runtime").glob("*"))
    assert not (TEMPLATES / "Start_HASHI_Workbench.bat").exists()
    assert not (TEMPLATES / "launcher" / "Start-Workbench.ps1").exists()
    assert "Start-Workbench" not in common
    assert "Start HASHI Workbench.lnk" not in installer
    assert "retired Workbench frontend and Node server are not included" in readme


def test_public_portable_data_has_no_identity_or_credentials(tmp_path):
    builder = _load_builder()

    builder.configure_data(tmp_path, private_deepseek_key=None)

    identity = json.loads(
        (tmp_path / "data" / "portable-instance.json").read_text(encoding="utf-8")
    )
    secrets = json.loads(
        (tmp_path / "data" / "secrets.json").read_text(encoding="utf-8")
    )
    assert identity == {
        "schema_version": 2,
        "product": "HASHI Portable Windows x64",
        "provisioning_state": "unprovisioned",
        "portable_instance_id": None,
        "identity_lineage_id": None,
        "created_at_utc": None,
    }
    assert secrets["deepseek_api_key"] == ""
    assert secrets["workbench_admin_token"] == ""
    assert secrets["hashi_remote_shared_token"] == ""


def test_private_portable_finalization_injects_only_named_deepseek_key(tmp_path):
    builder = _load_builder()

    builder.configure_data(tmp_path, private_deepseek_key="private-deepseek")

    secrets = json.loads(
        (tmp_path / "data" / "secrets.json").read_text(encoding="utf-8")
    )
    assert secrets["deepseek_api_key"] == "private-deepseek"
    assert re.fullmatch(r"[A-Za-z0-9_-]{32,}", secrets["workbench_admin_token"])
    assert re.fullmatch(r"[A-Za-z0-9_-]{48,}", secrets["hashi_remote_shared_token"])
    assert "dashscope_api_key" not in secrets
    assert "openrouter_key" not in secrets


def test_portable_build_cli_keeps_private_key_out_of_process_arguments(tmp_path):
    builder = _load_builder()
    key_file = tmp_path / "deepseek.key"
    key_file.write_text("secret-value\n", encoding="utf-8")

    public = builder.parse_args([])
    private = builder.parse_args(["--private-deepseek-key-file", str(key_file)])

    assert public.private_deepseek_key_file is None
    assert private.private_deepseek_key_file == key_file
    assert not hasattr(public, "secrets")
    assert not hasattr(public, "allow_missing_deepseek_key")


def test_portable_installer_has_update_rollback_lineage_and_language_contract():
    installer = (TEMPLATES / "launcher" / "Install-To-PC.ps1").read_text(
        encoding="utf-8"
    )
    rollback = (TEMPLATES / "launcher" / "Rollback-Previous.ps1").read_text(
        encoding="utf-8"
    )
    common = (TEMPLATES / "launcher" / "Common.ps1").read_text(encoding="utf-8")
    elevated = (TEMPLATES / "launcher" / "Elevated-Entry.ps1").read_text(
        encoding="utf-8"
    )

    assert "[string]$InstallRoot = 'C:\\HASHI-Portable'" in installer
    assert "identity_lineage_id" in installer
    assert "portable_instance_id" in installer
    assert "Preserve-LocalData" in installer
    assert "authoritative_data = 'local:data'" in installer
    assert ".previous" in installer
    assert "Restore-PreviousInstallation" in installer
    assert "same bundle is already installed" in installer
    assert "Select-InstallLanguage" in installer
    assert "ui_language" in installer
    assert "ui_language.json" in installer
    assert "Update" in elevated
    assert "Rollback" in elevated
    assert "Rollback-Previous.ps1" in elevated
    assert "identity_lineage_id" in rollback
    assert "Move-Item -LiteralPath $script:PreviousRoot" in rollback
    assert "@((Get-Item" not in rollback
    assert re.search(
        r"foreach \(\$item in @\(\s*"
        r"Get-Item -LiteralPath \$Root -Force\s*"
        r"Get-ChildItem -LiteralPath \$Root -Recurse -Force\s*"
        r"\)\)",
        rollback,
    )
    assert "HASHI_PORTABLE_LANGUAGE" in common


def test_portable_launcher_set_includes_explicit_update_and_rollback_entries():
    for name, action in (
        ("Update_HASHI_On_This_PC.bat", "-Action Update"),
        ("Rollback_HASHI_On_This_PC.bat", "-Action Rollback"),
    ):
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "chcp 65001" in source
        assert "%~dp0" in source
        assert action in source
