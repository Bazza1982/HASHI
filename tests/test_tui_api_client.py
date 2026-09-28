from __future__ import annotations

import base64
import hashlib
import json

import pytest

from tui.api_client import TuiApiClient


@pytest.mark.asyncio
async def test_tui_write_timeout_never_replays_on_fallback_instance(monkeypatch):
    client = TuiApiClient(
        "http://127.0.0.1:18800",
        fallback_base_urls=["http://127.0.0.1:18804"],
        expected_instance_id="HASHI1",
    )
    attempts = []

    class _Response:
        status = 200

        async def text(self):
            return json.dumps({"ok": True, "instance_id": "HASHI1"})

    class _RequestContext:
        def __init__(self, url):
            self.url = url

        async def __aenter__(self):
            if self.url.endswith(":18800/api/chat"):
                raise TimeoutError("response lost after possible acceptance")
            return _Response()

        async def __aexit__(self, *_args):
            return False

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def request(self, method, url, *, json=None):
            attempts.append((method, url))
            return _RequestContext(url)

    monkeypatch.setattr("tui.api_client.aiohttp.ClientSession", lambda timeout: _Session())

    result = await client._direct_request(
        "POST", "/api/chat", json_body={"agent": "akane", "text": "hello"}
    )

    assert result["ok"] is False
    assert result["code"] == "request_outcome_unknown"
    assert attempts == [
        ("GET", "http://127.0.0.1:18800/api/health"),
        ("POST", "http://127.0.0.1:18800/api/chat"),
    ]


@pytest.mark.asyncio
async def test_tui_write_refuses_fallback_with_wrong_instance_identity(monkeypatch):
    client = TuiApiClient(
        "http://127.0.0.1:18800",
        fallback_base_urls=["http://127.0.0.1:18804"],
        expected_instance_id="HASHI1",
    )
    attempts = []

    class _Response:
        status = 200

        async def text(self):
            return json.dumps({"ok": True, "instance_id": "HASHI4"})

    class _RequestContext:
        def __init__(self, method, url):
            self.method, self.url = method, url

        async def __aenter__(self):
            if self.method != "GET":
                raise AssertionError("submission reached wrong instance")
            if self.url.startswith("http://127.0.0.1:18800"):
                raise TimeoutError("first health route unavailable")
            return _Response()

        async def __aexit__(self, *_args):
            return False

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def request(self, method, url, *, json=None):
            attempts.append((method, url))
            return _RequestContext(method, url)

    monkeypatch.setattr("tui.api_client.aiohttp.ClientSession", lambda timeout: _Session())
    result = await client._direct_request(
        "POST", "/api/chat", json_body={"agent": "akane", "text": "hello"}
    )

    assert result["error"] == "instance_identity_mismatch"
    assert [method for method, _ in attempts] == ["GET", "GET"]


@pytest.mark.asyncio
async def test_tui_write_uses_reachable_fallback_only_after_identity_check(monkeypatch):
    client = TuiApiClient(
        "http://127.0.0.1:18800",
        fallback_base_urls=["http://127.0.0.1:18804"],
        expected_instance_id="HASHI1",
    )
    attempts = []

    class _Response:
        status = 200

        async def text(self):
            return json.dumps({"ok": True, "instance_id": "HASHI1"})

    class _RequestContext:
        def __init__(self, method, url):
            self.method, self.url = method, url

        async def __aenter__(self):
            if self.url.startswith("http://127.0.0.1:18800"):
                raise TimeoutError("first route unavailable")
            return _Response()

        async def __aexit__(self, *_args):
            return False

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def request(self, method, url, *, json=None):
            attempts.append((method, url))
            return _RequestContext(method, url)

    monkeypatch.setattr("tui.api_client.aiohttp.ClientSession", lambda timeout: _Session())
    result = await client._direct_request(
        "POST", "/api/chat", json_body={"agent": "akane", "text": "hello"}
    )

    assert result["ok"] is True
    assert attempts == [
        ("GET", "http://127.0.0.1:18800/api/health"),
        ("GET", "http://127.0.0.1:18804/api/health"),
        ("POST", "http://127.0.0.1:18804/api/chat"),
    ]


@pytest.mark.asyncio
async def test_direct_tui_chat_preserves_legacy_fallback_without_session_capability(monkeypatch):
    client = TuiApiClient("http://127.0.0.1:18800")
    calls = []

    async def _request(method, path, *, json_body=None, timeout=10):
        calls.append(
            {"method": method, "path": path, "json": json_body, "timeout": timeout}
        )
        if path == "/api/v1/capabilities":
            return {"ok": True}
        return {"ok": True}

    monkeypatch.setattr(client, "_direct_request", _request)

    await client.send_chat(
        "akane",
        "hello",
        client_id="tui-window-1",
        telegram_mirror=False,
        ui_locale="zh-CN",
    )

    assert [call["path"] for call in calls] == ["/api/v1/capabilities", "/api/chat"]
    assert calls[-1]["method"] == "POST"
    assert calls[-1]["json"]["source"] == "tui"
    assert calls[-1]["json"]["delivery_policy"]["targets"] == []
    assert calls[-1]["json"]["delivery_policy"]["scope"] == "run"
    assert calls[-1]["json"]["ui_locale"] == "zh-CN"


