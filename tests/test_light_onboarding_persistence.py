from __future__ import annotations

import json

import pytest

from orchestrator.config_json import read_config_json
from tui import light_onboarding
from tui.onboarding import write_config


def _legacy_bytes(value: dict) -> bytes:
    rendered = json.dumps(value, ensure_ascii=False, indent=2).replace("\n", "\r\n")
    return b"\xef\xbb\xbf" + (rendered + "\r\n").encode("utf-8")


def _sample() -> dict:
    return {
        "global": {"authorized_id": 0, "sample_only": True},
        "agents": [
            {
                "name": "hashiko",
                "type": "flex",
                "workspace_dir": "workspaces/hashiko",
                "allowed_backends": [{"engine": "codex-cli"}],
                "active_backend": "codex-cli",
            }
        ],
    }


def test_save_key_reads_legacy_bytes_and_preserves_unrelated_secrets(tmp_path, monkeypatch):
    path = tmp_path / "secrets.json"
    path.write_bytes(_legacy_bytes({"unrelated": {"keep": True}}))
    monkeypatch.setattr(light_onboarding, "_ping_openrouter", lambda _key: True)

    ok, engine = light_onboarding.save_new_api_key(tmp_path, "sk-or-example")

    assert (ok, engine) == (True, "openrouter-api")
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw
    assert raw.endswith(b"\n")
    saved = read_config_json(path)
    assert saved["unrelated"] == {"keep": True}
    assert saved["openrouter-api_key"] == "sk-or-example"


def test_corrupt_secrets_are_not_replaced_after_a_valid_key_check(tmp_path, monkeypatch):
    path = tmp_path / "secrets.json"
    original = b'{"incomplete":'
    path.write_bytes(original)
    monkeypatch.setattr(light_onboarding, "_ping_deepseek", lambda _key: True)

    with pytest.raises(json.JSONDecodeError):
        light_onboarding.save_new_api_key(tmp_path, "sk-deepseek-example")

    assert path.read_bytes() == original


def test_ensure_agents_normalizes_legacy_empty_config_without_losing_extensions(tmp_path):
    path = tmp_path / "agents.json"
    path.write_bytes(
        _legacy_bytes(
            {
                "global": {"authorized_id": 7, "instance_id": "TEST"},
                "agents": [],
                "extension": {"keep": "yes"},
            }
        )
    )
    (tmp_path / "agents.json.sample").write_bytes(_legacy_bytes(_sample()))

    light_onboarding.ensure_agents_json(tmp_path, "deepseek-api")

    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw
    saved = read_config_json(path)
    assert saved["global"] == {"authorized_id": 7, "instance_id": "TEST"}
    assert saved["extension"] == {"keep": "yes"}
    assert saved["agents"][0]["active_backend"] == "deepseek-api"


def test_ensure_agents_rejects_corrupt_existing_config_without_overwrite(tmp_path):
    path = tmp_path / "agents.json"
    original = b"not-json"
    path.write_bytes(original)
    (tmp_path / "agents.json.sample").write_bytes(_legacy_bytes(_sample()))

    with pytest.raises(json.JSONDecodeError):
        light_onboarding.ensure_agents_json(tmp_path, "codex-cli")

    assert path.read_bytes() == original


def test_completion_marker_is_revision_safe_and_normalized(tmp_path):
    marker = tmp_path / "workspaces" / "hashiko" / "tui_onboarding_complete"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(_legacy_bytes({"extension": 1, "lang": "en"}))

    light_onboarding.write_completion_marker(tmp_path, "zh")

    raw = marker.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw
    assert read_config_json(marker) == {
        "extension": 1,
        "lang": "zh",
        "completed": True,
    }


def test_legacy_onboarding_preserves_existing_declarations_and_normalizes_bytes(tmp_path):
    agents = tmp_path / "agents.json"
    secrets = tmp_path / "secrets.json"
    agents.write_bytes(
        _legacy_bytes(
            {
                "global": {"authorized_id": 9, "extension": True},
                "agents": [],
                "root_extension": "kept",
            }
        )
    )
    secrets.write_bytes(_legacy_bytes({"existing": "kept"}))

    write_config(tmp_path, "codex-cli", {"welcomePrompt": "Welcome"}, "en")

    for path in (agents, secrets):
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw
    saved_agents = read_config_json(agents)
    assert saved_agents["global"] == {"authorized_id": 9, "extension": True}
    assert saved_agents["root_extension"] == "kept"
    assert saved_agents["agents"][0]["name"] == "hashiko"
    assert read_config_json(secrets)["existing"] == "kept"


def test_legacy_onboarding_preflights_all_config_before_side_effects(tmp_path):
    agents = tmp_path / "agents.json"
    secrets = tmp_path / "secrets.json"
    original_agents = _legacy_bytes({"global": {}, "agents": []})
    agents.write_bytes(original_agents)
    secrets.write_bytes(b'{"broken":')

    with pytest.raises(json.JSONDecodeError):
        write_config(tmp_path, "codex-cli", {}, "en")

    assert agents.read_bytes() == original_agents
    assert not (tmp_path / "workspaces").exists()
