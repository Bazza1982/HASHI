from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from orchestrator.config_json import ConfigConflictError, read_config_json, write_config_json
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime


def _legacy(value: dict) -> bytes:
    rendered = json.dumps(value, ensure_ascii=False, indent=2).replace("\n", "\r\n")
    return b"\xef\xbb\xbf" + (rendered + "\r\n").encode("utf-8")


def test_runtime_session_state_normalizes_legacy_bytes_and_preserves_unknowns(tmp_path):
    path = tmp_path / ".runtime_session.json"
    path.write_bytes(_legacy({"clean_shutdown": True, "extension": "kept"}))
    runtime = SimpleNamespace(runtime_session_path=path)

    state = FlexibleAgentRuntime._load_runtime_session_state(runtime)
    state["clean_shutdown"] = False
    FlexibleAgentRuntime._save_runtime_session_state(runtime, state)

    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw
    assert read_config_json(path)["extension"] == "kept"


def test_runtime_session_state_rejects_stale_publication(tmp_path):
    path = tmp_path / ".runtime_session.json"
    path.write_text('{"clean_shutdown": true}', encoding="utf-8")
    runtime = SimpleNamespace(runtime_session_path=path)
    stale = FlexibleAgentRuntime._load_runtime_session_state(runtime)
    winner = read_config_json(path)
    winner["external"] = "kept"
    write_config_json(path, winner)

    stale["clean_shutdown"] = False
    with pytest.raises(ConfigConflictError):
        FlexibleAgentRuntime._save_runtime_session_state(runtime, stale)
    assert read_config_json(path)["external"] == "kept"


def test_skill_state_update_rejects_corruption_without_overwrite(tmp_path):
    path = tmp_path / "skill_state.json"
    original = b'{"broken":'
    path.write_bytes(original)
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.workspace_dir = tmp_path

    assert FlexibleAgentRuntime._get_skill_state(runtime) == {}
    with pytest.raises(json.JSONDecodeError):
        FlexibleAgentRuntime._set_skill_state(runtime, "safevoice", False)
    assert path.read_bytes() == original


def test_skill_state_update_reads_bom_crlf_and_publishes_utf8_lf(tmp_path):
    path = tmp_path / "skill_state.json"
    path.write_bytes(_legacy({"extension": 1, "safevoice": True}))
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.workspace_dir = tmp_path

    FlexibleAgentRuntime._set_skill_state(runtime, "safevoice", False)

    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw
    assert read_config_json(path) == {"extension": 1, "safevoice": False}
