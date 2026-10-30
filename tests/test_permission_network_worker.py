"""Fixed HTTP contract checks; no server, business process or live request starts."""
import json
import socket

import pytest

from orchestrator.permissions.descriptors import describe_tool
from orchestrator.permissions.network_worker import (
    RequestNotSent, _PinnedResolver, address_permitted, build_options, origin, prepare, run,
)


def options(*, response_fields=()):
    return build_options({"credentials": {"reports": {
        "origin": "https://api.example.com", "methods": ["GET"], "secret_ref": "reports-key",
        "header": "Authorization", "prefix": "Bearer ", "response_fields": list(response_fields),
    }}}, {"reports-key": "protected-business-canary"})


def test_credential_capability_is_exact_and_has_a_separate_permission_scope(tmp_path):
    arguments = {"url": "https://api.example.com/report", "credential": "reports"}
    plan = prepare("http_request", arguments, options())
    assert plan["headers"] == {"Authorization": "Bearer protected-business-canary"}
    assert plan["response_fields"] == []
    descriptor = describe_tool("http_request", arguments, cwd=tmp_path, read_roots=(tmp_path,))
    assert {scope.kind for scope in descriptor.scopes} == {"network", "capability"}
    assert any(scope.resource == "credential:reports" and scope.actions == ("use",) for scope in descriptor.scopes)
    for bad in (
        {**arguments, "url": "https://other.example.com/report"},
        {**arguments, "method": "POST"},
        {"url": arguments["url"], "headers": {"Authorization": "copied-raw-token"}},
        {"url": arguments["url"], "headers": {"Host": "other.example.com"}},
        {"url": "https://user:password@api.example.com"},
    ):
        with pytest.raises(RequestNotSent):
            prepare("http_request", bad, options())


@pytest.mark.parametrize("ip", ["127.0.0.1", "::1", "169.254.169.254", "0.0.0.0", "::", "224.0.0.1"])
def test_loopback_metadata_and_non_unicast_targets_remain_forbidden(ip):
    assert address_permitted(ip, True) is False
    assert address_permitted(ip, False) is False


@pytest.mark.asyncio
async def test_pinned_dns_rejects_another_host_or_port_and_private_resolution():
    resolver = _PinnedResolver("api.example.com", 443, [{"host": "93.184.216.34"}])
    assert await resolver.resolve("api.example.com", 443) == [{"host": "93.184.216.34"}]
    with pytest.raises(RequestNotSent):
        await resolver.resolve("other.example.com", 443)
    with pytest.raises(RequestNotSent):
        await resolver.resolve("api.example.com", 80)
    assert address_permitted("10.20.30.40", False) is False
    assert address_permitted("10.20.30.40", True) is True
    assert origin("https://api.example.com/a?b=c") == origin("https://api.example.com")


@pytest.mark.asyncio
async def test_fixed_http_disables_redirects_cookies_proxies_and_filters_credential_response(monkeypatch):
    import asyncio
    import aiohttp

    calls = []
    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)

    class Content:
        async def iter_chunked(self, size):
            yield json.dumps({"result": "saved", "token": "protected-business-canary"}).encode()

    class Response:
        status = 302
        headers = {"Location": "https://other.example.com", "Set-Cookie": "approval-cookie"}
        content = Content()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    class Session:
        def __init__(self, **kwargs):
            self.connector = kwargs["connector"]
            assert kwargs["trust_env"] is False and isinstance(kwargs["cookie_jar"], aiohttp.DummyCookieJar)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            await self.connector.close()
        def request(self, method, url, **kwargs):
            assert kwargs["allow_redirects"] is False
            calls.append((method, url))
            return Response()

    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    plan = prepare("http_request", {"url": "https://api.example.com/report", "credential": "reports"},
                   options(response_fields=("result", "token")))
    result = json.loads(await run(plan))
    assert calls == [("GET", "https://api.example.com/report")]
    assert result["redirect_followed"] is False
    assert "Set-Cookie" not in result["headers"]
    assert "protected-business-canary" not in result["body"]
    assert json.loads(result["body"]) == {"result": "saved", "token": "[credential redacted]"}
