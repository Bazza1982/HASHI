from __future__ import annotations

import json
from urllib.error import URLError

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


def test_format_hchat_message_adds_autoreply_instruction():
    message = hchat_send.format_hchat_message("zelda", "HASHI1", "Please review the queue fix.")

    assert message.startswith("[hchat from zelda@HASHI1] HChat protocol note:")
    assert "Do not run hchat_send.py" in message
    assert "Please review the queue fix." in message


def test_format_hchat_message_preserves_reply_body_for_loop_guard():
    message = hchat_send.format_hchat_message("akane", "HASHI1", "[hchat reply from akane] done")

    assert message == "[hchat from akane@HASHI1] [hchat reply from akane] done"


def test_send_via_workbench_uses_autoreply_envelope(monkeypatch):
    payloads = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout):
        payloads.append(json.loads(req.data.decode("utf-8")))
        return FakeResponse()

    monkeypatch.setattr(hchat_send.urllib_request, "urlopen", fake_urlopen)

    assert hchat_send._send_via_workbench(
        "127.0.0.1",
        18800,
        "akane",
        "zelda",
        "Please review the queue fix.",
        "HASHI1",
    )

    assert payloads[0]["agent"] == "akane"
    assert payloads[0]["text"].startswith("[hchat from zelda@HASHI1] HChat protocol note:")
    assert "Do not run hchat_send.py" in payloads[0]["text"]


def test_probe_http_returns_false_on_unexpected_exception(monkeypatch):
    def fake_urlopen(_req, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(hchat_send.urllib_request, "urlopen", fake_urlopen)

    assert hchat_send._probe_http("http://127.0.0.1:18800/api/health") is False


def test_probe_http_returns_false_on_url_error(monkeypatch):
    def fake_urlopen(_req, **_kwargs):
        raise URLError("down")

    monkeypatch.setattr(hchat_send.urllib_request, "urlopen", fake_urlopen)

    assert hchat_send._probe_http("http://127.0.0.1:18800/api/health") is False


def test_check_hchat_route_local_agent_probes_without_delivery(monkeypatch):
    cfg = _local_cfg()
    send_calls = []

    monkeypatch.setattr(hchat_send, "_load_config", lambda: cfg)
    monkeypatch.setattr(hchat_send, "_send_via_local_workbench", lambda *args: send_calls.append(args) or True)
    monkeypatch.setattr(hchat_send, "_first_reachable_workbench", lambda hosts, port: "10.255.255.254")

    result = hchat_send.check_hchat_route("akane", "zelda")

    assert result["ok"] is True
    assert result["delivery_attempted"] is False
    assert result["route_type"] == "local_workbench"
    assert result["host"] == "10.255.255.254"
    assert result["port"] == 18800
    assert send_calls == []


def test_check_hchat_route_unknown_local_agent_fails_before_probe(monkeypatch):
    cfg = _local_cfg()
    probes = []

    monkeypatch.setattr(hchat_send, "_load_config", lambda: cfg)
    monkeypatch.setattr(hchat_send, "_first_reachable_workbench", lambda hosts, port: probes.append((hosts, port)) or None)

    result = hchat_send.check_hchat_route("unknown", "zelda")

    assert result["ok"] is False
    assert "not a local active agent" in result["error"]
    assert probes == []


def test_check_hchat_route_group_reports_members_without_delivery(monkeypatch):
    cfg = _local_cfg()
    cfg["groups"] = {"staff": {"members": ["akane", "zelda"], "exclude_from_broadcast": []}}

    monkeypatch.setattr(hchat_send, "_load_config", lambda: cfg)
    monkeypatch.setattr(hchat_send, "_first_reachable_workbench", lambda hosts, port: "127.0.0.1")

    result = hchat_send.check_hchat_route("@staff", "zelda")

    assert result["ok"] is True
    assert result["route_type"] == "local_group_workbench"
    assert result["members"] == ["akane"]


def test_check_hchat_route_remote_workbench(monkeypatch):
    cfg = _local_cfg()
    remote = {
        "instance_id": "HASHI2",
        "host": "10.0.0.3",
        "wb_port": 18802,
        "remote_host": "10.0.0.3",
        "remote_port": 8767,
    }

    monkeypatch.setattr(hchat_send, "_load_config", lambda: cfg)
    monkeypatch.setattr(hchat_send, "_get_cached_route", lambda _agent: None)
    monkeypatch.setattr(hchat_send, "_find_remote_instance", lambda *args, **kwargs: remote)
    monkeypatch.setattr(hchat_send, "_first_reachable_workbench", lambda hosts, port: "10.0.0.3")

    result = hchat_send.check_hchat_route("rika@HASHI2", "zelda")

    assert result["ok"] is True
    assert result["route_type"] == "remote_workbench"
    assert result["host"] == "10.0.0.3"
    assert result["port"] == 18802
