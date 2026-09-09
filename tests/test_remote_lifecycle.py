import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.modules.setdefault(
    "zeroconf",
    SimpleNamespace(
        IPVersion=object,
        InterfaceChoice=object,
        ServiceBrowser=object,
        ServiceInfo=object,
        ServiceListener=object,
        Zeroconf=object,
    ),
)

from orchestrator import remote_lifecycle
from orchestrator.startup_manager import StartupManager
from remote.live_endpoints import read_live_endpoints, write_live_endpoints
from remote.main import HashiRemoteApplication, _build_local_capabilities
from remote.peer.base import PeerInfo
from remote.runtime_identity import (
    read_runtime_claim,
    runtime_claim_path,
    validate_launch_context,
    write_runtime_claim,
)


def test_disabled_state_uses_hashi_root_state_path(tmp_path):
    state_path = remote_lifecycle.write_disabled_state(tmp_path, reason="manual test")

    assert state_path == tmp_path / "state" / "remote_disabled.json"
    state = remote_lifecycle.read_disabled_state(tmp_path)
    assert state["disabled"] is True
    assert state["reason"] == "manual test"

    assert remote_lifecycle.clear_disabled_state(tmp_path) is True
    assert remote_lifecycle.read_disabled_state(tmp_path) is None


def test_load_settings_reads_default_on_lifecycle(tmp_path):
    (tmp_path / "remote").mkdir()
    (tmp_path / "remote" / "config.yaml").write_text(
        "\n".join(
            [
                "server:",
                "  port: 8770",
                "  use_tls: false",
                "lifecycle:",
                "  remote_enabled: true",
                "  remote_supervised: false",
                "discovery:",
                "  backend: tailscale",
            ]
        ),
        encoding="utf-8",
    )

    settings = remote_lifecycle.load_settings(tmp_path)

    assert settings.enabled is True
    assert settings.supervised is False
    assert settings.port == 8770
    assert settings.use_tls is False
    assert settings.backend == "tailscale"
    assert settings.disabled_path == tmp_path / "state" / "remote_disabled.json"


def test_remote_advertises_complete_agent_transfer_lifecycle():
    capabilities = _build_local_capabilities(rescue_start_enabled=False)

    assert "agent_move_receive_v1" in capabilities
    assert "agent_transfer_lifecycle_v1" in capabilities


def test_load_settings_defaults_remote_and_supervisor_on(tmp_path):
    settings = remote_lifecycle.load_settings(tmp_path)

    assert settings.enabled is True
    assert settings.supervised is True


def test_load_settings_prefers_instance_registry_port(tmp_path):
    (tmp_path / "remote").mkdir()
    (tmp_path / "remote" / "config.yaml").write_text(
        "\n".join(
            [
                "server:",
                "  port: 8767",
                "  use_tls: false",
                "lifecycle:",
                "  remote_enabled: true",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "HASHI1", "remote_port": 9999}}),
        encoding="utf-8",
    )
    (tmp_path / "instances.json").write_text(
        json.dumps({"instances": {"hashi1": {"instance_id": "HASHI1", "remote_port": 8766}}}),
        encoding="utf-8",
    )

    settings = remote_lifecycle.load_settings(tmp_path)

    assert settings.port == 8766


@pytest.mark.platform
@pytest.mark.skipif(sys.platform == "win32", reason="systemd user-unit contract")
def test_remote_supervisor_info_is_per_instance_and_root_bound(tmp_path, monkeypatch):
    root = tmp_path / "hashi one"
    root.mkdir()
    (root / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "HASHI1"}}),
        encoding="utf-8",
    )
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    unit = config_home / "systemd" / "user" / "hashi-remote-hashi1.service"
    unit.parent.mkdir(parents=True)
    unit.write_text(
        "\n".join(
            [
                "[Service]",
                f'WorkingDirectory="{root}"',
                'ExecStart="python3" -m remote',
            ]
        ),
        encoding="utf-8",
    )

    info = remote_lifecycle.remote_supervisor_info(root)

    assert info.instance_id == "HASHI1"
    assert info.service_name == "hashi-remote-hashi1.service"
    assert info.service_path == unit
    assert info.installed is True
    assert info.declared_root == root.resolve()
    assert info.owns_root is True


