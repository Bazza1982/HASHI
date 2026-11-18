"""Fixed HTTP contract checks; no server, business process or live request starts."""
import json
import socket

import pytest

from orchestrator.permissions.descriptors import describe_tool
from orchestrator.permissions.network_worker import (
    RequestNotSent, _PinnedResolver, address_permitted, build_options, origin, prepare, run,
)


def options(*, response_fields=None, secret="protected-business-canary"):
    credential = {
        "origin": "https://api.example.com", "methods": ["GET"], "secret_ref": "reports-key",
        "header": "Authorization", "prefix": "Bearer ",
    }
    if response_fields is not None:
        credential["response_fields"] = list(response_fields)
    return build_options({"credentials": {"reports": credential}}, {"reports-key": secret})


def test_credential_capability_is_exact_and_has_a_separate_permission_scope(tmp_path):
    arguments = {"url": "https://api.example.com/report", "credential": "reports"}
    plan = prepare("http_request", arguments, options())
    assert plan["headers"] == {"Authorization": "Bearer protected-business-canary"}
    assert plan["response_fields"] is None
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


@pytest.mark.parametrize("ip", ["169.254.169.254", "0.0.0.0", "::", "224.0.0.1"])
def test_metadata_and_non_unicast_targets_remain_forbidden(ip):
    assert address_permitted(ip, True) is False
    assert address_permitted(ip, False) is False
    assert address_permitted(ip, True, True) is False


@pytest.mark.parametrize("ip", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_loopback_requires_its_own_explicit_origin(ip):
    assert address_permitted(ip, True) is False
    assert address_permitted(ip, False) is False
    assert address_permitted(ip, True, True) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
async def test_installed_exact_loopback_service_is_reachable_without_expanding_scope(host):
    from aiohttp import web

    async def report(_request):
        return web.json_response({"items": [1, 2]})

    app = web.Application()
    app.router.add_get("/report", report)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, 0)
    try:
        try:
            await site.start()
        except OSError:
            pytest.skip(f"loopback address {host} is unavailable")
        port = site._server.sockets[0].getsockname()[1]
        name = f"[{host}]" if ":" in host else host
        allowed = f"http://{name}:{port}"
        policy = build_options({"private_origins": [allowed]}, {})
        result = json.loads(await run(prepare("http_request", {"url": allowed + "/report"}, policy)))
        assert result["status"] == 200
        assert json.loads(result["body"]) == {"items": [1, 2]}
        other_port = 1 if port != 1 else 2
        other = f"http://{name}:{other_port}/report"
        with pytest.raises(RequestNotSent, match="network boundary"):
            await run(prepare("http_request", {"url": other}, policy))
        blocked = build_options({"private_origins": [allowed],
                                 "forbidden_origins": [f"http://localhost:{port}"]}, {})
        with pytest.raises(RequestNotSent, match="control plane"):
            prepare("http_request", {"url": allowed + "/report"}, blocked)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("host, resolved", [
    ("business.example", "127.0.0.1"),
    ("localhost", "93.184.216.34"),
])
async def test_installed_name_cannot_rebind_across_loopback_boundary(monkeypatch, host, resolved):
    import asyncio

    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (resolved, 9000))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    target = f"http://{host}:9000"
    plan = prepare("http_request", {"url": target}, build_options({"private_origins": [target]}, {}))
    with pytest.raises(RequestNotSent, match="network boundary"):
        await run(plan)


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
@pytest.mark.parametrize("secret", ["protected-business-canary", '密钥"\\\\canary'])
@pytest.mark.parametrize("fields, expected", [
    (None, {"result": {"items": [{"id": 7, "token": "[credential redacted]"}],
                       "meta": {"count": 1}}, "token": "[credential redacted]"}),
    (("result.items", "result.meta"), {"result.items": [{"id": 7, "token": "[credential redacted]"}],
                                       "result.meta": {"count": 1}}),
    ((), {}),
])
async def test_fixed_http_disables_redirects_cookies_proxies_and_filters_credential_response(
    monkeypatch, fields, expected, secret
):
    import asyncio
    import aiohttp

    calls = []
    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)

    class Content:
        async def iter_chunked(self, size):
            yield json.dumps({
                "result": {"items": [{"id": 7, "token": secret}],
                           "meta": {"count": 1}},
                "token": secret,
            }).encode()

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
                   options(response_fields=fields, secret=secret))
    result = json.loads(await run(plan))
    assert calls == [("GET", "https://api.example.com/report")]
    assert result["redirect_followed"] is False
    assert "Set-Cookie" not in result["headers"]
    assert secret not in result["body"]
    assert json.dumps(secret, ensure_ascii=True)[1:-1] not in result["body"]
    assert json.loads(result["body"]) == expected