@pytest.mark.asyncio
async def test_tui_does_not_infer_endpoint_from_schema_version_alone(monkeypatch):
    client = TuiApiClient("http://127.0.0.1:18800")
    calls = []

    async def _request(method, path, *, json_body=None, timeout=10):
        calls.append(path)
        if path == "/api/v1/capabilities":
            return {
                "ok": True,
                "session_api_version": "1.0",
                "frontend_contract_versions": {"ingress": 2},
            }
        if path == "/api/chat":
            return {"ok": True, "request_id": "legacy"}
        raise AssertionError(f"unadvertised endpoint was called: {path}")

    monkeypatch.setattr(client, "_direct_request", _request)
    result = await client.send_chat("akane", "hello", client_id="tui-1")

    assert result["request_id"] == "legacy"
    assert calls == ["/api/v1/capabilities", "/api/chat"]


@pytest.mark.asyncio
async def test_direct_tui_text_uses_canonical_session_run_when_available(monkeypatch):
    client = TuiApiClient("http://127.0.0.1:18800")
    calls = []

    async def _request(method, path, *, json_body=None, timeout=10, pinned_base=None):
        calls.append((method, path, json_body))
        if path == "/api/v1/capabilities":
            return {
                "ok": True,
                "session_api_version": "1.0",
                "frontend_contract_versions": {"ingress": 2},
                "tui_session_ingress": {
                    "version": 1, "primary_session": True, "text_runs": True
                },
            }
        if path == "/api/v1/agents/akane/primary-session":
            return {"ok": True, "session": {"session_id": "ses-1"}}
        if path == "/api/v1/sessions/ses-1/runs":
            return {
                "ok": True, "session_id": "ses-1", "run_id": "run-1",
                "request_id": "req-1",
            }
        raise AssertionError(f"unexpected TUI route: {path}")

    monkeypatch.setattr(client, "_direct_request", _request)
    accepted = await client.send_chat(
        "akane", "hello", client_id="tui-window-1",
        telegram_mirror=False, ui_locale="zh-CN",
    )

    assert accepted["run_id"] == "run-1"
    assert [path for _, path, _ in calls] == [
        "/api/v1/capabilities",
        "/api/v1/agents/akane/primary-session",
        "/api/v1/sessions/ses-1/runs",
    ]
    run = calls[-1][2]
    assert run["surface"] == "tui"
    assert run["client_id"] == "tui-window-1"
    assert run["message"]["content"] == [{"type": "text", "text": "hello"}]
    assert run["delivery_policy"]["targets"] == []
    assert run["idempotency_key"]


@pytest.mark.asyncio
async def test_tui_session_resolution_rejects_wrong_instance_before_submission(
    monkeypatch,
):
    client = TuiApiClient(
        "http://127.0.0.1:18800", expected_instance_id="HASHI1"
    )
    calls = []

    async def _request(method, path, *, json_body=None, timeout=10):
        calls.append(path)
        if path == "/api/v1/capabilities":
            return {
                "ok": True, "session_api_version": "1.0",
                "frontend_contract_versions": {"ingress": 2},
                "tui_session_ingress": {
                    "version": 1, "primary_session": True, "text_runs": True
                },
            }
        if path == "/api/v1/agents/akane/primary-session":
            return {
                "ok": True,
                "session": {"session_id": "ses-wrong", "instance_id": "HASHI2"},
            }
        raise AssertionError(f"unexpected submission: {path}")

    monkeypatch.setattr(client, "_direct_request", _request)
    result = await client.send_chat("akane", "hello", client_id="tui-1")

    assert result["ok"] is False
    assert result["error"] == "instance_identity_mismatch"
    assert calls == [
        "/api/v1/capabilities", "/api/v1/agents/akane/primary-session"
    ]


@pytest.mark.asyncio
async def test_direct_tui_text_run_stays_on_resolved_session_host(monkeypatch):
    client = TuiApiClient(
        "http://127.0.0.1:18800",
        fallback_base_urls=["http://127.0.0.1:18804"],
        expected_instance_id="HASHI1",
    )
    submitted = []

    async def request(method, path, *, json_body=None, timeout=10, pinned_base=None):
        if path == "/api/v1/capabilities":
            return {
                "ok": True, "session_api_version": "1.0",
                "frontend_contract_versions": {"ingress": 2},
                "tui_session_ingress": {
                    "version": 1, "primary_session": True, "text_runs": True,
                },
            }
        if path == "/api/v1/agents/akane/primary-session":
            return {
                "ok": True,
                "session": {"session_id": "ses-1", "instance_id": "HASHI1"},
            }
        submitted.append((method, path, pinned_base))
        return {"ok": True, "run_id": "run-1"}

    monkeypatch.setattr(client, "_direct_request", request)
    result = await client.send_chat("akane", "hello", client_id="tui-1")

    assert result["run_id"] == "run-1"
    assert submitted == [
        ("POST", "/api/v1/sessions/ses-1/runs", "http://127.0.0.1:18800")
    ]


