from types import SimpleNamespace
import json

import pytest

from orchestrator import remote_lifecycle
from orchestrator.startup_manager import StartupManager
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
