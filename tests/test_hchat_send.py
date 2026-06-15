from __future__ import annotations

import json
from urllib.error import URLError

from orchestrator.enterprise import ChannelRegistry, IdentityService
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


def _enterprise_cfg(tmp_path) -> dict:
    cfg = _local_cfg()
    cfg["global"] = {
        **cfg["global"],
        "deployment_profile": "enterprise",
        "organization_id": "ORG-001",
        "bridge_home": str(tmp_path),
    }
    return cfg


def _audit_events(tmp_path) -> list[dict]:
    path = tmp_path / "state" / "enterprise_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


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


def test_send_hchat_enterprise_denies_disabled_hchat_egress(tmp_path, monkeypatch):
    calls = []
    cfg = _enterprise_cfg(tmp_path)
    IdentityService.from_path(tmp_path / "state" / "enterprise.sqlite").create_organization(
        org_id="ORG-001",
        name="Acme",
    )

    monkeypatch.setattr(hchat_send, "_load_config", lambda: cfg)
    monkeypatch.setattr(hchat_send, "_build_reply_route", lambda _cfg: {"instance_id": "HASHI1"})
    monkeypatch.setattr(
        hchat_send,
        "_send_via_local_workbench",
        lambda *args: calls.append(args) or True,
    )

    assert hchat_send.send_hchat("akane", "zelda", "hello") is False
    assert calls == []
    event = _audit_events(tmp_path)[-1]
    assert event["event_type"] == "channel"
    assert event["status"] == "denied"
    assert event["context"]["channel_type"] == "hchat"
    assert event["context"]["reason"] == "channel_disabled"
    assert event["context"]["send_hchat"] is True


def test_send_hchat_enterprise_allows_bound_sender_agent(tmp_path, monkeypatch):
    calls = []
    cfg = _enterprise_cfg(tmp_path)
    IdentityService.from_path(tmp_path / "state" / "enterprise.sqlite").create_organization(
        org_id="ORG-001",
        name="Acme",
    )
    registry = ChannelRegistry.from_path(tmp_path / "state" / "enterprise.sqlite")
    registry.ensure_default_channels(org_id="ORG-001")
    registry.register_channel(org_id="ORG-001", channel_type="hchat", enabled=True)
    registry.bind_channel(
        org_id="ORG-001",
        channel_type="hchat",
        scope_type="agent",
        scope_id="zelda",
        permission="egress",
    )

    monkeypatch.setattr(hchat_send, "_load_config", lambda: cfg)
    monkeypatch.setattr(hchat_send, "_build_reply_route", lambda _cfg: {"instance_id": "HASHI1"})
    monkeypatch.setattr(
        hchat_send,
        "_send_via_local_workbench",
        lambda *args: calls.append(args) or True,
    )

    assert hchat_send.send_hchat("akane", "zelda", "hello") is True
    assert calls == [(cfg, 18800, "akane", "zelda", "hello", "HASHI1", {"instance_id": "HASHI1"})]
    assert _audit_events(tmp_path) == []


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


def test_send_hchat_remote_prefers_protocol_transport_when_shared_token_available(monkeypatch):
    cfg = _local_cfg()
    calls = []

    monkeypatch.setattr(hchat_send, "_load_config", lambda: cfg)
    monkeypatch.setattr(hchat_send, "_shared_token_for_protocol", lambda: "shared-secret")
    monkeypatch.setattr(
        hchat_send,
        "_send_via_protocol_transport",
        lambda to_agent, target_instance, from_agent, text: calls.append((to_agent, target_instance, from_agent, text)) or True,
    )

    assert hchat_send.send_hchat("agent1@INTEL", "zelda", "hello over protocol") is True
    assert calls == [("agent1", "INTEL", "zelda", "hello over protocol")]


def test_protocol_transport_delegates_to_protocol_send(monkeypatch):
    from tools import protocol_send

    calls = []
    monkeypatch.setattr(hchat_send, "_shared_token_for_protocol", lambda: "shared-secret")
    monkeypatch.setattr(
        protocol_send,
        "send_protocol_message",
        lambda target, from_agent, text, **kwargs: calls.append((target, from_agent, text, kwargs)) or True,
    )

    assert hchat_send._send_via_protocol_transport("agent1", "INTEL", "zelda", "hello over protocol") is True
    assert calls == [
        (
            "agent1@INTEL",
            "zelda",
            "hello over protocol",
            {"target_instance": "INTEL", "shared_token": "shared-secret"},
        )
    ]