@pytest.mark.asyncio
async def test_direct_tui_command_uses_session_bound_typed_invocation(monkeypatch):
    client = TuiApiClient("http://127.0.0.1:18800")
    submitted = []

    async def _request(
        method, path, *, json_body=None, timeout=10, pinned_base=None
    ):
        submitted.append((method, path, json_body, pinned_base))
        if path == "/api/v1/capabilities":
            return {
                "ok": True,
                "session_api_version": "1.0",
                "frontend_contract_versions": {"ingress": 2},
                "tui_session_ingress": {
                    "version": 1,
                    "primary_session": True,
                    "text_runs": True,
                    "command_invocations": True,
                },
            }
        if path.endswith("/primary-session"):
            return {
                "ok": True,
                "session": {
                    "session_id": "ses-command",
                    "instance_id": "HASHI1",
                    "context_generation": 4,
                },
            }
        return {"ok": True, "slash_command": True}

    monkeypatch.setattr(client, "_direct_request", _request)
    result = await client.send_chat("akane", "/model balanced", client_id="tui-1")

    assert result["slash_command"] is True
    assert [item[1] for item in submitted] == [
        "/api/v1/capabilities",
        "/api/v1/agents/akane/primary-session",
        "/api/v1/sessions/ses-command/commands",
    ]
    body = submitted[-1][2]
    assert body["command"] == "model"
    assert body["arguments"] == ["balanced"]
    assert body["client_id"] == "tui-1"
    assert body["context_generation"] == 4
    assert body["request_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/telegram", "/whatsapp"])
async def test_tui_mirror_command_uses_central_session_command(monkeypatch, command):
    client = TuiApiClient("http://127.0.0.1:18800")
    calls = []

    async def _request(method, path, *, json_body=None, timeout=10, pinned_base=None):
        calls.append(path)
        if path == "/api/v1/capabilities":
            return {
                "ok": True,
                "session_api_version": "1.0",
                "frontend_contract_versions": {"ingress": 2},
                "tui_session_ingress": {
                    "version": 1,
                    "primary_session": True,
                    "text_runs": True,
                    "command_invocations": True,
                },
            }
        if path == "/api/v1/agents/akane/primary-session":
            return {"ok": True, "session": {
                "session_id": "ses-1", "context_generation": 1,
            }}
        return {"ok": True, "command_invocation": True}

    monkeypatch.setattr(client, "_direct_request", _request)
    result = await client.send_chat("akane", command, client_id="tui-1")

    assert result["command_invocation"] is True
    assert calls == [
        "/api/v1/capabilities",
        "/api/v1/agents/akane/primary-session",
        "/api/v1/sessions/ses-1/commands",
    ]


@pytest.mark.asyncio
async def test_remote_tui_command_uses_typed_proxy_operation(monkeypatch):
    client = TuiApiClient(
        remote_url="http://127.0.0.1:8766", target_instance="HASHI2"
    )
    calls = []

    async def _proxy(operation, **kwargs):
        calls.append((operation, kwargs))
        if operation == "capabilities":
            return {
                "ok": True,
                "session_api_version": "1.0",
                "frontend_contract_versions": {"ingress": 2},
                "tui_session_ingress": {
                    "version": 1,
                    "primary_session": True,
                    "text_runs": True,
                    "command_invocations": True,
                },
            }
        if operation == "primary_session":
            return {
                "ok": True,
                "session": {
                    "session_id": "ses-peer",
                    "instance_id": "HASHI2",
                    "context_generation": 7,
                },
            }
        if operation == "session_command_invocation":
            return {"ok": True, "slash_command": True}
        raise AssertionError(f"unexpected operation: {operation}")

    monkeypatch.setattr(client, "_proxy_request", _proxy)
    result = await client.send_chat(
        "akane", "/mode fast", client_id="tui-remote-command"
    )

    assert result["slash_command"] is True
    assert [operation for operation, _ in calls] == [
        "capabilities",
        "primary_session",
        "session_command_invocation",
    ]
    command = calls[-1][1]
    assert command["session_id"] == "ses-peer"
    assert command["context_generation"] == 7
    assert command["command"] == "mode"
    assert command["arguments"] == ["fast"]
    assert command["request_id"]


@pytest.mark.asyncio
async def test_remote_tui_text_uses_session_run_over_authenticated_proxy(monkeypatch):
    client = TuiApiClient(
        remote_url="http://127.0.0.1:8766", target_instance="HASHI2"
    )
    calls = []

    async def _proxy(operation, **kwargs):
        calls.append((operation, kwargs))
        if operation == "capabilities":
            return {
                "ok": True,
                "session_api_version": "1.0",
                "frontend_contract_versions": {"ingress": 2},
                "tui_session_ingress": {
                    "version": 1, "primary_session": True, "text_runs": True
                },
            }
        if operation == "primary_session":
            return {
                "ok": True,
                "session": {
                    "session_id": "ses-remote", "instance_id": "HASHI2"
                },
            }
        if operation == "session_run":
            return {
                "ok": True, "session_id": "ses-remote", "run_id": "run-1"
            }
        raise AssertionError(f"unexpected operation: {operation}")

    monkeypatch.setattr(client, "_proxy_request", _proxy)
    result = await client.send_chat(
        "akane", "hello", client_id="tui-remote-1",
        telegram_mirror=False,
    )

    assert result["run_id"] == "run-1"
    assert [operation for operation, _ in calls] == [
        "capabilities", "primary_session", "session_run"
    ]
    assert calls[-1][1]["session_id"] == "ses-remote"
    assert calls[-1][1]["delivery_policy"]["targets"] == []
    assert calls[-1][1]["idempotency_key"]


