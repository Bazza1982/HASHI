from __future__ import annotations

import json

import pytest

from orchestrator.config_json import read_config_json, write_config_json
from transports.chat_router import ChatRouter, ChatRouterPersistenceError


def _legacy(value: dict) -> bytes:
    rendered = json.dumps(value, ensure_ascii=False, indent=2).replace("\n", "\r\n")
    return b"\xef\xbb\xbf" + (rendered + "\r\n").encode("utf-8")


def test_router_loads_legacy_bytes_and_normalizes_only_changed_route(tmp_path):
    path = tmp_path / "whatsapp_routes.json"
    path.write_bytes(
        _legacy(
            {
                "one@s.whatsapp.net": {"mode": "single", "agents": ["lily"]},
                "extension": {"keep": True},
            }
        )
    )
    router = ChatRouter(path)

    router.set_group("two@s.whatsapp.net", ["akane", "kasumi"])

    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw
    saved = read_config_json(path)
    assert saved["extension"] == {"keep": True}
    assert saved["one@s.whatsapp.net"]["agents"] == ["lily"]
    assert saved["two@s.whatsapp.net"]["mode"] == "group"


def test_router_rejects_stale_change_and_restores_in_memory_route(tmp_path):
    path = tmp_path / "whatsapp_routes.json"
    path.write_text(
        json.dumps({"chat": {"mode": "single", "agents": ["lily"]}}),
        encoding="utf-8",
    )
    router = ChatRouter(path)
    winner = read_config_json(path)
    winner["external"] = {"keep": True}
    write_config_json(path, winner)

    with pytest.raises(ChatRouterPersistenceError):
        router.set_single("chat", "stale")

    assert router.get_targets("chat") == ["lily"]
    assert read_config_json(path)["external"] == {"keep": True}
    assert read_config_json(path)["chat"]["agents"] == ["lily"]


def test_router_never_replaces_corrupt_saved_state(tmp_path):
    path = tmp_path / "whatsapp_routes.json"
    original = b'{"broken":'
    path.write_bytes(original)
    router = ChatRouter(path)

    with pytest.raises(ChatRouterPersistenceError):
        router.set_single("chat", "lily")

    assert router.get_targets("chat") == []
    assert path.read_bytes() == original