@pytest.mark.asyncio
async def test_selected_same_origin_redirect_preserves_scope_and_never_forwards_to_another_origin():
    from aiohttp import web

    seen = []

    async def step(request):
        seen.append((request.method, request.path))
        if request.path == "/same":
            raise web.HTTPFound("/done?selected=yes")
        if request.path == "/cross":
            raise web.HTTPFound(other_origin + "/foreign")
        if request.path == "/loop":
            raise web.HTTPFound("/loop")
        if request.path == "/post":
            raise web.HTTPTemporaryRedirect("/done")
        if request.path.startswith("/chain/"):
            count = int(request.path.rsplit("/", 1)[1])
            raise web.HTTPFound("/chain/" + str(count + 1))
        return web.json_response({"path": request.path, "query": request.query_string})

    async def foreign(request):
        seen.append(("FOREIGN", request.path))
        return web.Response(text="must not receive request")

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", step)
    other = web.Application()
    other.router.add_route("*", "/{tail:.*}", foreign)
    runner, other_runner = web.AppRunner(app), web.AppRunner(other)
    await runner.setup()
    await other_runner.setup()
    site, other_site = web.TCPSite(runner, "127.0.0.1", 0), web.TCPSite(other_runner, "127.0.0.1", 0)
    await site.start()
    await other_site.start()
    target = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    other_origin = f"http://127.0.0.1:{other_site._server.sockets[0].getsockname()[1]}"
    policy = build_options({"private_origins": [target, other_origin]}, {})
    try:
        def plan(path, **extra):
            return prepare("http_request", {"url": target + path, **extra}, policy)

        default = json.loads(await run(plan("/same")))
        assert default["status"] == 302 and default["redirect_followed"] is False
        result = json.loads(await run(plan("/same", follow_redirects=True)))
        assert result["status"] == 200 and result["redirect_followed"] is True
        assert json.loads(result["body"]) == {"path": "/done", "query": "selected=yes"}
        assert seen[:3] == [("GET", "/same"), ("GET", "/same"), ("GET", "/done")]

        cross = json.loads(await run(plan("/cross", follow_redirects=True)))
        assert cross["status"] == 302 and cross["redirect_followed"] is False
        assert cross["redirect_stop_reason"] == "different_origin"
        assert not any(method == "FOREIGN" for method, _ in seen)

        before = len(seen)
        loop = json.loads(await run(plan("/loop", follow_redirects=True)))
        assert loop["status"] == 302 and loop["redirect_stop_reason"] == "loop"
        assert len(seen) == before + 1

        before = len(seen)
        mutation = json.loads(await run(plan("/post", method="POST", follow_redirects=True)))
        assert mutation["status"] == 307 and mutation["redirect_stop_reason"] == "method_not_replayed"
        assert seen[before:] == [("POST", "/post")]
        before = len(seen)
        with_body = json.loads(await run(plan("/same", body="body", follow_redirects=True)))
        assert with_body["status"] == 302 and with_body["redirect_stop_reason"] == "method_not_replayed"
        assert seen[before:] == [("GET", "/same")]

        before = len(seen)
        limited = json.loads(await run(plan("/chain/0", follow_redirects=True, max_redirects=1)))
        assert limited["status"] == 302 and limited["redirect_stop_reason"] == "limit"
        assert seen[before:] == [("GET", "/chain/0"), ("GET", "/chain/1")]

        before = len(seen)
        head = json.loads(await run(plan("/same", method="HEAD", follow_redirects=True)))
        assert head["status"] == 200 and head["redirect_followed"] is True
        assert head["body"] == "" and seen[before:] == [("HEAD", "/same"), ("HEAD", "/done")]
    finally:
        await runner.cleanup()
        await other_runner.cleanup()