@pytest.mark.asyncio
async def test_remote_tui_old_proxy_falls_back_only_before_submission(monkeypatch):
    client = TuiApiClient(
        remote_url="http://127.0.0.1:8766", target_instance="HASHI2"
    )
    calls = []

    async def _proxy(operation, **kwargs):
        calls.append(operation)
        if operation == "capabilities":
            return {
                "ok": True,
                "session_api_version": "1.0",
                "frontend_contract_versions": {"ingress": 2},
                "tui_session_ingress": {
                    "version": 1, "primary_session": True, "text_runs": True
                },
            }
        if operation == "primary_session":
            return {"ok": False, "error": "operation_not_allowed"}
        if operation == "chat":
            return {"ok": True, "request_id": "req-legacy"}
        raise AssertionError(f"unexpected operation: {operation}")

    monkeypatch.setattr(client, "_proxy_request", _proxy)
    result = await client.send_chat("akane", "hello", client_id="tui-remote-1")

    assert result["request_id"] == "req-legacy"
    assert calls == ["capabilities", "primary_session", "chat"]


@pytest.mark.asyncio
async def test_remote_tui_unknown_session_run_does_not_replay_on_legacy_chat(monkeypatch):
    client = TuiApiClient(
        remote_url="http://127.0.0.1:8766", target_instance="HASHI2"
    )
    calls = []

    async def _proxy(operation, **kwargs):
        calls.append(operation)
        if operation == "capabilities":
            return {
                "ok": True,
                "session_api_version": "1.0",
                "frontend_contract_versions": {"ingress": 2},
                "tui_session_ingress": {
                    "version": 1, "primary_session": True, "text_runs": True
                },
            }
        if operation == "primary_session":
            return {
                "ok": True,
                "session": {
                    "session_id": "ses-remote", "instance_id": "HASHI2"
                },
            }
        if operation == "session_run":
            return {"ok": False, "code": "request_timeout", "error": "unknown"}
        raise AssertionError(f"unexpected operation: {operation}")

    monkeypatch.setattr(client, "_proxy_request", _proxy)
    result = await client.send_chat("akane", "hello", client_id="tui-remote-1")

    assert result["code"] == "request_timeout"
    assert calls == ["capabilities", "primary_session", "session_run"]


@pytest.mark.asyncio
async def test_remote_tui_submission_timeout_reports_unknown_acceptance(monkeypatch):
    client = TuiApiClient(
        remote_url="http://127.0.0.1:8766", target_instance="HASHI2"
    )

    class _RequestContext:
        async def __aenter__(self):
            raise TimeoutError("proxy response was lost")

        async def __aexit__(self, *_args):
            return False

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def post(self, url, *, json):
            assert url == "http://127.0.0.1:8766/tui/proxy"
            assert json["operation"] == "session_run"
            return _RequestContext()

    monkeypatch.setattr("tui.api_client.aiohttp.ClientSession", lambda timeout: _Session())
    result = await client._proxy_request(
        "session_run", agent="akane", session_id="ses-1", text="hello",
        idempotency_key="tui-1",
    )
    assert result["code"] == "request_outcome_unknown"
    assert result["accepted"] is None


@pytest.mark.asyncio
async def test_direct_tui_attachment_sends_bytes_and_caption_in_one_request(monkeypatch):
    client = TuiApiClient("http://127.0.0.1:18800")
    captured = {}

    async def request(method, path, *, json_body=None, timeout=10):
        captured.update(method=method, path=path, body=json_body, timeout=timeout)
        return {"ok": True, "request_id": "request-1"}

    monkeypatch.setattr(client, "_direct_request", request)
    attachment = {
        "filename": "photo.png",
        "media_type": "image/png",
        "size_bytes": 8,
        "sha256": "digest",
        "content_b64": "iVBORw0KGgo=",
    }

    result = await client.send_chat_attachment(
        "akane",
        "describe it",
        attachment=attachment,
        client_id="tui-1",
        telegram_mirror=False,
    )

    assert result["request_id"] == "request-1"
    assert captured["path"] == "/api/chat"
    assert captured["body"]["text"] == "describe it"
    assert captured["body"]["attachment"] == attachment
    assert captured["body"]["delivery_policy"]["targets"] == []


@pytest.mark.asyncio
async def test_direct_tui_attachment_uses_session_asset_and_run_when_advertised(monkeypatch):
    client = TuiApiClient(
        "http://127.0.0.1:18800", expected_instance_id="HASHI1"
    )
    calls = []
    content = b"example image bytes"
    attachment = {
        "filename": "photo.png",
        "media_type": "image/png",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "content_b64": base64.b64encode(content).decode("ascii"),
    }

    async def request(method, path, *, json_body=None, timeout=10, pinned_base=None):
        calls.append((method, path, json_body))
        if path == "/api/v1/capabilities":
            return {
                "ok": True,
                "session_api_version": "1.0",
                "frontend_contract_versions": {"ingress": 2},
                "tui_session_ingress": {
                    "version": 1,
                    "primary_session": True,
                    "text_runs": True,
                    "attachment_runs": True,
                },
            }
        if path == "/api/v1/agents/akane/primary-session":
            return {
                "ok": True,
                "session": {"session_id": "ses-1", "instance_id": "HASHI1"},
            }
        if path == "/api/v1/sessions/ses-1/attachments":
            return {"ok": True, "attachment": {"attachment_id": "att-1"}}
        if path == "/api/v1/sessions/ses-1/attachments/att-1/commit":
            return {"ok": True, "attachment": {"attachment_id": "att-1"}}
        if path == "/api/v1/sessions/ses-1/runs":
            return {"ok": True, "run_id": "run-1", "session_id": "ses-1"}
        raise AssertionError(f"unexpected route {path}")

    async def binary_request(method, path, *, payload, timeout=10, pinned_base=None):
        calls.append((method, path, payload))
        return {"ok": True, "attachment": {"attachment_id": "att-1"}}

    monkeypatch.setattr(client, "_direct_request", request)
    monkeypatch.setattr(client, "_direct_binary_request", binary_request, raising=False)
    accepted = await client.send_chat_attachment(
        "akane", "describe it", attachment=attachment,
        client_id="tui-1", telegram_mirror=False,
    )

    assert accepted["run_id"] == "run-1"
    assert [path for _method, path, _body in calls] == [
        "/api/v1/capabilities",
        "/api/v1/agents/akane/primary-session",
        "/api/v1/sessions/ses-1/attachments",
        "/api/v1/sessions/ses-1/attachments/att-1/content",
        "/api/v1/sessions/ses-1/attachments/att-1/commit",
        "/api/v1/sessions/ses-1/runs",
    ]
    assert calls[2][2]["sha256"] == attachment["sha256"]
    assert calls[3][2] == content
    assert calls[-1][2]["message"]["content"] == [
        {"type": "text", "text": "describe it"},
        {"type": "attachment", "attachment_id": "att-1"},
    ]
    assert calls[-1][2]["delivery_policy"]["targets"] == []


