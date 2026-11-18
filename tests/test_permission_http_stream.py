"""Streaming response redaction must be lossless apart from known secrets."""
import pytest

from orchestrator.permissions.network_stream import StreamingRedactor


@pytest.mark.parametrize("page_size", [1, 2, 3, 7, 16, 1024])
def test_split_secrets_do_not_leak_or_lose_unrelated_bytes(page_size):
    patterns = [b"abcab", b"credential", b"unique-cookie"]
    payload = b"start|abcab|middle|credential|unique-cookie|end"
    redactor = StreamingRedactor(patterns)
    pieces = []
    for start in range(0, len(payload), page_size):
        pieces.append(redactor.feed(payload[start:start + page_size]))
        assert redactor.buffered <= max(map(len, patterns)) - 1
    pieces.append(redactor.feed(b"", final=True))
    output = b"".join(pieces)
    assert output == b"start|" + redactor.marker + b"|middle|" + redactor.marker + b"|" + redactor.marker + b"|end"
    assert all(pattern not in output for pattern in patterns)
    assert redactor.redacted is True


@pytest.mark.parametrize("page_size", [1, 4, 16, 100])
def test_partial_http_range_does_not_disclose_secret_suffix_or_prefix(page_size):
    redactor = StreamingRedactor([b"abcdefghij"], partial=True)
    payload = b"fghij visible abcde"
    pieces = [redactor.feed(payload[i:i + page_size]) for i in range(0, len(payload), page_size)]
    pieces.append(redactor.feed(b"", final=True))
    assert b"".join(pieces) == redactor.marker + b" visible " + redactor.marker
    assert redactor.redacted is True


def test_normal_no_secret_stream_is_exact_and_empty_pattern_set_has_no_delay():
    original = bytes(range(256)) * 100
    redactor = StreamingRedactor([])
    assert redactor.feed(original) == original
    assert redactor.buffered == 0
    assert redactor.feed(b"", final=True) == b""
    assert redactor.redacted is False


def test_overlapping_and_repeated_patterns_never_reintroduce_credentials():
    patterns = [b"a[credential redacted]b", b"abcab", b"bc"]
    payload = b"a" + patterns[0] + b"b abcab abcab"
    redactor = StreamingRedactor(patterns)
    output = b"".join(redactor.feed(bytes([value])) for value in payload) + redactor.feed(b"", final=True)
    assert all(pattern not in output for pattern in patterns)
    assert output.startswith(b"a") and output.endswith(b" " + redactor.marker)


def test_redactor_rejects_reuse_and_missing_safe_marker():
    redactor = StreamingRedactor([b"secret"])
    redactor.feed(b"", final=True)
    with pytest.raises(ValueError):
        redactor.feed(b"second")
    with pytest.raises(ValueError):
        StreamingRedactor([bytes([value]) for value in range(256)])


def test_stream_redaction_is_independent_of_chunk_boundaries():
    import random

    randomizer = random.Random(245813)
    alphabet = b"abcde"
    for _ in range(250):
        patterns = [
            bytes(randomizer.choice(alphabet) for _ in range(randomizer.randint(1, 9)))
            for _ in range(randomizer.randint(1, 4))
        ]
        payload = bytes(randomizer.choice(alphabet) for _ in range(randomizer.randint(0, 150)))
        for partial in (False, True):
            whole = StreamingRedactor(patterns, partial=partial)
            expected = whole.feed(payload, final=True)
            paged = StreamingRedactor(patterns, partial=partial)
            chunks = []
            cursor = 0
            while cursor < len(payload):
                size = randomizer.randint(1, 11)
                chunks.append(paged.feed(payload[cursor:cursor + size]))
                cursor += size
            chunks.append(paged.feed(b"", final=True))
            actual = b"".join(chunks)
            assert actual == expected
            assert all(pattern not in actual for pattern in patterns)