@pytest.mark.asyncio
@pytest.mark.platform
@pytest.mark.skipif(sys.platform == "win32", reason="systemd user-unit contract")
async def test_control_remote_supervisor_refuses_unit_owned_by_other_root(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "hashi1"
    other = tmp_path / "hashi2"
    root.mkdir()
    other.mkdir()
    (root / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "HASHI1"}}),
        encoding="utf-8",
    )
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    unit = config_home / "systemd" / "user" / "hashi-remote-hashi1.service"
    unit.parent.mkdir(parents=True)
    unit.write_text(f"[Service]\nWorkingDirectory={other}\n", encoding="utf-8")

    result = await remote_lifecycle.control_remote_supervisor(root, action="start")

    assert result["ok"] is False
    assert result["action"] == "supervisor_root_mismatch"
    assert str(other) in result["reason"]


def test_validate_launch_context_refuses_different_working_hashi_root(tmp_path, monkeypatch):
    code_root = tmp_path / "hashi"
    working_root = tmp_path / "hashi2"
    code_root.mkdir()
    working_root.mkdir()
    (code_root / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "HASHI1"}}),
        encoding="utf-8",
    )
    (working_root / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "HASHI2"}}),
        encoding="utf-8",
    )
    monkeypatch.chdir(working_root)

    with pytest.raises(RuntimeError, match="differs from working HASHI root"):
        validate_launch_context(hashi_root=code_root)


def test_runtime_claim_round_trip(tmp_path):
    claim = write_runtime_claim(
        root=tmp_path,
        instance_id="HASHI1",
        port=8766,
        bind_host="0.0.0.0",
        code_root=tmp_path,
        supervised=False,
    )

    assert runtime_claim_path(tmp_path).exists()
    assert claim["instance_id"] == "HASHI1"
    assert read_runtime_claim(tmp_path)["port"] == 8766


def test_remote_shutdown_removes_own_live_endpoint(tmp_path):
    app = HashiRemoteApplication(hashi_root=tmp_path, use_tls=False)
    app._instance_id = "HASHI1"
    write_runtime_claim(
        root=tmp_path,
        instance_id="HASHI1",
        port=8766,
        bind_host="0.0.0.0",
        code_root=tmp_path,
        supervised=False,
    )
    write_live_endpoints(
        tmp_path,
        [
            PeerInfo(
                instance_id="HASHI1",
                display_name="HASHI1",
                host="127.0.0.1",
                port=8766,
                workbench_port=18800,
                platform="linux",
            ),
            PeerInfo(
                instance_id="HASHI2",
                display_name="HASHI2",
                host="127.0.0.2",
                port=30264,
                workbench_port=18802,
                platform="linux",
            ),
        ],
    )

    app.shutdown()

    assert read_runtime_claim(tmp_path) is None
    endpoints = read_live_endpoints(tmp_path)
    assert "hashi1" not in endpoints
    assert "hashi2" in endpoints


def test_build_child_command_pins_hashi_root(monkeypatch, tmp_path):
    python = tmp_path / "python"
    python.write_text("", encoding="utf-8")
    monkeypatch.setattr(remote_lifecycle, "find_python", lambda root: python)
    settings = remote_lifecycle.RemoteLifecycleSettings(
        root=tmp_path,
        enabled=True,
        supervised=False,
        disabled_path=tmp_path / "state" / "remote_disabled.json",
        port=8766,
        use_tls=False,
        backend="lan",
    )

    cmd = remote_lifecycle.build_child_command(settings)

    assert "--hashi-root" in cmd
    assert str(tmp_path) in cmd


def test_find_python_prefers_current_approved_runtime(monkeypatch, tmp_path):
    current = tmp_path / "approved" / "python3"
    legacy = tmp_path / ".venv" / "bin" / "python3"
    current.parent.mkdir(parents=True)
    legacy.parent.mkdir(parents=True)
    current.write_text("", encoding="utf-8")
    legacy.write_text("", encoding="utf-8")
    monkeypatch.setattr(remote_lifecycle.sys, "executable", str(current))

    assert remote_lifecycle.find_python(tmp_path) == current


def test_build_child_command_supports_separate_portable_control_root(monkeypatch, tmp_path):
    python = tmp_path / "python"
    python.write_text("", encoding="utf-8")
    control_root = tmp_path / "app" / "hashi"
    control_root.mkdir(parents=True)
    monkeypatch.setattr(remote_lifecycle, "find_python", lambda root: python)
    monkeypatch.setenv("HASHI_REMOTE_CONTROL_ROOT", str(control_root))
    settings = remote_lifecycle.RemoteLifecycleSettings(
        root=tmp_path,
        enabled=True,
        supervised=False,
        disabled_path=tmp_path / "state" / "remote_disabled.json",
        port=8766,
        use_tls=False,
        backend="lan",
    )

    cmd = remote_lifecycle.build_child_command(settings)

    assert cmd[cmd.index("--control-hashi-root") + 1] == str(control_root.resolve())