@pytest.mark.asyncio
async def test_remote_tui_attachment_uses_one_canonical_session_operation(monkeypatch):
    client = TuiApiClient(
        remote_url="http://127.0.0.1:8766", target_instance="HASHI2"
    )
    content = b"remote attachment"
    attachment = {
        "filename": "note.txt", "media_type": "text/plain",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "content_b64": base64.b64encode(content).decode("ascii"),
    }
    original_attachment = dict(attachment)
    calls = []

    async def proxy(operation, **kwargs):
        calls.append((operation, kwargs))
        if operation == "capabilities":
            return {
                "ok": True, "session_api_version": "1.0",
                "frontend_contract_versions": {"ingress": 2},
                "tui_session_ingress": {
                    "version": 1, "primary_session": True,
                    "attachment_runs": True,
                },
            }
        if operation == "primary_session":
            attachment["filename"] = "changed-after-selection.txt"
            return {
                "ok": True,
                "session": {"session_id": "ses-2", "instance_id": "HASHI2"},
            }
        if operation == "session_attachment_run":
            return {"ok": True, "run_id": "run-2", "session_id": "ses-2"}
        raise AssertionError(f"unexpected proxy operation {operation}")

    monkeypatch.setattr(client, "_proxy_request", proxy)
    result = await client.send_chat_attachment(
        "akane", "read this", attachment=attachment,
        client_id="tui-window-1", telegram_mirror=False,
    )

    assert result["run_id"] == "run-2"
    assert [operation for operation, _ in calls] == [
        "capabilities", "primary_session", "session_attachment_run"
    ]
    submitted = calls[-1][1]
    assert submitted["session_id"] == "ses-2"
    assert submitted["attachment"] == original_attachment
    assert submitted["idempotency_key"].startswith("tui-")
    assert submitted["delivery_policy"]["targets"] == []
    assert "workzone_ref" not in submitted


@pytest.mark.asyncio
async def test_remote_tui_attachment_unknown_outcome_never_replays_legacy(monkeypatch):
    client = TuiApiClient(
        remote_url="http://127.0.0.1:8766", target_instance="HASHI2"
    )
    content = b"data"
    calls = []

    async def proxy(operation, **kwargs):
        calls.append(operation)
        if operation == "capabilities":
            return {
                "ok": True, "session_api_version": "1.0",
                "frontend_contract_versions": {"ingress": 2},
                "tui_session_ingress": {
                    "version": 1, "primary_session": True,
                    "attachment_runs": True,
                },
            }
        if operation == "primary_session":
            return {"ok": True, "session": {
                "session_id": "ses-2", "instance_id": "HASHI2"
            }}
        if operation == "session_attachment_run":
            return {"ok": False, "code": "request_outcome_unknown", "accepted": None}
        raise AssertionError("the unknown Run must not be replayed")

    monkeypatch.setattr(client, "_proxy_request", proxy)
    result = await client.send_chat_attachment(
        "akane", "read", attachment={
            "filename": "note.txt", "media_type": "text/plain",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "content_b64": base64.b64encode(content).decode("ascii"),
        },
    )

    assert result["code"] == "request_outcome_unknown"
    assert calls == ["capabilities", "primary_session", "session_attachment_run"]


@pytest.mark.asyncio
async def test_remote_tui_attachment_uses_legacy_only_before_new_operation_exists(
    monkeypatch,
):
    client = TuiApiClient(
        remote_url="http://127.0.0.1:8766", target_instance="HASHI2"
    )
    content = b"data"
    calls = []

    async def proxy(operation, **kwargs):
        calls.append(operation)
        if operation == "capabilities":
            return {
                "ok": True, "session_api_version": "1.0",
                "frontend_contract_versions": {"ingress": 2},
                "tui_session_ingress": {
                    "version": 1, "primary_session": True,
                    "attachment_runs": True,
                },
            }
        if operation == "primary_session":
            return {"ok": False, "error": "operation_not_allowed"}
        if operation == "chat_attachment":
            return {"ok": True, "request_id": "legacy-1"}
        raise AssertionError(f"unexpected operation {operation}")

    monkeypatch.setattr(client, "_proxy_request", proxy)
    result = await client.send_chat_attachment(
        "akane", "read", attachment={
            "filename": "note.txt", "media_type": "text/plain",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "content_b64": base64.b64encode(content).decode("ascii"),
        },
    )
    assert result["request_id"] == "legacy-1"
    assert calls == ["capabilities", "primary_session", "chat_attachment"]