def test_redirect_selection_requires_boolean_and_positive_budget():
    policy = build_options({}, {})
    for values in ({"follow_redirects": "true"}, {"follow_redirects": True, "max_redirects": 0},
                   {"follow_redirects": True, "max_redirects": True}, {"session_cookies": "true"}):
        with pytest.raises(RequestNotSent):
            prepare("http_request", {"url": "https://api.example.com/report", **values}, policy)


@pytest.mark.asyncio
async def test_installed_credential_redirect_stays_same_origin_and_never_reaches_foreign_host(monkeypatch):
    import asyncio
    import aiohttp

    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    calls = []

    class Content:
        async def iter_chunked(self, size):
            yield b'{"ok":true}'

    class Response:
        def __init__(self, status, location=None):
            self.status = status
            self.headers = {"Location": location} if location else {}
            self.content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Session:
        def __init__(self, **kwargs):
            self.connector = kwargs["connector"]
            assert kwargs["trust_env"] is False
            assert isinstance(kwargs["cookie_jar"], aiohttp.DummyCookieJar)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.connector.close()

        def request(self, method, url, **kwargs):
            assert kwargs["allow_redirects"] is False
            calls.append((method, url, dict(kwargs["headers"])))
            if url.endswith("/start"):
                return Response(302, "https://api.example.com.:443/next")
            if url.endswith("/next"):
                return Response(302, "https://other.example.com/steal")
            raise AssertionError("a different origin must not receive credentials")

    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    plan = prepare("http_request", {
        "url": "https://api.example.com/start", "credential": "reports", "follow_redirects": True,
    }, options())
    result = json.loads(await run(plan))
    assert result["status"] == 302 and result["redirect_followed"] is True
    assert result["redirect_stop_reason"] == "different_origin"
    assert [url for _, url, _ in calls] == [
        "https://api.example.com/start", "https://api.example.com/next",
    ]
    assert all(headers["Authorization"] == "Bearer protected-business-canary" for _, _, headers in calls)
    assert "protected-business-canary" not in json.dumps(result)


def test_fixed_helper_executes_and_renders_the_scoped_redirect_plan(tmp_path):
    import base64
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from orchestrator.permissions.file_results import render_helper_result
    from orchestrator.permissions.file_worker import run as run_helper

    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            paths.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(303)
            self.send_header("Location", "/private")
            self.send_header("Set-Cookie", "sid=fixed-helper-session; HttpOnly")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            paths.append(self.path)
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/finish")
                self.end_headers()
                return
            if self.path == "/private":
                assert self.headers.get("Cookie") == "sid=fixed-helper-session"
                body = b"session:fixed-helper-session"
            else:
                body = b"fixed helper result"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    target = f"http://127.0.0.1:{server.server_port}"
    arguments = {"url": target + "/start", "follow_redirects": True}
    policy = build_options({"private_origins": [target]}, {})
    descriptor = describe_tool("http_request", arguments, cwd=tmp_path, read_roots=(tmp_path,))
    options = prepare("http_request", arguments, policy)
    helper_plan = {
        "format": "hashi-file-helper-v1", "action": "http", "handles": [],
        "directory": "", "name": "", "data": base64.b64encode(json.dumps(options).encode()).decode(),
        "result": str(tmp_path / "unused.json"),
    }
    try:
        saved = run_helper(helper_plan)
        result = json.loads(render_helper_result(descriptor, arguments, saved))
        login_arguments = {"url": target + "/login", "method": "POST", "body": "fixture",
                           "follow_redirects": True, "session_cookies": True}
        login_descriptor = describe_tool("http_request", login_arguments, cwd=tmp_path, read_roots=(tmp_path,))
        login_options = prepare("http_request", login_arguments, policy)
        login_plan = {**helper_plan, "data": base64.b64encode(json.dumps(login_options).encode()).decode()}
        login_saved = run_helper(login_plan)
        login_result = json.loads(render_helper_result(login_descriptor, login_arguments, login_saved))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert result["status"] == 200
    assert result["body"] == "fixed helper result" and result["redirect_followed"] is True
    assert paths == ["/start", "/finish", "/login", "/private"]
    assert login_result["status"] == 200 and login_result["redirect_followed"] is True
    assert login_result["body"] == "session:[credential redacted]"
    assert "fixed-helper-session" not in json.dumps(login_result)