@pytest.mark.asyncio
async def test_find_owned_remote_accepts_claim_port_with_matching_identity(monkeypatch, tmp_path):
    (tmp_path / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "HASHI1"}}),
        encoding="utf-8",
    )
    write_runtime_claim(
        root=tmp_path,
        instance_id="HASHI1",
        port=23456,
        bind_host="0.0.0.0",
        code_root=tmp_path,
        supervised=False,
    )
    settings = remote_lifecycle.RemoteLifecycleSettings(
        root=tmp_path,
        enabled=True,
        supervised=False,
        disabled_path=tmp_path / "state" / "remote_disabled.json",
        port=8766,
        use_tls=False,
        backend="lan",
    )

    monkeypatch.setattr(remote_lifecycle, "local_http_hosts", lambda: ("127.0.0.1",))

    async def fake_health(host, port):
        if port == 23456:
            return {
                "ok": True,
                "instance": {
                    "instance_id": "HASHI1",
                    "runtime_claim": {"root": str(tmp_path), "port": 23456},
                },
            }
        return None

    monkeypatch.setattr(remote_lifecycle, "_fetch_remote_health", fake_health)

    owned = await remote_lifecycle._find_owned_remote(settings)

    assert owned["port"] == 23456


@pytest.mark.asyncio
async def test_find_owned_remote_rejects_wrong_identity(monkeypatch, tmp_path):
    (tmp_path / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "HASHI1"}}),
        encoding="utf-8",
    )
    settings = remote_lifecycle.RemoteLifecycleSettings(
        root=tmp_path,
        enabled=True,
        supervised=False,
        disabled_path=tmp_path / "state" / "remote_disabled.json",
        port=8766,
        use_tls=False,
        backend="lan",
    )

    monkeypatch.setattr(remote_lifecycle, "local_http_hosts", lambda: ("127.0.0.1",))

    async def fake_health(host, port):
        return {
            "ok": True,
            "instance": {
                "instance_id": "HASHI2",
                "runtime_claim": {"root": str(tmp_path), "port": 8766},
            },
        }

    monkeypatch.setattr(remote_lifecycle, "_fetch_remote_health", fake_health)

    assert await remote_lifecycle._find_owned_remote(settings) is None


@pytest.mark.asyncio
async def test_ensure_remote_started_skips_when_explicitly_disabled(tmp_path):
    remote_lifecycle.write_disabled_state(tmp_path, reason="manual /remote off")

    result = await remote_lifecycle.ensure_remote_started(tmp_path)

    assert result["ok"] is False
    assert result["action"] == "skipped"
    assert result["reason"] == "remote explicitly disabled"


@pytest.mark.asyncio
async def test_ensure_remote_started_uses_per_instance_supervisor(monkeypatch, tmp_path):
    (tmp_path / "remote").mkdir()
    (tmp_path / "remote" / "config.yaml").write_text(
        "\n".join(
            [
                "server:",
                "  port: 8766",
                "  use_tls: false",
                "lifecycle:",
                "  remote_enabled: true",
                "  remote_supervised: true",
            ]
        ),
        encoding="utf-8",
    )
    calls = []

    async def fake_control(root, *, action):
        calls.append((root, action))
        return {
            "ok": True,
            "action": "supervisor_started",
            "service_name": "hashi-remote-hashi1.service",
        }

    health_results = iter(
        [
            None,
            None,
            {
                "port": 8766,
                "health": {"ok": True},
                "health_host": "127.0.0.1",
            },
        ]
    )

    async def fake_owned(_settings):
        return next(health_results)

    monkeypatch.setattr(remote_lifecycle, "control_remote_supervisor", fake_control)
    monkeypatch.setattr(remote_lifecycle, "_find_owned_remote", fake_owned)
    monkeypatch.setattr(remote_lifecycle, "_SUPERVISOR_HEALTH_INTERVAL_SECONDS", 0)

    result = await remote_lifecycle.ensure_remote_started(tmp_path)

    assert calls == [(tmp_path, "start")]
    assert result["ok"] is True
    assert result["action"] == "started_supervisor"
    assert result["service_name"] == "hashi-remote-hashi1.service"
    assert result["port"] == 8766