@pytest.mark.asyncio
async def test_remote_tui_attachment_never_falls_back_after_canonical_admission(
    monkeypatch,
):
    client = TuiApiClient(
        remote_url="http://127.0.0.1:8766", target_instance="HASHI2"
    )
    content = b"data"
    calls = []

    async def proxy(operation, **kwargs):
        calls.append(operation)
        if operation == "capabilities":
            return {
                "ok": True, "session_api_version": "1.0",
                "frontend_contract_versions": {"ingress": 2},
                "tui_session_ingress": {
                    "version": 1, "primary_session": True,
                    "attachment_runs": True,
                },
            }
        if operation == "primary_session":
            return {"ok": True, "session": {
                "session_id": "ses-2", "instance_id": "HASHI2"
            }}
        if operation == "session_attachment_run":
            return {
                "ok": False, "error": "operation_not_allowed",
                "canonical_operation_attempted": True,
            }
        raise AssertionError("canonical attempt must not use legacy fallback")

    monkeypatch.setattr(client, "_proxy_request", proxy)
    result = await client.send_chat_attachment(
        "akane", "read", attachment={
            "filename": "note.txt", "media_type": "text/plain",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "content_b64": base64.b64encode(content).decode("ascii"),
        },
    )
    assert result["canonical_operation_attempted"] is True
    assert calls == ["capabilities", "primary_session", "session_attachment_run"]


@pytest.mark.asyncio
async def test_direct_tui_workzone_reference_binds_session_run(monkeypatch):
    client = TuiApiClient("http://127.0.0.1:18800", expected_instance_id="HASHI1")
    calls = []

    async def request(method, path, *, json_body=None, timeout=10, pinned_base=None):
        calls.append((method, path, json_body, pinned_base))
        if path == "/api/v1/capabilities":
            return {
                "ok": True, "session_api_version": "1.0",
                "frontend_contract_versions": {"ingress": 2},
                "tui_session_ingress": {
                    "version": 1, "primary_session": True,
                    "workzone_attachment_runs": True,
                },
            }
        if path == "/api/v1/agents/akane/primary-session":
            return {"ok": True, "session": {
                "session_id": "ses-1", "instance_id": "HASHI1"
            }}
        if path == "/api/v1/sessions/ses-1/attachments/from-workzone":
            return {"ok": True, "attachment": {
                "attachment_id": "att-1", "state": "committed"
            }}
        if path == "/api/v1/sessions/ses-1/runs":
            return {"ok": True, "run_id": "run-1"}
        raise AssertionError(f"unexpected route {path}")

    monkeypatch.setattr(client, "_direct_request", request)
    result = await client.send_chat_attachment(
        "akane", "read it", workzone_ref="reports/weekly.pdf",
        client_id="tui-window-1", telegram_mirror=False,
    )

    assert result["run_id"] == "run-1"
    assert [path for _, path, _, _ in calls] == [
        "/api/v1/capabilities",
        "/api/v1/agents/akane/primary-session",
        "/api/v1/sessions/ses-1/attachments/from-workzone",
        "/api/v1/sessions/ses-1/runs",
    ]
    assert calls[2][2] == {"reference": "reports/weekly.pdf"}
    assert calls[2][3] == calls[3][3] == "http://127.0.0.1:18800"
    assert calls[-1][2]["message"]["content"] == [
        {"type": "text", "text": "read it"},
        {"type": "attachment", "attachment_id": "att-1"},
    ]


@pytest.mark.asyncio
async def test_remote_tui_workzone_reference_stays_target_relative(monkeypatch):
    client = TuiApiClient(
        remote_url="http://127.0.0.1:8766", target_instance="HASHI2"
    )
    calls = []

    async def proxy(operation, **kwargs):
        calls.append((operation, kwargs))
        if operation == "capabilities":
            return {
                "ok": True, "session_api_version": "1.0",
                "frontend_contract_versions": {"ingress": 2},
                "tui_session_ingress": {
                    "version": 1, "primary_session": True,
                    "workzone_attachment_runs": True,
                },
            }
        if operation == "primary_session":
            return {"ok": True, "session": {
                "session_id": "ses-2", "instance_id": "HASHI2"
            }}
        if operation == "session_workzone_attachment_run":
            return {"ok": True, "run_id": "run-2"}
        raise AssertionError(f"unexpected operation {operation}")

    monkeypatch.setattr(client, "_proxy_request", proxy)
    result = await client.send_chat_attachment(
        "akane", "read it", workzone_ref="reports/weekly.pdf",
        client_id="tui-window-1",
    )

    assert result["run_id"] == "run-2"
    assert [operation for operation, _ in calls] == [
        "capabilities", "primary_session", "session_workzone_attachment_run"
    ]
    assert calls[-1][1]["workzone_ref"] == "reports/weekly.pdf"
    assert calls[-1][1]["session_id"] == "ses-2"
    assert "attachment" not in calls[-1][1]


@pytest.mark.asyncio
async def test_tui_workzone_reference_rejects_absolute_and_traversal_before_io(
    monkeypatch,
):
    client = TuiApiClient("http://127.0.0.1:18800")

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid reference must not touch the server")

    monkeypatch.setattr(client, "capabilities_info", forbidden)
    for reference in ("../secret.txt", "/etc/passwd", "C:\\private.txt"):
        result = await client.send_chat_attachment(
            "akane", "read", workzone_ref=reference
        )
        assert result["code"] == "invalid_workzone_ref"


