import json
from pathlib import Path

import pytest

from orchestrator.media_runtime import configured_media_executable
from tui.audio import _player_command


def publish(home, executable):
    config = home / "state/platform/media.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"schema_version": 1, "executables": {"ffplay": str(executable)}}))
    return config


def test_explicit_tui_launch_home_wins_over_another_instance_environment(tmp_path, monkeypatch):
    home = tmp_path / "launch"
    player = tmp_path / "ffplay.exe"
    player.write_bytes(b"test executable choice")
    config = publish(home, player)
    before = config.read_bytes()
    monkeypatch.setenv("BRIDGE_HOME", str(tmp_path / "other-instance"))
    command = _player_command(tmp_path / "a path with spaces.ogg", bridge_home=home)
    assert command[0] == str(player)
    assert command[-1] == str(tmp_path / "a path with spaces.ogg")
    assert config.read_bytes() == before


def test_unavailable_configured_player_fails_closed_without_a_display_fallback(tmp_path):
    config = publish(tmp_path, tmp_path / "missing.exe")
    before = config.read_bytes()
    with pytest.raises(ValueError, match="unavailable"):
        configured_media_executable("ffplay", bridge_home=tmp_path)
    assert config.read_bytes() == before


def test_absent_platform_config_is_not_created_by_read(tmp_path):
    assert configured_media_executable("ffmpeg", bridge_home=tmp_path) is None
    assert not (tmp_path / "state").exists()
