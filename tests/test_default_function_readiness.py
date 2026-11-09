from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from adapters.her_habits import HabitMeditationConfig
from orchestrator.exchange_config import parse_exchange_config


def test_shipped_habit_learning_is_available_without_an_extra_enable_flag():
    fresh = SimpleNamespace(her_providers={})
    assert HabitMeditationConfig.resolve(fresh, {}, environ={}).enabled is True
    assert HabitMeditationConfig.resolve(
        fresh, {"habit_meditation_enabled": False}, environ={},
    ).enabled is False
    assert HabitMeditationConfig.resolve(
        fresh, {"habit_learning_eligible": False}, environ={},
    ).enabled is False


def test_configured_exchange_connects_without_a_redundant_enable_flag():
    raw = {
        "authority_id": "authority-1",
        "url": "wss://exchange.example.com/v1/connect",
        "registered_instance_id": "instance-1",
        "instance_alias": "personal",
        "credential_ref": "instance-key",
        "published_agents": [],
    }
    result = parse_exchange_config(raw)
    assert result.enabled is True
    assert result.published_agents == ()
    assert parse_exchange_config({**raw, "enabled": False}).enabled is False
    assert parse_exchange_config({}).enabled is False


def test_portable_template_has_normal_function_defaults_ready():
    templates = Path(__file__).resolve().parents[1] / "packaging/portable_windows/templates"
    global_config = json.loads((templates / "agents.json").read_text(encoding="utf-8"))["global"]
    gateway = json.loads((templates / "api_gateway_state.json").read_text(encoding="utf-8"))
    assert gateway["enabled"] is True
    assert global_config["desktop_enabled"] is True
    assert HabitMeditationConfig.resolve(
        SimpleNamespace(her_providers=global_config["her_providers"]), {}, environ={},
    ).enabled is True
# HASHI-Fingerprint: hfp_f8403b975ac1423b8be894a760f037d5