@pytest.mark.asyncio
@pytest.mark.parametrize("session_cookies", [False, True])
async def test_http_response_sink_streams_once_with_cookie_redaction(tmp_path, session_cookies):
    import json
    from aiohttp import web
    from orchestrator.permissions.descriptors import describe_tool
    from orchestrator.permissions.network_worker import build_options, prepare, run

    secret = "synthetic-cookie-67890"
    payload = b"A" * (16384 - 4) + secret.encode() + b"B" * (2 * 1024 * 1024)
    seen = []

    async def endpoint(request):
        seen.append((request.method, await request.read()))
        response = web.Response(body=payload)
        response.set_cookie("session", secret)
        return response

    class Sink:
        def __init__(self):
            self.content = bytearray()
            self.committed = False

        def write(self, data):
            self.content.extend(data)

        def commit(self):
            self.committed = True

    app = web.Application()
    app.router.add_post("/perform", endpoint)
    server = web.AppRunner(app)
    await server.setup()
    site = web.TCPSite(server, "127.0.0.1", 0)
    await site.start()
    try:
        base = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        arguments = {
            "url": base + "/perform", "method": "POST", "body": "perform-once",
            "session_cookies": session_cookies, "response_encoding": "base64",
            "response_file": str(tmp_path / "response.bin"),
        }
        descriptor = describe_tool("http_request", arguments, cwd=tmp_path, read_roots=(tmp_path,))
        file_scope = next(item for item in descriptor.scopes if item.kind == "file")
        assert set(file_scope.actions) == {"create", "write"}
        plan = prepare("http_request", arguments, build_options({"private_origins": [base]}, {}))
        sink = Sink()
        result = json.loads(await run(plan, response_sink=sink))
        assert seen == [("POST", b"perform-once")]
        assert sink.committed
        assert len(sink.content) > 2 * 1024 * 1024
        assert sink.content.startswith(b"A" * (16384 - 4))
        assert sink.content.endswith(b"B" * (2 * 1024 * 1024))
        assert secret.encode() not in sink.content
        assert secret not in json.dumps(result)
        assert result["body_redacted"] is True
        assert result["body_saved"] is True
        assert result["truncated"] is False
    finally:
        await server.cleanup()


@pytest.mark.asyncio
async def test_http_response_sink_must_be_bound_and_projection_is_rejected_before_dispatch():
    from orchestrator.permissions.network_worker import RequestNotSent, run

    minimal = {
        "body_source": None, "response_sink": "bound", "body": None,
        "response_fields": None, "url": "http://127.0.0.1/",
    }
    with pytest.raises(RequestNotSent):
        await run(minimal)
    with pytest.raises(RequestNotSent):
        await run({**minimal, "response_fields": ["selected"]}, response_sink=object())


@pytest.mark.skipif(__import__("os").name != "nt", reason="held file bindings require native Windows")
def test_native_streamed_create_is_atomic_and_aborts_safely(tmp_path):
    from hashlib import sha256
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.windows_file_binding import FileBindingError, open_bound_create

    target = tmp_path / "download.bin"
    scope = normalize_scope("file", str(target), ("create", "write"), cwd=tmp_path)
    chunk = bytes(range(256)) * 4096
    with open_bound_create(scope) as bound:
        with bound.begin_stream() as sink:
            sink.write(chunk)
            sink.write(chunk)
            assert not target.exists()
            sink.commit()
    assert target.stat().st_size == 2 * len(chunk)
    assert sha256(target.read_bytes()).digest() == sha256(chunk * 2).digest()

    with open_bound_create(scope) as bound:
        with pytest.raises(FileBindingError):
            with bound.begin_stream() as sink:
                sink.write(b"replacement")
                sink.commit()
    assert target.read_bytes() == chunk * 2

    aborted = tmp_path / "aborted.bin"
    abort_scope = normalize_scope("file", str(aborted), ("create", "write"), cwd=tmp_path)
    with open_bound_create(abort_scope) as bound:
        with pytest.raises(RuntimeError):
            with bound.begin_stream() as sink:
                sink.write(b"partial")
                raise RuntimeError("interrupted")
    assert not aborted.exists()
    assert list(tmp_path.glob(".hashi-permission-*.tmp")) == []