@pytest.mark.asyncio
async def test_selected_same_origin_login_cookie_completes_once_and_is_redacted(tmp_path):
    from urllib.parse import quote

    from aiohttp import web

    requests = []
    cookie_value = "synthetic/session+secret"

    async def login(request):
        requests.append((request.method, request.path))
        destination = {"/login": "/account", "/trunc-login": "/partial"}.get(
            request.path, foreign_origin + "/steal"
        )
        result = web.HTTPSeeOther(destination)
        result.set_cookie("sid", cookie_value, httponly=True)
        raise result

    async def account(request):
        requests.append((request.method, request.path))
        assert request.method == "GET" and await request.read() == b""
        assert request.headers.get("Content-Type") is None
        value = request.cookies.get("sid")
        if not value:
            return web.json_response({"error": "no session"}, status=401)
        if request.path == "/account":
            raise web.HTTPFound("/welcome")
        result = web.json_response({"message": "welcome", "observed_session": value,
                                    "encoded_session": quote(value, safe="")})
        result.del_cookie("sid")
        return result

    async def partial(request):
        requests.append((request.method, request.path))
        assert request.cookies.get("sid") == cookie_value
        result = web.Response(text="x" * (512 * 1024 - 14) + cookie_value[:14] + "later bytes")
        result.del_cookie("sid")
        return result

    async def foreign(request):
        requests.append(("FOREIGN", request.path))
        return web.Response(text="must not be contacted")

    app = web.Application()
    app.router.add_post("/login", login)
    app.router.add_post("/cross-login", login)
    app.router.add_post("/trunc-login", login)
    app.router.add_get("/partial", partial)
    app.router.add_get("/account", account)
    app.router.add_get("/welcome", account)
    other = web.Application()
    other.router.add_route("*", "/{tail:.*}", foreign)
    runner, other_runner = web.AppRunner(app), web.AppRunner(other)
    await runner.setup()
    await other_runner.setup()
    site, other_site = web.TCPSite(runner, "127.0.0.1", 0), web.TCPSite(other_runner, "127.0.0.1", 0)
    await site.start()
    await other_site.start()
    target = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    foreign_origin = f"http://127.0.0.1:{other_site._server.sockets[0].getsockname()[1]}"
    policy = build_options({"private_origins": [target, foreign_origin]}, {})
    try:
        arguments = {"url": target + "/login", "method": "POST", "body": "synthetic-login",
                     "follow_redirects": True, "session_cookies": True}
        descriptor = describe_tool("http_request", arguments, cwd=tmp_path, read_roots=(tmp_path,))
        assert set(descriptor.scopes[0].actions) == {"fetch", "send", "upload"}
        result = json.loads(await run(prepare("http_request", arguments, policy)))
        assert result["status"] == 200 and result["redirect_followed"] is True
        assert result["redirects"] == [{"status": 303, "method": "POST"}, {"status": 302, "method": "GET"}]
        assert json.loads(result["body"]) == {"message": "welcome", "observed_session": "[credential redacted]",
                                               "encoded_session": "[credential redacted]"}
        assert cookie_value not in json.dumps(result)
        assert requests == [("POST", "/login"), ("GET", "/account"), ("GET", "/welcome")]

        requests.clear()
        no_session = json.loads(await run(prepare("http_request", {
            **arguments, "session_cookies": False,
        }, policy)))
        assert no_session["status"] == 401
        assert requests == [("POST", "/login"), ("GET", "/account")]

        requests.clear()
        partial_result = json.loads(await run(prepare("http_request", {
            **arguments, "url": target + "/trunc-login",
        }, policy)))
        assert partial_result["truncated"] is True
        assert partial_result["body"].endswith("[credential redacted]")
        assert cookie_value[:14] not in partial_result["body"]
        assert requests == [("POST", "/trunc-login"), ("GET", "/partial")]

        requests.clear()
        cross = json.loads(await run(prepare("http_request", {
            **arguments, "url": target + "/cross-login",
        }, policy)))
        assert cross["status"] == 303 and cross["redirect_stop_reason"] == "different_origin"
        assert requests == [("POST", "/cross-login")]
        assert cookie_value not in json.dumps(cross)
    finally:
        await runner.cleanup()
        await other_runner.cleanup()