@pytest.mark.asyncio
async def test_ensure_remote_started_activates_missing_supervisor_by_default(
    monkeypatch,
    tmp_path,
):
    calls = []

    async def fake_control(root, *, action):
        calls.append(("control", root, action))
        return {
            "ok": False,
            "action": "supervisor_unavailable",
            "reason": "supervisor is not registered",
            "service_name": "hashi-remote-hashi.service",
        }

    async def fake_activate(root):
        calls.append(("activate", root))
        return {
            "ok": True,
            "action": "supervisor_activated",
            "service_name": "hashi-remote-hashi.service",
        }

    health_results = iter(
        [
            None,
            {
                "port": 8766,
                "health": {"ok": True},
                "health_host": "127.0.0.1",
            },
        ]
    )

    async def fake_owned(_settings):
        return next(health_results)

    monkeypatch.setattr(remote_lifecycle, "control_remote_supervisor", fake_control)
    monkeypatch.setattr(remote_lifecycle, "activate_remote_supervisor", fake_activate)
    monkeypatch.setattr(remote_lifecycle, "_find_owned_remote", fake_owned)
    monkeypatch.setattr(remote_lifecycle, "_SUPERVISOR_HEALTH_INTERVAL_SECONDS", 0)

    result = await remote_lifecycle.ensure_remote_started(tmp_path)

    assert calls == [
        ("control", tmp_path, "start"),
        ("activate", tmp_path),
    ]
    assert result["ok"] is True
    assert result["action"] == "started_supervisor"
    assert result["service_name"] == "hashi-remote-hashi.service"