@pytest.mark.asyncio
async def test_remote_tui_workzone_unknown_outcome_does_not_replay_legacy(
    monkeypatch,
):
    client = TuiApiClient(
        remote_url="http://127.0.0.1:8766", target_instance="HASHI2"
    )
    calls = []

    async def proxy(operation, **kwargs):
        calls.append(operation)
        if operation == "capabilities":
            return {
                "ok": True, "session_api_version": "1.0",
                "frontend_contract_versions": {"ingress": 2},
                "tui_session_ingress": {
                    "version": 1, "primary_session": True,
                    "workzone_attachment_runs": True,
                },
            }
        if operation == "primary_session":
            return {"ok": True, "session": {
                "session_id": "ses-2", "instance_id": "HASHI2"
            }}
        if operation == "session_workzone_attachment_run":
            return {"ok": False, "code": "request_outcome_unknown", "accepted": None}
        raise AssertionError("uncertain write must not replay")

    monkeypatch.setattr(client, "_proxy_request", proxy)
    result = await client.send_chat_attachment(
        "akane", "read", workzone_ref="reports/weekly.pdf"
    )
    assert result["code"] == "request_outcome_unknown"
    assert calls == [
        "capabilities", "primary_session", "session_workzone_attachment_run"
    ]


@pytest.mark.asyncio
async def test_tui_workzone_reference_preserves_old_instance_chat_fallback(
    monkeypatch,
):
    client = TuiApiClient("http://127.0.0.1:18800")
    calls = []

    async def request(method, path, *, json_body=None, timeout=10):
        calls.append((method, path, json_body))
        if path == "/api/v1/capabilities":
            return {"ok": True, "session_api_version": "1.0"}
        if path == "/api/chat":
            return {"ok": True, "request_id": "legacy-workzone"}
        raise AssertionError(f"unexpected route {path}")

    monkeypatch.setattr(client, "_direct_request", request)
    result = await client.send_chat_attachment(
        "akane", "read", workzone_ref="reports/weekly.pdf"
    )
    assert result["request_id"] == "legacy-workzone"
    assert [path for _, path, _ in calls] == [
        "/api/v1/capabilities", "/api/chat"
    ]
    assert calls[-1][2]["workzone_ref"] == "reports/weekly.pdf"


@pytest.mark.asyncio
async def test_tui_workzone_reference_preserves_old_remote_proxy_fallback(
    monkeypatch,
):
    client = TuiApiClient(
        remote_url="http://127.0.0.1:8766", target_instance="HASHI2"
    )
    calls = []

    async def proxy(operation, **kwargs):
        calls.append(operation)
        if operation == "capabilities":
            return {
                "ok": True, "session_api_version": "1.0",
                "frontend_contract_versions": {"ingress": 2},
                "tui_session_ingress": {
                    "version": 1, "primary_session": True,
                    "workzone_attachment_runs": True,
                },
            }
        if operation == "primary_session":
            return {"ok": True, "session": {
                "session_id": "ses-2", "instance_id": "HASHI2"
            }}
        if operation == "session_workzone_attachment_run":
            return {"ok": False, "error": "operation_not_allowed"}
        if operation == "chat_attachment":
            return {"ok": True, "request_id": "legacy-workzone"}
        raise AssertionError(f"unexpected operation {operation}")

    monkeypatch.setattr(client, "_proxy_request", proxy)
    result = await client.send_chat_attachment(
        "akane", "read", workzone_ref="reports/weekly.pdf"
    )
    assert result["request_id"] == "legacy-workzone"
    assert calls == [
        "capabilities", "primary_session",
        "session_workzone_attachment_run", "chat_attachment",
    ]


@pytest.mark.asyncio
async def test_tui_attachment_never_replays_legacy_after_canonical_upload_uncertainty(monkeypatch):
    client = TuiApiClient("http://127.0.0.1:18800")
    calls = []
    content = b"data"
    attachment = {
        "filename": "note.txt",
        "media_type": "text/plain",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "content_b64": base64.b64encode(content).decode("ascii"),
    }

    async def request(method, path, *, json_body=None, timeout=10, pinned_base=None):
        calls.append(path)
        if path == "/api/v1/capabilities":
            return {
                "ok": True, "session_api_version": "1.0",
                "frontend_contract_versions": {"ingress": 2},
                "tui_session_ingress": {
                    "version": 1, "primary_session": True,
                    "text_runs": True, "attachment_runs": True,
                },
            }
        if path == "/api/v1/agents/akane/primary-session":
            return {"ok": True, "session": {"session_id": "ses-1"}}
        if path == "/api/v1/sessions/ses-1/attachments":
            return {"ok": True, "attachment": {"attachment_id": "att-1"}}
        raise AssertionError(f"unexpected request {path}")

    async def binary_request(method, path, *, payload, timeout=10, pinned_base=None):
        calls.append(path)
        return {"ok": False, "code": "request_outcome_unknown", "accepted": None}

    monkeypatch.setattr(client, "_direct_request", request)
    monkeypatch.setattr(client, "_direct_binary_request", binary_request, raising=False)
    result = await client.send_chat_attachment(
        "akane", "read", attachment=attachment, client_id="tui-1"
    )
    assert result["code"] == "request_outcome_unknown"
    assert calls == [
        "/api/v1/capabilities",
        "/api/v1/agents/akane/primary-session",
        "/api/v1/sessions/ses-1/attachments",
        "/api/v1/sessions/ses-1/attachments/att-1/content",
    ]


