from __future__ import annotations

from tools import hchat_send


def _local_cfg() -> dict:
    return {
        "global": {
            "instance_id": "HASHI1",
            "workbench_port": 18800,
            "api_host": "127.0.0.1",
        },
        "agents": [
            {"name": "akane", "is_active": True},
            {"name": "zelda", "is_active": True},
        ],
    }


def test_send_hchat_local_agent_uses_local_workbench_fallback(monkeypatch):
    calls = []
    cfg = _local_cfg()

    monkeypatch.setattr(hchat_send, "_load_config", lambda: cfg)
    monkeypatch.setattr(hchat_send, "_build_reply_route", lambda _cfg: {"instance_id": "HASHI1"})
    monkeypatch.setattr(
        hchat_send,
        "_send_via_local_workbench",
        lambda *args: calls.append(args) or True,
    )

    assert hchat_send.send_hchat("akane", "zelda", "hello") is True

    assert calls == [(cfg, 18800, "akane", "zelda", "hello", "HASHI1", {"instance_id": "HASHI1"})]


def test_send_hchat_explicit_local_instance_uses_local_workbench_fallback(monkeypatch):
    calls = []
    cfg = _local_cfg()

    monkeypatch.setattr(hchat_send, "_load_config", lambda: cfg)
    monkeypatch.setattr(hchat_send, "_build_reply_route", lambda _cfg: {"instance_id": "HASHI1"})
    monkeypatch.setattr(
        hchat_send,
        "_send_via_local_workbench",
        lambda *args: calls.append(args) or True,
    )

    assert hchat_send.send_hchat("akane@HASHI1", "zelda", "hello") is True

    assert calls == [(cfg, 18800, "akane", "zelda", "hello", "HASHI1", {"instance_id": "HASHI1"})]