@pytest.mark.skipif(__import__("os").name != "nt", reason="held file bindings require native Windows")
def test_native_fixed_helper_streams_input_and_response_to_held_files(tmp_path):
    import base64
    import json
    import threading
    from hashlib import sha256
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from orchestrator.permissions.file_worker import run as run_helper
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.network_worker import build_options, prepare
    from orchestrator.permissions.windows_file_binding import open_bound_create, open_bound_read
    from orchestrator.permissions.windows_process import inheritable_handle_copy

    source = tmp_path / "input.bin"
    target = tmp_path / "output.bin"
    content = bytes(range(256)) * 4200
    returned = bytes(reversed(range(256))) * 4500
    source.write_bytes(content)
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            received.append(sha256(self.rfile.read(length)).hexdigest())
            self.send_response(200)
            self.send_header("Content-Length", str(len(returned)))
            self.end_headers()
            self.wfile.write(returned)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = "http://127.0.0.1:" + str(server.server_port)
    try:
        arguments = {
            "url": origin + "/do", "method": "POST", "body_file": str(source),
            "response_file": str(target), "response_encoding": "base64",
        }
        network = prepare("http_request", arguments, build_options({"private_origins": [origin]}, {}))
        source_scope = normalize_scope("file", str(source), ("read",), cwd=tmp_path)
        target_scope = normalize_scope("file", str(target), ("create", "write"), cwd=tmp_path)
        with open_bound_read(source_scope, exclusive=True) as reader, open_bound_create(target_scope) as writer:
            _kernel, read_handle = inheritable_handle_copy(reader._handles[-1])
            _kernel, parent_handle = inheritable_handle_copy(writer._handles[-1])
            plan = {
                "format": "hashi-file-helper-v1", "action": "http", "handles": [read_handle, parent_handle],
                "directory": writer._directory, "name": writer._name,
                "data": base64.b64encode(json.dumps(network).encode()).decode(),
                "result": str(tmp_path / "unused.json"),
            }
            response = run_helper(plan)
        result = json.loads(base64.b64decode(response["data"]))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert response["outcome"] == "succeeded"
    assert result["body_saved"] is True and result["truncated"] is False
    assert received == [sha256(content).hexdigest()]
    assert sha256(target.read_bytes()).hexdigest() == sha256(returned).hexdigest()
    assert source.read_bytes() == content


def test_supervisor_binds_http_files_in_source_then_target_order(tmp_path):
    from orchestrator.permissions.descriptors import describe_tool
    from orchestrator.permissions.file_process_supervisor import prepare_managed_http_file_scopes

    source = tmp_path / "z-source.bin"
    target = tmp_path / "a-target.bin"
    args = {
        "url": "https://api.example.com/path", "method": "POST",
        "body_file": str(source), "response_file": str(target),
    }
    descriptor = describe_tool("http_request", args, cwd=tmp_path, read_roots=(tmp_path,))
    scopes = prepare_managed_http_file_scopes(descriptor, args, cwd=tmp_path)
    assert len(scopes) == 2
    assert scopes[0].resource.casefold() == str(source).casefold()
    assert scopes[0].actions == ("read",)
    assert scopes[1].resource.casefold() == str(target).casefold()
    assert set(scopes[1].actions) == {"create", "write"}
    with pytest.raises(ValueError):
        prepare_managed_http_file_scopes(descriptor, {**args, "response_file": str(source)}, cwd=tmp_path)
    with pytest.raises(ValueError):
        prepare_managed_http_file_scopes(descriptor, {**args, "body_file": str(target)}, cwd=tmp_path)