@pytest.mark.asyncio
async def test_tui_rejects_malformed_attachment_size_without_submitting(monkeypatch):
    client = TuiApiClient("http://127.0.0.1:18800")
    content = b"data"
    attachment = {
        "filename": "note.txt", "media_type": "text/plain",
        "size_bytes": "not-a-number",
        "sha256": hashlib.sha256(content).hexdigest(),
        "content_b64": base64.b64encode(content).decode("ascii"),
    }

    async def request(method, path, *, json_body=None, timeout=10, pinned_base=None):
        assert path == "/api/v1/capabilities"
        return {
            "ok": True, "session_api_version": "1.0",
            "frontend_contract_versions": {"ingress": 2},
            "tui_session_ingress": {
                "version": 1, "primary_session": True,
                "attachment_runs": True,
            },
        }

    monkeypatch.setattr(client, "_direct_request", request)
    result = await client.send_chat_attachment("akane", "read", attachment=attachment)
    assert result["code"] == "invalid_attachment"


@pytest.mark.asyncio
async def test_tui_rejects_unsafe_attachment_name_before_stage(monkeypatch):
    client = TuiApiClient("http://127.0.0.1:18800")
    content = b"data"
    attachment = {
        "media_type": "text/plain", "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "content_b64": base64.b64encode(content).decode("ascii"),
    }

    async def request(method, path, *, json_body=None, timeout=10, pinned_base=None):
        assert path == "/api/v1/capabilities"
        return {
            "ok": True, "session_api_version": "1.0",
            "frontend_contract_versions": {"ingress": 2},
            "tui_session_ingress": {
                "version": 1, "primary_session": True,
                "attachment_runs": True,
            },
        }

    monkeypatch.setattr(client, "_direct_request", request)
    for filename in ("..", "bad\nname.txt"):
        result = await client.send_chat_attachment(
            "akane", "read", attachment={**attachment, "filename": filename}
        )
        assert result["code"] == "invalid_attachment"


@pytest.mark.asyncio
async def test_remote_tui_speech_and_profile_use_local_presentation_operations(monkeypatch):
    client = TuiApiClient(
        remote_url="http://127.0.0.1:8766",
        target_instance="HASHI3",
    )
    calls = []

    async def proxy(operation, **kwargs):
        calls.append((operation, kwargs))
        return {"ok": True}

    monkeypatch.setattr(client, "_proxy_request", proxy)

    await client.voice_state("akane")
    await client.set_voice_profile("akane", "clear_female")
    await client.synthesize_speech("akane", "hello", request_id="say-1")

    assert calls == [
        ("voice_state", {"agent": "akane", "timeout": 10}),
        (
            "voice_profile",
            {"agent": "akane", "voice_profile": "clear_female", "timeout": 15},
        ),
        (
            "speech",
            {"agent": "akane", "text": "hello", "request_id": "say-1", "timeout": 130},
        ),
    ]


@pytest.mark.asyncio
async def test_remote_tui_run_status_uses_typed_proxy_fields(monkeypatch):
    client = TuiApiClient(
        remote_url="http://127.0.0.1:8766",
        target_instance="HASHI2",
    )
    captured = {}

    async def _proxy(operation, **kwargs):
        captured.update({"operation": operation, **kwargs})
        return {"ok": True, "run": {"state": "running"}}

    monkeypatch.setattr(client, "_proxy_request", _proxy)

    result = await client.run_info("session_1", "run_2")

    assert result["run"]["state"] == "running"
    assert captured == {
        "operation": "run_info",
        "session_id": "session_1",
        "run_id": "run_2",
        "timeout": 5,
    }


@pytest.mark.asyncio
async def test_sidepanel_reads_authoritative_direct_endpoints(monkeypatch):
    client = TuiApiClient("http://127.0.0.1:18800")
    calls = []

    async def _request(method, path, *, json_body=None, timeout=10):
        calls.append((method, path, json_body, timeout))
        return {"ok": True}

    monkeypatch.setattr(client, "_direct_request", _request)

    await client.agent_overview("agent name")
    await client.scheduler_jobs("agent name")
    await client.background_jobs("agent name", limit=7)

    assert calls == [
        ("GET", "/api/agents/agent%20name/overview", None, 8),
        ("GET", "/api/agents/agent%20name/scheduler/jobs", None, 8),
        ("GET", "/api/background-jobs?agent=agent%20name&limit=7", None, 8),
    ]


@pytest.mark.asyncio
async def test_sidepanel_reads_use_typed_remote_proxy_operations(monkeypatch):
    client = TuiApiClient(
        remote_url="http://127.0.0.1:8766",
        target_instance="HASHI2",
    )
    calls = []

    async def _proxy(operation, **kwargs):
        calls.append((operation, kwargs))
        return {"ok": True}

    monkeypatch.setattr(client, "_proxy_request", _proxy)

    await client.agent_overview("akane")
    await client.scheduler_jobs("akane")
    await client.background_jobs("akane", limit=7)

    assert calls == [
        ("agent_overview", {"agent": "akane", "timeout": 8}),
        ("scheduler_jobs", {"agent": "akane", "timeout": 8}),
        ("background_jobs", {"agent": "akane", "limit": 7, "timeout": 8}),
    ]
