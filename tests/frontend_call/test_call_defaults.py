import pytest

from orchestrator.config_json import read_config_json, write_config_json
from orchestrator.frontend_call.config import CallConfig
from orchestrator.frontend_call.contract import CallError


def test_fresh_install_has_audio_and_camera_and_keeps_saved_opt_out(tmp_path):
    path = tmp_path / "call_profiles.json"
    config = CallConfig(path)
    config.initialize()
    context = config.context("owner", "agent")
    assert context["call_ready"] is True
    assert context["camera_available"] is True
    assert {t["kind"] for t in context["targets"]} == {"stt", "tts", "vision"}
    doc = read_config_json(path)
    doc["enabled"] = False
    doc["extension"] = {"retained": True}
    write_config_json(path, doc)
    config.initialize()
    assert config.route("owner", "agent")["call_ready"] is False
    assert read_config_json(path)["extension"] == {"retained": True}


def test_invalid_existing_configuration_is_never_replaced(tmp_path):
    path = tmp_path / "call_profiles.json"
    path.write_bytes(b"{broken")
    CallConfig(path).initialize()
    with pytest.raises(CallError, match="call_configuration_invalid"):
        CallConfig(path).context("owner", "agent")
    assert path.read_bytes() == b"{broken"