def test_check_hchat_route_reports_protocol_transport_when_available(monkeypatch):
    cfg = _local_cfg()
    remote = {
        "instance_id": "INTEL",
        "host": "192.168.0.6",
        "wb_port": 18802,
        "remote_host": "192.168.0.6",
        "remote_port": 8766,
    }

    monkeypatch.setattr(hchat_send, "_load_config", lambda: cfg)
    monkeypatch.setattr(hchat_send, "_shared_token_for_protocol", lambda: "shared-secret")
    monkeypatch.setattr(hchat_send, "_find_remote_instance", lambda *args, **kwargs: remote)
    monkeypatch.setattr(hchat_send, "_probe_remote_http", lambda host, port, timeout=3: True)

    result = hchat_send.check_hchat_route("agent1@INTEL", "zelda")

    assert result["ok"] is True
    assert result["route_type"] == "remote_protocol"
    assert result["host"] == "192.168.0.6"
    assert result["remote_port"] == 8766


def test_load_instances_overlays_live_endpoint_over_stale_instance(monkeypatch, tmp_path):
    instances_path = tmp_path / "instances.json"
    live_path = tmp_path / "state" / "remote_live_endpoints.json"
    live_path.parent.mkdir(parents=True)
    instances_path.write_text(
        json.dumps(
            {
                "instances": {
                    "intel": {
                        "instance_id": "INTEL",
                        "active": False,
                        "api_host": "192.168.0.6",
                        "lan_ip": "192.168.0.6",
                        "remote_port": 40050,
                        "workbench_port": 18802,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    live_path.write_text(
        json.dumps(
            {
                "endpoints": {
                    "intel": {
                        "instance_id": "INTEL",
                        "display_name": "INTEL",
                        "host": "192.168.0.6",
                        "port": 8766,
                        "remote_port": 8766,
                        "workbench_port": 18802,
                        "platform": "windows",
                        "discovery": "lan",
                        "capabilities": ["agent_directory_v1"],
                        "updated_at": 123.0,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(hchat_send, "INSTANCES_FILE", instances_path)
    monkeypatch.setattr(hchat_send, "LIVE_ENDPOINTS_FILE", live_path)
    monkeypatch.setattr(hchat_send, "_load_config", lambda: _local_cfg())
    monkeypatch.setattr(hchat_send.time, "time", lambda: 124.0)

    instances = hchat_send._load_instances()

    assert instances["intel"]["active"] is True
    assert instances["intel"]["api_host"] == "192.168.0.6"
    assert instances["intel"]["lan_ip"] == "192.168.0.6"
    assert instances["intel"]["remote_port"] == 8766
    assert instances["intel"]["workbench_port"] == 18802
    assert instances["intel"]["_discovery"] == "lan"


def test_load_instances_ignores_stale_live_endpoint(monkeypatch, tmp_path):
    instances_path = tmp_path / "instances.json"
    live_path = tmp_path / "state" / "remote_live_endpoints.json"
    live_path.parent.mkdir(parents=True)
    instances_path.write_text(
        json.dumps(
            {
                "instances": {
                    "intel": {
                        "instance_id": "INTEL",
                        "active": False,
                        "api_host": "192.168.0.6",
                        "remote_port": 40050,
                        "workbench_port": 18802,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    live_path.write_text(
        json.dumps(
            {
                "endpoints": {
                    "intel": {
                        "instance_id": "INTEL",
                        "host": "192.168.0.6",
                        "remote_port": 8766,
                        "workbench_port": 18802,
                        "updated_at": 100.0,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(hchat_send, "INSTANCES_FILE", instances_path)
    monkeypatch.setattr(hchat_send, "LIVE_ENDPOINTS_FILE", live_path)
    monkeypatch.setattr(hchat_send, "_load_config", lambda: _local_cfg())
    monkeypatch.setattr(hchat_send.time, "time", lambda: 100.0 + hchat_send.LIVE_ENDPOINT_TTL_SECONDS + 1)

    instances = hchat_send._load_instances()

    assert instances["intel"]["active"] is False
    assert instances["intel"]["remote_port"] == 40050


def test_remote_agent_names_falls_back_to_workbench_agents(monkeypatch):
    payloads = {
        "http://192.168.0.6:40050/protocol/agents": URLError("remote stale"),
        "http://192.168.0.6:18802/api/agents": {
            "agents": [
                {"id": "lily", "name": "lily", "online": True},
                {"id": "agent1", "name": "agent1", "online": True},
            ]
        },
    }

    class FakeResponse:
        def __init__(self, body):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self._body).encode("utf-8")

    def fake_urlopen(req, timeout):
        body = payloads[req.full_url]
        if isinstance(body, Exception):
            raise body
        return FakeResponse(body)

    monkeypatch.setattr(hchat_send, "_shared_token_for_protocol", lambda: None)
    monkeypatch.setattr(hchat_send.urllib_request, "urlopen", fake_urlopen)

    agents = hchat_send._remote_agent_names(
        "intel",
        {
            "instance_id": "INTEL",
            "api_host": "192.168.0.6",
            "lan_ip": "192.168.0.6",
            "remote_port": 40050,
            "workbench_port": 18802,
        },
    )

    assert agents == ["lily", "agent1"]


def test_remote_agent_names_prefers_authenticated_protocol_directory(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "ok": True,
                    "agents": [
                        {"agent_name": "lily", "is_active": True},
                        {"agent_name": "offline", "is_active": False},
                    ],
                    "agent_directory": {"directory_state": "fresh"},
                }
            ).encode("utf-8")

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        return FakeResponse()

    monkeypatch.setattr(hchat_send, "_shared_token_for_protocol", lambda: "shared-secret")
    monkeypatch.setattr(hchat_send, "_load_config", lambda: _local_cfg())
    monkeypatch.setattr(hchat_send.urllib_request, "urlopen", fake_urlopen)

    agents = hchat_send._remote_agent_names(
        "intel",
        {
            "instance_id": "INTEL",
            "api_host": "192.168.0.6",
            "lan_ip": "192.168.0.6",
            "remote_port": 40050,
            "workbench_port": 18802,
        },
    )

    assert agents == ["lily"]
    assert captured["url"] == "http://192.168.0.6:40050/protocol/directory"
    headers = {key.lower(): value for key, value in captured["headers"].items()}
    assert headers["x-hashi-auth-scheme"] == "hashi-shared-hmac-v1"
    assert headers["x-hashi-from-instance"] == "HASHI1"


def test_find_remote_instance_requires_agent_on_explicit_target(monkeypatch):
    instances = {
        "hashi9": {
            "instance_id": "HASHI9",
            "active": True,
            "workbench_port": 18819,
            "api_host": "127.0.0.1",
            "remote_port": 60862,
        }
    }

    monkeypatch.setattr(hchat_send, "_load_instances", lambda: instances)
    monkeypatch.setattr(hchat_send, "_remote_agent_names", lambda _inst_id, _inst_info: ["hashiko"])

    assert hchat_send._find_remote_instance("rain", "HASHI1", target_instance="HASHI9") is None


def test_find_remote_instance_accepts_agent_on_explicit_target(monkeypatch):
    instances = {
        "hashi9": {
            "instance_id": "HASHI9",
            "active": True,
            "workbench_port": 18819,
            "api_host": "127.0.0.1",
            "remote_port": 60862,
        }
    }

    monkeypatch.setattr(hchat_send, "_load_instances", lambda: instances)
    monkeypatch.setattr(hchat_send, "_remote_agent_names", lambda _inst_id, _inst_info: ["hashiko"])
    monkeypatch.setattr(hchat_send, "_probe_workbench", lambda _host, _port: False)

    route = hchat_send._find_remote_instance("hashiko", "HASHI1", target_instance="HASHI9")

    assert route is not None
    assert route["instance_id"] == "HASHI9"


def test_preferred_host_deprioritizes_same_host_loopback():
    instance_info = {
        "same_host_loopback": "127.0.0.1",
        "api_host": "127.0.0.1",
        "lan_ip": "192.168.0.211",
        "tailscale_ip": "100.64.0.9",
        "internet_host": "198.51.100.10",
    }

    assert hchat_send._preferred_host(instance_info) == "192.168.0.211"
    assert hchat_send._preferred_host(instance_info, for_remote=True) == "192.168.0.211"


def test_workbench_hosts_for_route_prefers_canonical_before_loopback():
    route = {
        "host": "192.168.0.211",
        "api_host": "127.0.0.1",
        "lan_ip": "192.168.0.211",
        "same_host_loopback": "127.0.0.1",
    }

    hosts = hchat_send._workbench_hosts_for_route(route)

    assert hosts[0] == "192.168.0.211"
    assert hosts[-1] == "127.0.0.1"


def test_find_exchange_instance_prefers_non_loopback_host(monkeypatch):
    monkeypatch.setattr(
        hchat_send,
        "_load_instances",
        lambda: {
            "hashi1": {
                "instance_id": "HASHI1",
                "active": True,
                "api_host": "127.0.0.1",
                "lan_ip": "172.21.12.144",
                "same_host_loopback": "127.0.0.1",
                "workbench_port": 18800,
                "remote_port": 8766,
            }
        },
    )

    route = hchat_send._find_exchange_instance("HASHI2")

    assert route is not None
    assert route["host"] == "172.21.12.144"