@pytest.mark.asyncio
async def test_303_does_not_expand_an_installed_credentials_allowed_methods(monkeypatch):
    import asyncio
    import aiohttp

    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    calls = []

    class Content:
        async def iter_chunked(self, size):
            yield b"redirect"

    class Response:
        status = 303
        headers = {"Location": "/next"}
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Session:
        def __init__(self, **kwargs):
            self.connector = kwargs["connector"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.connector.close()

        def request(self, method, url, **kwargs):
            calls.append((method, url))
            return Response()

    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    policy = build_options({"credentials": {"submit": {
        "origin": "https://api.example.com", "methods": ["POST"], "secret_ref": "key",
        "header": "Authorization", "prefix": "Bearer ",
    }}}, {"key": "synthetic-method-limited-secret"})
    result = json.loads(await run(prepare("http_request", {
        "url": "https://api.example.com/start", "method": "POST", "credential": "submit",
        "body": "fixture", "follow_redirects": True,
    }, policy)))
    assert result["status"] == 303 and result["redirect_stop_reason"] == "credential_method_not_granted"
    assert calls == [("POST", "https://api.example.com/start")]
    assert "synthetic-method-limited-secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_complete_overlapping_credential_at_capture_boundary_is_fully_redacted(monkeypatch):
    import asyncio
    import aiohttp

    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)

    class Content:
        async def iter_chunked(self, size):
            yield b"x" * (512 * 1024 - 5) + b"abcab" + b"later bytes"

    class Response:
        status = 200
        headers = {}
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Session:
        def __init__(self, **kwargs):
            self.connector = kwargs["connector"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.connector.close()

        def request(self, method, url, **kwargs):
            return Response()

    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    result = json.loads(await run(prepare("http_request", {
        "url": "https://api.example.com/report", "credential": "reports",
    }, options(secret="abcab"))))
    assert result["truncated"] is True
    assert result["body"].endswith("[credential redacted]")
    assert "abc" not in result["body"]

# HASHI-Fingerprint: hfp_faffa27d4a724bc3aff617da8c7b6501



@pytest.mark.asyncio
async def test_managed_http_streams_an_authorized_file_and_preserves_redirect_scope(tmp_path):
    """A selected file is read in pages rather than embedded in a size-limited plan."""
    from hashlib import sha256
    from aiohttp import web

    source = tmp_path / "upload.bin"
    payload = bytes(range(256)) * 10000
    source.write_bytes(payload)
    seen = []

    async def upload(request):
        digest = sha256()
        count = 0
        async for chunk in request.content.iter_chunked(32768):
            digest.update(chunk)
            count += len(chunk)
        seen.append((request.method, request.path, count, digest.hexdigest()))
        assert request.headers.get("Content-Length") == str(len(payload))
        response = web.HTTPSeeOther("/finished")
        response.set_cookie("sid", "synthetic-upload-cookie", httponly=True)
        raise response

    async def finished(request):
        assert request.headers.get("Content-Length") is None
        assert await request.read() == b""
        assert request.cookies.get("sid") == "synthetic-upload-cookie"
        seen.append((request.method, request.path, 0, ""))
        return web.json_response({"accepted": len(payload), "sha256": sha256(payload).hexdigest(),
                                  "session": request.cookies["sid"]})

    app = web.Application()
    app.router.add_post("/upload", upload)
    app.router.add_get("/finished", finished)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    target = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    args = {"url": target + "/upload", "method": "POST", "body_file": str(source),
            "follow_redirects": True, "session_cookies": True}
    descriptor = describe_tool("http_request", args, cwd=tmp_path, read_roots=(tmp_path,))
    assert {scope.kind for scope in descriptor.scopes} == {"file", "network"}
    file_scope = next(scope for scope in descriptor.scopes if scope.kind == "file")
    assert file_scope.resource.casefold() == str(source.resolve()).casefold() and file_scope.actions == ("read",)
    net_scope = next(scope for scope in descriptor.scopes if scope.kind == "network")
    assert set(net_scope.actions) == {"send", "upload", "fetch"}

    class FixtureBoundFile:
        def __init__(self):
            self.pages = []
        def identity(self):
            return {"size": source.stat().st_size}
        def read_at(self, *, offset, max_bytes):
            self.pages.append(max_bytes)
            with source.open("rb") as stream:
                stream.seek(offset)
                return stream.read(max_bytes)

    held = FixtureBoundFile()
    try:
        plan = prepare("http_request", args, build_options({"private_origins": [target]}, {}))
        assert plan["body_source"] == "bound"
        assert str(source) not in json.dumps(plan)
        result = json.loads(await run(plan, body_reader=held))
        assert result["status"] == 200 and result["redirect_followed"] is True
        assert json.loads(result["body"]) == {
            "accepted": len(payload), "sha256": sha256(payload).hexdigest(),
            "session": "[credential redacted]",
        }
        assert "synthetic-upload-cookie" not in json.dumps(result)
        assert seen == [("POST", "/upload", len(payload), sha256(payload).hexdigest()),
                        ("GET", "/finished", 0, "")]
        assert len(held.pages) > 1 and max(held.pages) <= 1024 * 1024
        with pytest.raises(RequestNotSent):
            await run(plan)
    finally:
        await runner.cleanup()


def test_managed_http_file_body_is_explicit_and_cannot_replace_a_text_body(tmp_path):
    target = "https://api.example.com/upload"
    source = str(tmp_path / "payload.bin")
    options = build_options({}, {})
    for invalid in (
        {"url": target, "body_file": source, "body": "another body"},
        {"url": target, "body_file": ""},
        {"url": target, "body_file": 7},
    ):
        with pytest.raises(RequestNotSent):
            prepare("http_request", invalid, options)
    get = describe_tool("http_request", {"url": target, "body_file": source},
                        cwd=tmp_path, read_roots=(tmp_path,))
    network = next(scope for scope in get.scopes if scope.kind == "network")
    assert "upload" in network.actions
    assert any(scope.kind == "file" and scope.actions == ("read",) for scope in get.scopes)



def test_native_windows_held_file_streams_through_fixed_http_helper(tmp_path):
    import base64
    import os
    import threading
    from hashlib import sha256
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    if os.name != "nt":
        pytest.skip("the real held-file helper is Windows-only")
    from orchestrator.permissions.file_results import render_helper_result
    from orchestrator.permissions.file_worker import run as run_helper
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.windows_file_binding import open_bound_read
    from orchestrator.permissions.windows_process import inheritable_handle_copy

    content = bytes(range(256)) * 12000
    source = tmp_path / "large-validated.bin"
    source.write_bytes(content)
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            remaining = int(self.headers["Content-Length"])
            digest = sha256()
            while remaining:
                block = self.rfile.read(min(65536, remaining))
                if not block:
                    break
                remaining -= len(block)
                digest.update(block)
            received.append((remaining, digest.hexdigest()))
            response = json.dumps({"sha256": digest.hexdigest()}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    target = f"http://127.0.0.1:{server.server_port}"
    args = {"url": target + "/upload", "method": "POST", "body_file": str(source)}
    descriptor = describe_tool("http_request", args, cwd=tmp_path, read_roots=(tmp_path,))
    plan = prepare("http_request", args, build_options({"private_origins": [target]}, {}))
    try:
        with open_bound_read(normalize_scope("file", str(source), ["read"], cwd=tmp_path),
                             exclusive=True) as approved:
            _kernel, duplicate = inheritable_handle_copy(approved._handles[-1])
            helper = {
                "format": "hashi-file-helper-v1", "action": "http", "handles": [duplicate],
                "directory": "", "name": "",
                "data": base64.b64encode(json.dumps(plan).encode()).decode(),
                "result": str(tmp_path / "unused.json"),
            }
            result = json.loads(render_helper_result(descriptor, args, run_helper(helper)))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert result["status"] == 200
    assert json.loads(result["body"]) == {"sha256": sha256(content).hexdigest()}
    assert received == [(0, sha256(content).hexdigest())]
    assert source.read_bytes() == content



def test_fixed_http_helper_rejects_missing_or_unrequested_file_handle(tmp_path):
    import base64

    from orchestrator.permissions.file_worker import FilePlanRejected, run as run_helper

    base = {
        "format": "hashi-file-helper-v1", "action": "http", "handles": [],
        "directory": "", "name": "", "result": str(tmp_path / "unused.json"),
    }
    bound_plan = prepare("http_request", {"url": "https://api.example.com", "body_file": "fixture"},
                         build_options({}, {}))
    encoded = base64.b64encode(json.dumps(bound_plan).encode()).decode()
    with pytest.raises(FilePlanRejected):
        run_helper({**base, "data": encoded})

    ordinary_plan = prepare("http_request", {"url": "https://api.example.com"}, build_options({}, {}))
    encoded = base64.b64encode(json.dumps(ordinary_plan).encode()).decode()
    with pytest.raises(FilePlanRejected):
        run_helper({**base, "handles": [123], "data": encoded})



@pytest.mark.asyncio
async def test_interrupted_file_upload_is_not_reported_as_a_safe_preflight_failure(tmp_path):
    import asyncio
    import threading
    from aiohttp import web

    observed = []
    first_received = threading.Event()

    async def upload(request):
        try:
            async for chunk in request.content.iter_chunked(65536):
                observed.append(len(chunk))
                first_received.set()
        except (ConnectionError, asyncio.CancelledError):
            pass
        return web.Response(text="not complete")

    app = web.Application()
    app.router.add_post("/upload", upload)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    target = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    class InterruptedFile:
        def identity(self):
            return {"size": 2 * 1024 * 1024}

        def read_at(self, *, offset, max_bytes):
            if offset == 0:
                return b"x" * max_bytes
            if not first_received.wait(5):
                raise OSError("fixture server did not receive the first page")
            raise OSError("synthetic read failure after send began")

    try:
        plan = prepare(
            "http_request",
            {"url": target + "/upload", "method": "POST", "body_file": str(tmp_path / "body")},
            build_options({"private_origins": [target]}, {}),
        )
        with pytest.raises(Exception) as caught:
            await run(plan, body_reader=InterruptedFile())
        assert not isinstance(caught.value, RequestNotSent)
        assert sum(observed) > 0
    finally:
        await runner.cleanup()


def test_fixed_http_helper_marks_a_post_dispatch_failure_unknown(tmp_path, monkeypatch):
    import base64
    import hashlib
    import sys

    from orchestrator.permissions import file_worker, network_worker

    async def interrupted(_plan):
        raise OSError("fixture failure after request may have started")

    monkeypatch.setattr(network_worker, "run", interrupted)
    result_path = tmp_path / "result.json"
    network = prepare("http_request", {"url": "https://api.example.com"}, build_options({}, {}))
    plan = {
        "format": "hashi-file-helper-v1", "action": "http", "handles": [],
        "directory": "", "name": "", "data": base64.b64encode(json.dumps(network).encode()).decode(),
        "result": str(result_path),
    }
    payload = json.dumps(plan).encode()
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(payload)
    monkeypatch.setattr(sys, "argv", ["helper", str(plan_path), hashlib.sha256(payload).hexdigest()])
    file_worker.main()
    assert json.loads(result_path.read_text(encoding="utf-8")) == {
        "outcome": "unknown", "error_type": "OSError",
    }



@pytest.mark.asyncio
async def test_http_preview_reports_truncation_only_when_bytes_were_omitted():
    import base64
    from aiohttp import web

    size = 512 * 1024

    async def content(request):
        extra = 1 if request.path == "/longer" else 0
        return web.Response(body=b"x" * (size + extra))

    app = web.Application()
    app.router.add_get("/{tail:.*}", content)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    target = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    policy = build_options({"private_origins": [target]}, {})
    try:
        for endpoint, truncated in (("/exact", False), ("/longer", True)):
            result = json.loads(await run(prepare("http_request", {
                "url": target + endpoint, "response_encoding": "base64",
            }, policy)))
            assert result["truncated"] is truncated
            assert result["captured_bytes"] == size
            assert base64.b64decode(result["body"], validate=True) == b"x" * size
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["text", "base64"])
@pytest.mark.parametrize(
    "secret,body,status,forbidden",
    [
        ("credential", "the credential is credential", 200, ("credential",)),
        ("a[credential redacted]b", "aa[credential redacted]bb", 200,
         ("a[credential redacted]b",)),
        ("abcdefghij", "fghij visible abcde", 206, ("fghij", "abcde")),
    ],
)
async def test_text_credential_redaction_handles_marker_and_range_edges(
    monkeypatch, secret, body, status, forbidden, encoding
):
    import asyncio
    import base64
    import aiohttp

    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)

    class Content:
        async def iter_chunked(self, size):
            yield body.encode("utf-8")

    class Response:
        headers = {"Location": "https://api.example.com/" + secret}
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    Response.status = status

    class Session:
        def __init__(self, **kwargs):
            self.connector = kwargs["connector"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.connector.close()

        def request(self, method, url, **kwargs):
            return Response()

    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    result = json.loads(await run(prepare(
        "http_request", {
            "url": "https://api.example.com/report", "credential": "reports",
            "response_encoding": encoding,
        },
        options(secret=secret),
    )))
    value = (base64.b64decode(result["body"]).decode("utf-8")
             if encoding == "base64" else result["body"])
    assert all(fragment not in value for fragment in forbidden)
    assert secret not in result["headers"].get("Location", "")
    assert result["body_redacted"] is True
    if status == 206:
        assert " visible " in value



@pytest.mark.asyncio
async def test_installed_credential_preserves_nested_results_over_verified_tls(tmp_path, monkeypatch):
    """Exercise real TLS and HTTP while keeping credential and target synthetic."""
    import ipaddress
    import ssl
    from datetime import datetime, timedelta, timezone

    import aiohttp.connector
    from aiohttp import web
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                       critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "test-ca.pem", tmp_path / "test-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    client_context = ssl.create_default_context(cafile=str(cert_path))
    monkeypatch.setattr(aiohttp.connector, "_SSL_CONTEXT_VERIFIED", client_context)

    secret = "synthetic-business-credential"
    calls = []

    async def report(request):
        calls.append((request.method, request.path))
        assert request.headers.get("Authorization") == "Bearer " + secret
        return web.json_response({
            "result": {"items": [{"id": 7, "token": secret}], "meta": {"count": 1}},
            "token": secret,
        })

    app = web.Application()
    app.router.add_get("/report", report)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_context)
    await site.start()
    origin_url = f"https://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    def installed(fields):
        credential = {
            "origin": origin_url, "methods": ["GET"], "secret_ref": "test-key",
            "header": "Authorization", "prefix": "Bearer ",
        }
        if fields is not None:
            credential["response_fields"] = fields
        return build_options(
            {"private_origins": [origin_url], "credentials": {"reports": credential}},
            {"test-key": secret},
        )

    try:
        expected = [
            (None, {"result": {"items": [{"id": 7, "token": "[credential redacted]"}],
                               "meta": {"count": 1}}, "token": "[credential redacted]"}),
            (["result.items", "result.meta"],
             {"result.items": [{"id": 7, "token": "[credential redacted]"}], "result.meta": {"count": 1}}),
            ([], {}),
        ]
        for fields, wanted in expected:
            result = json.loads(await run(prepare("http_request", {
                "url": origin_url + "/report", "credential": "reports",
            }, installed(fields))))
            assert result["status"] == 200 and not result["truncated"]
            assert json.loads(result["body"]) == wanted
            assert secret not in json.dumps(result)
        assert calls == [("GET", "/report")] * 3
        with pytest.raises(RequestNotSent, match="exact target"):
            prepare("http_request", {
                "url": "https://127.0.0.1:1/report", "credential": "reports",
            }, installed(None))
        # A correctly scoped origin still cannot receive the credential when
        # its server certificate is not trusted.
        monkeypatch.setattr(aiohttp.connector, "_SSL_CONTEXT_VERIFIED", ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
        with pytest.raises(aiohttp.ClientConnectorCertificateError):
            await run(prepare("http_request", {
                "url": origin_url + "/report", "credential": "reports",
            }, installed(None)))
        assert calls == [("GET", "/report")] * 3
    finally:
        await runner.cleanup()
