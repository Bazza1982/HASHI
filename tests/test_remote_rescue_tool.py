from __future__ import annotations

from tools import remote_rescue


def _instances():
    return {
        "hashi9": {
            "instance_id": "HASHI9",
            "lan_ip": "10.0.0.9",
            "remote_port": 8767,
        }
    }


def test_candidate_base_urls_prefer_https_then_http(monkeypatch):
    monkeypatch.setattr(remote_rescue, "_load_instances", _instances)
    monkeypatch.setattr(remote_rescue, "read_live_endpoints", lambda _root: {})
    monkeypatch.setattr(remote_rescue, "_default_instance_id", lambda: "HASHI1")
    monkeypatch.setattr(remote_rescue, "read_runtime_claim", lambda _root: None)

    urls = remote_rescue._candidate_base_urls("HASHI9")

    assert urls[:2] == ["https://10.0.0.9:8767", "http://10.0.0.9:8767"]


def test_candidate_base_urls_prefers_live_endpoint_port(monkeypatch):
    monkeypatch.setattr(remote_rescue, "_default_instance_id", lambda: "HASHI1")
    monkeypatch.setattr(
        remote_rescue,
        "read_live_endpoints",
        lambda _root: {
            "hashi9": {
                "instance_id": "HASHI9",
                "host": "192.168.0.211",
                "port": 35821,
                "remote_port": 35821,
            }
        },
    )

    urls = remote_rescue._candidate_base_urls("HASHI9")

    assert urls[:2] == ["https://192.168.0.211:35821", "http://192.168.0.211:35821"]


def test_capabilities_treat_missing_rescue_endpoint_as_unsupported(monkeypatch):
    monkeypatch.setattr(remote_rescue, "_load_instances", _instances)

    def fake_request(url, **kwargs):
        if url.endswith("/health"):
            return remote_rescue.HttpResult(200, {"ok": True}, url)
        if url.endswith("/protocol/status"):
            return remote_rescue.HttpResult(200, {"ok": True, "capabilities": ["handshake_v2"]}, url)
        if url.endswith("/control/hashi/status"):
            return remote_rescue.HttpResult(404, {"ok": False, "error": "not found"}, url)
        raise AssertionError(url)

    monkeypatch.setattr(remote_rescue, "_request_json_status", fake_request)

    result = remote_rescue.probe_capabilities("HASHI9")

    assert result["capabilities"]["remote_basic"] is True
    assert result["capabilities"]["protocol_status"] is True
    assert result["capabilities"]["rescue_control"] is False
    assert result["capabilities"]["rescue_start"] is False
    assert result["status_endpoint_status"] == 404


def test_status_returns_unsupported_for_old_remote(monkeypatch):
    monkeypatch.setattr(remote_rescue, "_load_instances", _instances)

    def fake_request(url, **kwargs):
        if url.endswith("/health"):
            return remote_rescue.HttpResult(200, {"ok": True}, url)
        if url.endswith("/control/hashi/status"):
            return remote_rescue.HttpResult(404, {"ok": False}, url)
        raise AssertionError(url)

    monkeypatch.setattr(remote_rescue, "_request_json_status", fake_request)

    code, payload = remote_rescue.rescue_status("HASHI9")

    assert code == remote_rescue.EXIT_UNSUPPORTED
    assert payload["supported"] is False
    assert payload["endpoint"] == "/control/hashi/status"


def test_start_returns_forbidden_when_l3_not_enabled(monkeypatch):
    monkeypatch.setattr(remote_rescue, "_load_instances", _instances)

    def fake_request(url, **kwargs):
        if url.endswith("/health"):
            return remote_rescue.HttpResult(200, {"ok": True}, url)
        if url.endswith("/control/hashi/start"):
            return remote_rescue.HttpResult(403, {"ok": False, "error": "requires L3_RESTART"}, url)
        raise AssertionError(url)

    monkeypatch.setattr(remote_rescue, "_request_json_status", fake_request)

    code, payload = remote_rescue.rescue_start("HASHI9", reason="test")

    assert code == remote_rescue.EXIT_FORBIDDEN
    assert "L3_RESTART" in payload["error"]


