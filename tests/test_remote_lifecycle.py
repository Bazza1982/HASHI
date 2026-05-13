from types import SimpleNamespace
import json

import pytest

from orchestrator import remote_lifecycle
from orchestrator.startup_manager import StartupManager


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