@pytest.mark.skipif(__import__("os").name != "nt", reason="held file bindings require native Windows")
def test_native_incomplete_response_does_not_publish_or_retry(tmp_path):
    import base64
    import json
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from orchestrator.permissions.file_worker import FilePlanRejected, run as run_helper
    from orchestrator.permissions.models import normalize_scope
    from orchestrator.permissions.network_worker import build_options, prepare
    from orchestrator.permissions.windows_file_binding import open_bound_create
    from orchestrator.permissions.windows_process import inheritable_handle_copy

    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", str(400000))
            self.end_headers()
            self.wfile.write(b"partial-response" * 4000)
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = "http://127.0.0.1:" + str(server.server_port)
    target = tmp_path / "result.bin"

    def attempt():
        args = {"url": origin + "/once", "method": "POST", "response_file": str(target)}
        network = prepare("http_request", args, build_options({"private_origins": [origin]}, {}))
        scope = normalize_scope("file", str(target), ("create", "write"), cwd=tmp_path)
        with open_bound_create(scope) as writer:
            _kernel, duplicate = inheritable_handle_copy(writer._handles[-1])
            return run_helper({
                "format": "hashi-file-helper-v1", "action": "http", "handles": [duplicate],
                "directory": writer._directory, "name": writer._name,
                "data": base64.b64encode(json.dumps(network).encode()).decode(),
                "result": str(tmp_path / "unused.json"),
            })

    try:
        with pytest.raises(Exception):
            attempt()
        assert seen == ["/once"]
        assert not target.exists()
        assert list(tmp_path.glob(".hashi-permission-*.tmp")) == []
        target.write_bytes(b"keep original")
        with pytest.raises(FilePlanRejected):
            attempt()
        assert seen == ["/once"]
        assert target.read_bytes() == b"keep original"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_percent_encoded_secrets_are_redacted_case_insensitively_across_chunks():
    from urllib.parse import quote
    from orchestrator.permissions.network_worker import _redact_value, _secret_patterns

    secret = "key /é?"
    encoded = quote(secret, safe="")
    mixed = encoded.replace("%2F", "%2f").replace("%C3", "%c3").replace("%3F", "%3f")
    assert mixed != encoded and mixed.lower() == encoded.lower()
    patterns = _secret_patterns([secret])
    visible = "prefix|" + mixed + "|suffix"
    assert mixed not in _redact_value(visible, patterns)

    redactor = StreamingRedactor([item.encode("utf-8") for item in patterns])
    data = visible.encode("utf-8")
    output = b"".join(redactor.feed(data[index:index + 3]) for index in range(0, len(data), 3))
    output += redactor.feed(b"", final=True)
    assert mixed.encode("utf-8") not in output
    assert output.startswith(b"prefix|") and output.endswith(b"|suffix")


@pytest.mark.asyncio
async def test_repeated_set_cookie_values_are_all_redacted():
    import json
    from aiohttp import web
    from orchestrator.permissions.network_worker import build_options, prepare, run

    values = ("synthetic-previous-cookie", "synthetic-new-cookie")

    async def endpoint(request):
        response = web.Response(text="tokens: " + " ".join(values))
        for value in values:
            response.headers.add("Set-Cookie", "session=" + value + "; Path=/")
        return response

    app = web.Application()
    app.router.add_get("/", endpoint)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        base = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        plan = prepare("http_request", {"url": base + "/"}, build_options({"private_origins": [base]}, {}))
        result = json.loads(await run(plan))
        assert all(value not in result["body"] for value in values)
        assert result["body_redacted"] is True
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_streaming_redaction_expansion_uses_bounded_file_writes(tmp_path):
    import json
    from aiohttp import web
    from orchestrator.permissions.network_worker import build_options, prepare, run

    class Sink:
        def __init__(self):
            self.lengths = []
            self.committed = False

        def write(self, data):
            assert len(data) <= 1024 * 1024
            self.lengths.append(len(data))

        def commit(self):
            self.committed = True

    async def endpoint(request):
        return web.Response(body=b"x" * 63000)

    app = web.Application()
    app.router.add_get("/", endpoint)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        base = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        plan = prepare("http_request", {
            "url": base + "/", "response_file": str(tmp_path / "result.txt"),
        }, build_options({"private_origins": [base]}, {}))
        plan["redact"] = ["x", "密" * 7000]
        sink = Sink()
        result = json.loads(await run(plan, response_sink=sink))
        assert sink.committed and result["body_redacted"] is True
        assert sum(sink.lengths) == result["saved_bytes"] > 1024 * 1024
        assert max(sink.lengths) <= 1024 * 1024
    finally:
        await runner.cleanup()
