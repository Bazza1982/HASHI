"""Range and byte-preserving results for the managed network worker."""

import base64
import hashlib
import json
import socket

import pytest

from orchestrator.permissions.network_worker import RequestNotSent, build_options, prepare, run


@pytest.mark.asyncio
async def test_authorized_ranged_binary_response_can_be_reconstructed_without_loss():
    from aiohttp import web

    content = bytes(range(256)) * 3073
    validator = '"fixture-version-1"'
    seen = []

    async def data(request):
        range_header = request.headers.get("Range")
        if_range = request.headers.get("If-Range")
        seen.append((range_header, if_range))
        if not range_header or if_range != validator:
            return web.Response(body=content, headers={"ETag": validator})
        start_text, end_text = range_header.removeprefix("bytes=").split("-")
        start, end = int(start_text), min(int(end_text), len(content) - 1)
        return web.Response(
            status=206, body=content[start:end + 1],
            headers={
                "ETag": validator, "Accept-Ranges": "bytes",
                "Content-Range": f"bytes {start}-{end}/{len(content)}",
            },
        )

    app = web.Application()
    app.router.add_get("/data", data)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        target = f"http://127.0.0.1:{port}"
        policy = build_options({"private_origins": [target]}, {})
        chunks = []
        chunk_size = 256 * 1024
        for start in range(0, len(content), chunk_size):
            end = min(start + chunk_size, len(content)) - 1
            arguments = {
                "url": target + "/data",
                "response_encoding": "base64",
                "headers": {"Range": f"bytes={start}-{end}", "If-Range": validator},
            }
            result = json.loads(await run(prepare("http_request", arguments, policy)))
            headers = {name.lower(): value for name, value in result["headers"].items()}
            assert result["status"] == 206
            assert result["body_encoding"] == "base64"
            assert result["truncated"] is False and result["body_redacted"] is False
            assert headers["content-range"] == f"bytes {start}-{end}/{len(content)}"
            assert headers["etag"] == validator and headers["accept-ranges"] == "bytes"
            part = base64.b64decode(result["body"], validate=True)
            assert result["captured_bytes"] == len(part) == end - start + 1
            chunks.append(part)
        assert hashlib.sha256(b"".join(chunks)).digest() == hashlib.sha256(content).digest()
        assert len(seen) == len(chunks)

        changed = json.loads(await run(prepare("http_request", {
            "url": target + "/data", "response_encoding": "base64",
            "headers": {"Range": "bytes=20-99", "If-Range": '"stale-version"'},
        }, policy)))
        assert changed["status"] == 200
        assert changed["truncated"] is True
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_ranged_binary_result_redacts_credential_at_both_edges(monkeypatch):
    import asyncio
    import aiohttp

    secret = "sensitive-credential"
    raw = secret.encode()
    body = raw[-7:] + b"\xff" + raw
    body += b"x" * (512 * 1024 - len(body) - 8) + raw[:8] + b"more"

    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)

    class Content:
        async def iter_chunked(self, size):
            yield body

    class Response:
        status = 206
        headers = {"Content-Range": f"bytes 5-{5 + len(body) - 1}/900000"}
        content = Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Session:
        def __init__(self, **kwargs):
            self.connector = kwargs["connector"]
            assert kwargs["auto_decompress"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.connector.close()

        def request(self, method, url, **kwargs):
            assert kwargs["headers"]["Accept-Encoding"] == "identity"
            return Response()

    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    policy = build_options({"credentials": {"reports": {
        "origin": "https://api.example.com", "methods": ["GET"], "secret_ref": "key",
        "header": "Authorization", "prefix": "Bearer ",
    }}}, {"key": secret})
    result = json.loads(await run(prepare("http_request", {
        "url": "https://api.example.com/data", "credential": "reports",
        "response_encoding": "base64", "headers": {"Range": "bytes=5-900000"},
    }, policy)))
    decoded = base64.b64decode(result["body"], validate=True)
    assert result["truncated"] is True and result["body_redacted"] is True
    assert result["captured_bytes"] == 512 * 1024
    assert decoded.startswith(b"[credential redacted]\xff[credential redacted]")
    assert decoded.endswith(b"[credential redacted]")
    unmarked = decoded.replace(b"[credential redacted]", b"")
    assert raw not in unmarked and raw[-7:] not in unmarked and raw[:8] not in unmarked

    # A complete, below-limit 206 is still only part of the original object.
    body = raw[-7:] + b"\xff" + raw[:8]
    Response.headers = {"Content-Range": f"bytes 5-{5 + len(body) - 1}/900000"}
    complete_range = json.loads(await run(prepare("http_request", {
        "url": "https://api.example.com/data", "credential": "reports",
        "response_encoding": "base64", "headers": {"Range": "bytes=5-19"},
    }, policy)))
    assert complete_range["truncated"] is False and complete_range["body_redacted"] is True
    assert base64.b64decode(complete_range["body"]) == (
        b"[credential redacted]\xff[credential redacted]"
    )

    import gzip
    body = gzip.compress(raw)
    Response.status = 200
    Response.headers = {"Content-Encoding": "gzip"}
    compressed = json.loads(await run(prepare("http_request", {
        "url": "https://api.example.com/data", "credential": "reports",
        "response_encoding": "base64",
    }, policy)))
    assert compressed["body"] == "" and compressed["truncated"] is True
    assert compressed["content_unavailable_reason"] == "encoded_sensitive_response"

    body = json.dumps({"allowed": 7, "private": secret}).encode()
    Response.headers = {"Content-Type": "application/json"}
    selected_policy = build_options({"credentials": {"reports": {
        "origin": "https://api.example.com", "methods": ["GET"], "secret_ref": "key",
        "header": "Authorization", "prefix": "Bearer ", "response_fields": ["allowed"],
    }}}, {"key": secret})
    selected = json.loads(await run(prepare("http_request", {
        "url": "https://api.example.com/data", "credential": "reports",
        "response_encoding": "base64",
    }, selected_policy)))
    assert selected["body_redacted"] is True
    assert json.loads(base64.b64decode(selected["body"])) == {"allowed": 7}
    assert raw not in base64.b64decode(selected["body"])


def test_binary_chunks_pass_through_fixed_helper_and_result_renderer(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from orchestrator.permissions.descriptors import describe_tool
    from orchestrator.permissions.file_results import render_helper_result
    from orchestrator.permissions.file_worker import run as run_helper

    content = bytes(range(256)) * 3073
    tag = '"helper-fixture"'

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.headers["If-Range"] == tag
            first, last = self.headers["Range"].removeprefix("bytes=").split("-")
            start, end = int(first), min(int(last), len(content) - 1)
            piece = content[start:end + 1]
            self.send_response(206)
            self.send_header("ETag", tag)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(content)}")
            self.send_header("Content-Length", str(len(piece)))
            self.end_headers()
            self.wfile.write(piece)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    target = f"http://127.0.0.1:{server.server_port}"
    policy = build_options({"private_origins": [target]}, {})
    result_data = bytearray()
    try:
        for start in range(0, len(content), 256 * 1024):
            end = min(start + 256 * 1024, len(content)) - 1
            args = {"url": target + "/bytes", "response_encoding": "base64",
                    "headers": {"Range": f"bytes={start}-{end}", "If-Range": tag}}
            descriptor = describe_tool("http_request", args, cwd=tmp_path, read_roots=(tmp_path,))
            prepared = prepare("http_request", args, policy)
            plan = {"format": "hashi-file-helper-v1", "action": "http", "handles": [],
                    "directory": "", "name": "", "result": str(tmp_path / "unused"),
                    "data": base64.b64encode(json.dumps(prepared).encode()).decode()}
            receipt = run_helper(plan)
            result = json.loads(render_helper_result(descriptor, args, receipt))
            assert result["status"] == 206 and result["truncated"] is False
            assert result["body_redacted"] is False
            result_data.extend(base64.b64decode(result["body"], validate=True))
        assert hashlib.sha256(result_data).digest() == hashlib.sha256(content).digest()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_actual_http_supports_selected_long_idle_timeout_and_many_safe_headers():
    from aiohttp import web

    expected = {f"X-Fixture-{index}": f"value-{index}" for index in range(48)}

    async def inspect(request):
        return web.json_response({name: request.headers.get(name) for name in expected})

    app = web.Application()
    app.router.add_get("/inspect", inspect)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        target = f"http://127.0.0.1:{port}"
        policy = build_options({"private_origins": [target]}, {})
        plan = prepare("http_request", {
            "url": target + "/inspect", "headers": expected, "timeout": 600,
        }, policy)
        assert plan["timeout"] == 600
        result = json.loads(await run(plan))
        assert result["status"] == 200 and json.loads(result["body"]) == expected
        for timeout in (0, -1, True, float("inf"), float("nan")):
            with pytest.raises(RequestNotSent):
                prepare("http_request", {"url": target, "timeout": timeout}, policy)
        with pytest.raises(RequestNotSent):
            prepare("http_request", {"url": target, "headers": {**expected, "Host": "other"}}, policy)
    finally:
        await runner.cleanup()


def test_invalid_response_encoding_is_rejected():
    with pytest.raises(RequestNotSent):
        prepare("http_request", {"url": "https://example.com", "response_encoding": "binary"},
                build_options({}, {}))