@pytest.mark.asyncio
async def test_ensure_remote_started_refreshes_healthy_supervisor_registration(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(remote_lifecycle.sys, "platform", "linux")
    write_runtime_claim(
        root=tmp_path,
        instance_id="HASHI",
        port=8766,
        bind_host="0.0.0.0",
        code_root=tmp_path,
        supervised=True,
    )
    activation_calls = []

    async def fake_owned(_settings):
        return {"port": 8766, "health": {"ok": True}, "health_host": "127.0.0.1"}

    async def fake_activate(root):
        activation_calls.append(root)
        return {"ok": True, "action": "supervisor_activated"}

    monkeypatch.setattr(remote_lifecycle, "_find_owned_remote", fake_owned)
    monkeypatch.setattr(remote_lifecycle, "activate_remote_supervisor", fake_activate)

    result = await remote_lifecycle.ensure_remote_started(tmp_path)

    assert result["ok"] is True
    assert result["action"] == "already_running"
    assert result["supervisor_refresh"]["ok"] is True
    assert activation_calls == [tmp_path]


@pytest.mark.asyncio
async def test_ensure_remote_started_falls_back_to_bundled_child(
    monkeypatch,
    tmp_path,
):
    supervisor_failure = {
        "ok": False,
        "action": "supervisor_activation_failed",
        "reason": "systemd user service is unavailable",
        "service_name": "hashi-remote-hashi.service",
    }

    async def fake_control(_root, *, action):
        assert action == "start"
        return {
            "ok": False,
            "action": "supervisor_unavailable",
            "reason": "supervisor is not registered",
            "service_name": "hashi-remote-hashi.service",
        }

    async def fake_activate(_root):
        return supervisor_failure

    async def fake_owned(_settings):
        return None

    async def fake_start_child(settings, *, supervisor_fallback=None):
        assert supervisor_fallback is supervisor_failure
        return {
            "ok": True,
            "action": "started_child_fallback",
            "settings": settings,
            "process": SimpleNamespace(pid=321),
            "supervisor_fallback": supervisor_fallback,
        }

    monkeypatch.setattr(remote_lifecycle, "control_remote_supervisor", fake_control)
    monkeypatch.setattr(remote_lifecycle, "activate_remote_supervisor", fake_activate)
    monkeypatch.setattr(remote_lifecycle, "_find_owned_remote", fake_owned)
    monkeypatch.setattr(remote_lifecycle, "_start_child_remote", fake_start_child)

    result = await remote_lifecycle.ensure_remote_started(tmp_path)

    assert result["ok"] is True
    assert result["action"] == "started_child_fallback"
    assert result["supervisor_fallback"]["reason"] == (
        "systemd user service is unavailable"
    )


@pytest.mark.asyncio
async def test_activate_remote_supervisor_uses_enable_action(monkeypatch, tmp_path):
    monkeypatch.setattr(remote_lifecycle.sys, "platform", "linux")
    helper = tmp_path / "bin" / "hashi-remote-ctl.sh"
    helper.parent.mkdir()
    helper.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    calls = []

    class _Process:
        returncode = 0

        async def communicate(self):
            return b"activated", b""

    async def fake_subprocess(*args, **kwargs):
        calls.append((args, kwargs))
        return _Process()

    monkeypatch.setattr(remote_lifecycle.asyncio, "create_subprocess_exec", fake_subprocess)

    result = await remote_lifecycle.activate_remote_supervisor(tmp_path)

    assert result["ok"] is True
    assert result["action"] == "supervisor_activated"
    assert calls[0][0] == ("bash", str(helper), "enable")
    assert calls[0][1]["cwd"] == str(tmp_path)


@pytest.mark.asyncio
async def test_stop_remote_uses_registered_supervisor(monkeypatch, tmp_path):
    calls = []

    async def fake_owned(_settings):
        return None

    async def fake_control(root, *, action):
        calls.append((root, action))
        return {"ok": True, "action": "supervisor_stopped"}

    monkeypatch.setattr(remote_lifecycle, "_find_owned_remote", fake_owned)
    monkeypatch.setattr(
        remote_lifecycle,
        "remote_supervisor_info",
        lambda _root: SimpleNamespace(installed=True, owns_root=True),
    )
    monkeypatch.setattr(remote_lifecycle, "control_remote_supervisor", fake_control)

    result = await remote_lifecycle.stop_remote(tmp_path)

    assert result["ok"] is True
    assert result["action"] == "supervisor_stopped"
    assert calls == [(tmp_path, "stop")]


@pytest.mark.asyncio
async def test_stop_remote_signals_only_matching_owned_child(monkeypatch, tmp_path):
    (tmp_path / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "HASHI2"}}),
        encoding="utf-8",
    )
    write_runtime_claim(
        root=tmp_path,
        instance_id="HASHI2",
        port=8767,
        bind_host="0.0.0.0",
        code_root=tmp_path,
        supervised=False,
    )
    claim_path = runtime_claim_path(tmp_path)
    claim = json.loads(claim_path.read_text(encoding="utf-8"))
    claim["pid"] = 4321
    claim_path.write_text(json.dumps(claim), encoding="utf-8")
    signals = []
    liveness = iter([True, False])

    async def fake_owned(_settings):
        return {"port": 8767, "health": {"ok": True}, "health_host": "127.0.0.1"}

    monkeypatch.setattr(remote_lifecycle, "_find_owned_remote", fake_owned)
    monkeypatch.setattr(remote_lifecycle.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(remote_lifecycle, "pid_is_alive", lambda _pid: next(liveness))
    monkeypatch.setattr(remote_lifecycle.asyncio, "sleep", AsyncMock())

    result = await remote_lifecycle.stop_remote(tmp_path)

    assert result["ok"] is True
    assert result["action"] == "child_stopped"
    assert signals == [(4321, remote_lifecycle.signal.SIGTERM)]


@pytest.mark.asyncio
async def test_startup_manager_runs_remote_lifecycle(monkeypatch, tmp_path):
    calls = []

    async def fake_ensure(root):
        calls.append(root)
        return {"ok": True, "action": "already_running", "settings": SimpleNamespace(port=8766)}

    monkeypatch.setattr(remote_lifecycle, "ensure_remote_started", fake_ensure)
    kernel = SimpleNamespace(global_config=SimpleNamespace(project_root=tmp_path))
    manager = StartupManager(kernel, console_handler=None)

    await manager._ensure_remote_lifecycle()

    assert calls == [tmp_path]


@pytest.mark.asyncio
async def test_startup_manager_uses_portable_remote_root(monkeypatch, tmp_path):
    calls = []
    portable_root = tmp_path / "data"

    async def fake_ensure(root):
        calls.append(root)
        return {"ok": True, "action": "already_running", "settings": SimpleNamespace(port=8766)}

    monkeypatch.setattr(remote_lifecycle, "ensure_remote_started", fake_ensure)
    monkeypatch.setenv("HASHI_REMOTE_ROOT", str(portable_root))
    kernel = SimpleNamespace(global_config=SimpleNamespace(project_root=tmp_path / "code"))
    manager = StartupManager(kernel, console_handler=None)

    await manager._ensure_remote_lifecycle()

    assert calls == [str(portable_root)]