def test_start_success_passes_reason(monkeypatch):
    monkeypatch.setattr(remote_rescue, "_load_instances", _instances)
    seen_payloads = []

    def fake_request(url, **kwargs):
        if url.endswith("/health"):
            return remote_rescue.HttpResult(200, {"ok": True}, url)
        if url.endswith("/control/hashi/start"):
            seen_payloads.append(kwargs["payload"])
            return remote_rescue.HttpResult(200, {"ok": True, "started": True}, url)
        raise AssertionError(url)

    monkeypatch.setattr(remote_rescue, "_request_json_status", fake_request)

    code, payload = remote_rescue.rescue_start("HASHI9", reason="core down")

    assert code == 0
    assert payload["started"] is True
    assert seen_payloads == [{"reason": "core down"}]


def test_start_can_send_shared_token_headers(monkeypatch):
    monkeypatch.setattr(remote_rescue, "_load_instances", _instances)
    seen_headers = []

    def fake_request(url, **kwargs):
        if url.endswith("/health"):
            return remote_rescue.HttpResult(200, {"ok": True}, url)
        if url.endswith("/control/hashi/start"):
            headers = remote_rescue.build_client_auth_headers(
                url=url,
                method="POST",
                data=b'{"reason": "core down"}',
                token=kwargs.get("token"),
                shared_token=kwargs.get("shared_token"),
                from_instance=kwargs.get("from_instance"),
                normalize_instance=remote_rescue._normalize_instance_id,
                load_default_instance=remote_rescue._default_instance_id,
            )
            seen_headers.append(headers)
            return remote_rescue.HttpResult(200, {"ok": True, "started": True}, url)
        raise AssertionError(url)

    monkeypatch.setattr(remote_rescue, "_request_json_status", fake_request)

    code, payload = remote_rescue.rescue_start("HASHI9", reason="core down", shared_token="secret", from_instance="HASHI1")

    assert code == 0
    assert payload["started"] is True
    assert seen_headers[0]["X-Hashi-Auth-Scheme"] == "hashi-shared-hmac-v1"


def test_logs_returns_remote_tail(monkeypatch):
    monkeypatch.setattr(remote_rescue, "_load_instances", _instances)

    def fake_request(url, **kwargs):
        if url.endswith("/health"):
            return remote_rescue.HttpResult(200, {"ok": True}, url)
        if "/control/hashi/logs" in url:
            return remote_rescue.HttpResult(
                200,
                {
                    "ok": True,
                    "lines": ["a", "b"],
                    "requested_tail": 2,
                    "effective_tail": 2,
                    "tail_truncated": False,
                },
                url,
            )
        raise AssertionError(url)

    monkeypatch.setattr(remote_rescue, "_request_json_status", fake_request)

    code, payload = remote_rescue.rescue_logs("HASHI9", name="start", tail=2)

    assert code == 0
    assert payload["lines"] == ["a", "b"]
    assert payload["effective_tail"] == 2
    assert payload["tail_truncated"] is False


def test_logs_returns_unsupported_for_old_remote(monkeypatch):
    monkeypatch.setattr(remote_rescue, "_load_instances", _instances)

    def fake_request(url, **kwargs):
        if url.endswith("/health"):
            return remote_rescue.HttpResult(200, {"ok": True}, url)
        if "/control/hashi/logs" in url:
            return remote_rescue.HttpResult(404, {"ok": False}, url)
        raise AssertionError(url)

    monkeypatch.setattr(remote_rescue, "_request_json_status", fake_request)

    code, payload = remote_rescue.rescue_logs("HASHI9", name="start", tail=2)

    assert code == remote_rescue.EXIT_UNSUPPORTED
    assert payload["supported"] is False
    assert payload["endpoint"] == "/control/hashi/logs"
