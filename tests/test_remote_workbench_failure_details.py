"""Finite connection causes, unknown writes and correlated Remote gateway responses."""
from __future__ import annotations

import errno
import json
import logging
import socket
from http.client import IncompleteRead
from urllib.error import URLError

import pytest

from remote.api import server as remote_server
from tests.test_remote_workbench_gateway import _client, _signed_headers


@pytest.mark.parametrize("cause,reason,accepted", [
    (ConnectionRefusedError(errno.ECONNREFUSED, "PRIVATE endpoint"), "connection_refused", False),
    (socket.gaierror(socket.EAI_NONAME, "PRIVATE host"), "name_not_resolved", False),
    (TimeoutError("PRIVATE timeout"), "timed_out", None),
    (ConnectionResetError(errno.ECONNRESET, "PRIVATE reset"), "connection_reset", None),
])
def test_actual_gateway_preserves_finite_cause_and_write_acceptance(tmp_path, monkeypatch, caplog, cause, reason, accepted):
    client, token = _client(tmp_path)
    caplog.set_level(logging.INFO, logger=remote_server.logger.name)
    calls = []
    def unavailable(request, timeout):
        calls.append(request)
        raise URLError(cause)
    monkeypatch.setattr(remote_server, "local_http_hosts", lambda: ("127.0.0.1", "localhost"))
    monkeypatch.setattr(remote_server.urllib_request, "urlopen", unavailable)
    path = "/workbench/v1/proxy/api/chat"
    body = b'{"text":"PRIVATE-BODY"}'
    response = client.post(path, content=body, headers=_signed_headers(token, method="POST", path=path, body=body))
    result = response.json()
    assert response.status_code == 503
    assert result["failure"]["reason"] == reason
    assert result["accepted"] is accepted
    assert result["effect_state"] == ("not_started" if accepted is False else "unconfirmed")
    assert result["diagnostic_ref"] in result["message"] and result["diagnostic_ref"] in caplog.text
    assert "PRIVATE" not in json.dumps(result) + caplog.text
    assert len(calls) == (2 if accepted is False else 1), "Uncertain writes cannot be replayed on a fallback address"
    if accepted is None:
        assert result["retryable"] is False and result["retry_safe"] is False


def test_read_failure_never_claims_a_change_was_submitted(tmp_path, monkeypatch):
    client, token = _client(tmp_path)
    monkeypatch.setattr(remote_server, "local_http_hosts", lambda: ("127.0.0.1",))
    def unavailable(request, timeout):
        raise URLError(TimeoutError("PRIVATE"))
    monkeypatch.setattr(remote_server.urllib_request, "urlopen", unavailable)
    path = "/workbench/v1/proxy/api/agents"
    response = client.get(path, headers=_signed_headers(token, method="GET", path=path))
    result = response.json()
    assert result["effect_state"] == "not_requested" and result["accepted"] is False
    assert result["failure"]["reason"] == "timed_out"


def test_bad_local_target_configuration_is_not_reported_as_bad_browser_input(tmp_path, monkeypatch):
    client, token = _client(tmp_path)
    def invalid_target(*args, **kwargs):
        raise ValueError("PRIVATE target URL")
    monkeypatch.setattr(remote_server, "local_http_url", invalid_target)
    path = "/workbench/v1/proxy/api/chat"
    body = b'{"text":"test"}'
    response = client.post(path, content=body, headers=_signed_headers(token, method="POST", path=path, body=body))
    result = response.json()
    assert response.status_code == 503
    assert result["failure"]["reason"] == "target_invalid" and result["accepted"] is False
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("read_error", [ConnectionResetError(errno.ECONNRESET, "PRIVATE response body"), IncompleteRead(b"PRIVATE")])
def test_response_lost_after_post_is_not_replayed_and_keeps_the_read_phase(tmp_path, monkeypatch, read_error):
    client, token = _client(tmp_path)
    posted = []
    class Response:
        status = 200
        headers = {"Content-Type": "application/json"}
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            raise read_error
    def committed(request, timeout):
        posted.append(request)
        return Response()
    monkeypatch.setattr(remote_server, "local_http_hosts", lambda: ("127.0.0.1", "localhost"))
    monkeypatch.setattr(remote_server.urllib_request, "urlopen", committed)
    path = "/workbench/v1/proxy/api/chat"
    body = b'{"text":"test"}'
    response = client.post(path, content=body, headers=_signed_headers(token, method="POST", path=path, body=body))
    result = response.json()
    assert result["accepted"] is None and result["failure"]["phase"] == "read"
    assert len(posted) == 1
