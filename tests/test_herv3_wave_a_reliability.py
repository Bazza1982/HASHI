"""Wave A red/green tests for the HERV3 performance & reliability repairs.

Covers:
  A1  SSE quadratic snapshot removal (DeepSeek + OpenRouter streaming paths)
  A2  Shell timeout partial-output preservation
  A3  DNS-before-send retry budget vs ordinary transient errors
"""

import asyncio
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from adapters.deepseek_api import DeepSeekAdapter
from adapters.openrouter_api import (
    OpenRouterAdapter,
    _is_dns_resolution_before_send,
    _provider_call_transport_retry_limit,
)
from tools.builtins import (
    BuiltinExecutionResult,
    _communicate_partial_tail,
    execute_shell,
)
from tools.registry import ToolResult
from tools.tool_activity import bounded_communicate


# --------------------------------------------------------------------------- #
# A3: DNS-before-send classification and retry budget
# --------------------------------------------------------------------------- #

class _AdapterStub:
    TRANSIENT_PROVIDER_CALL_RETRIES = 3
    TRANSIENT_PROVIDER_CALL_DNS_RETRIES = 6


def _dns_connect_error() -> httpx.ConnectError:
    err = httpx.ConnectError("getaddrinfo failed")
    err.__cause__ = socket.gaierror("Name or service not known")
    return err


def _refused_connect_error() -> httpx.ConnectError:
    err = httpx.ConnectError("Connection refused")
    err.__cause__ = ConnectionRefusedError("Connection refused")
    return err


def test_dns_resolution_before_send_true_for_getaddrinfo_cause():
    assert _is_dns_resolution_before_send(_dns_connect_error()) is True


def test_dns_resolution_before_send_true_for_text_fallback():
    err = httpx.ConnectError("[Errno -2] Name or service not known")
    assert _is_dns_resolution_before_send(err) is True


def test_dns_resolution_before_send_false_for_connection_refused():
    assert _is_dns_resolution_before_send(_refused_connect_error()) is False


def test_dns_resolution_before_send_false_for_tls_and_timeout():
    tls = httpx.ConnectError("SSL certificate verify failed")
    assert _is_dns_resolution_before_send(tls) is False
    assert _is_dns_resolution_before_send(httpx.ReadTimeout("timed out")) is False


def test_transport_retry_limit_dns_wider_than_default():
    assert (
        _provider_call_transport_retry_limit(_AdapterStub(), _dns_connect_error())
        == 6
    )
    assert (
        _provider_call_transport_retry_limit(_AdapterStub(), _refused_connect_error())
        == 3
    )


# --------------------------------------------------------------------------- #
# A2: shell timeout preserves partial output
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_shell_timeout_preserves_partial_output(tmp_path):
    if os.name != "nt":
        pytest.skip("native Windows shell contract")
    command = (
        'for ($i=1; $i -le 30; $i++) { '
        '[Console]::Out.WriteLine("line$i"); [Console]::Out.Flush(); '
        'Start-Sleep -Milliseconds 50 }; '
        'Start-Sleep -Seconds 60'
    )
    result = await execute_shell(
        {"command": command, "shell": "powershell", "timeout": 2.5},
        workspace_dir=tmp_path,
    )
    assert isinstance(result, BuiltinExecutionResult)
    assert "timed out" in result.output
    assert "[partial output]" in result.output
    assert "line1" in result.output
    assert "line5" in result.output


def test_communicate_partial_tail_falls_back_to_partial_state():
    async def _run():
        task = asyncio.create_task(asyncio.sleep(1))
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return _communicate_partial_tail(
            task,
            "utf-8",
            {"stdout_bytes": b"line1\n", "stderr_bytes": b""},
        )

    assert asyncio.run(_run()) == "line1"


@pytest.mark.asyncio
async def test_bounded_communicate_exposes_partial_output_after_cancel():
    class _BlockingStream:
        """Yields a few chunks then blocks forever (simulates a held pipe)."""

        def __init__(self, chunks):
            self._chunks = list(chunks)

        async def read(self, n=-1):
            if self._chunks:
                return self._chunks.pop(0)
            await asyncio.sleep(3600)

    class _Proc:
        stdout = _BlockingStream([b"line1\n", b"line2\n"])
        stderr = _BlockingStream([])

        async def wait(self):
            return 0

    partial: dict = {}
    task = asyncio.create_task(bounded_communicate(_Proc(), partial=partial))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert partial["stdout_bytes"].startswith(b"line1\nline2\n")


# --------------------------------------------------------------------------- #
# A1: SSE quadratic snapshot removed
# --------------------------------------------------------------------------- #

class _DummyToolRegistry:
    max_loops = 2

    def __init__(self):
        self.calls = []

    def get_tool_definitions(self, tiers=None):
        if tiers == []:
            return []
        return [
            {
                "type": "function",
                "function": {
                    "name": "file_list",
                    "description": "List files.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            }
        ]

    async def execute(self, tool_name, arguments, tool_call_id=""):
        self.calls.append((tool_name, arguments, tool_call_id))
        return ToolResult(tool_call_id=tool_call_id, output="tool output")


def _deepseek_adapter(tmp_path):
    cfg = SimpleNamespace(
        name="ying",
        engine="deepseek-api",
        model="deepseek-v4-pro",
        workspace_dir=tmp_path,
        system_md=None,
        extra={},
    )
    adapter = DeepSeekAdapter(cfg, SimpleNamespace(), api_key="test-key")
    adapter.tool_registry = _DummyToolRegistry()
    return adapter


def _openrouter_adapter(tmp_path):
    cfg = SimpleNamespace(
        name="nana",
        model="qwen/qwen3.7-flash",
        workspace_dir=tmp_path,
        system_md=None,
        extra={},
    )
    adapter = OpenRouterAdapter(
        cfg,
        SimpleNamespace(openrouter_url="https://openrouter.invalid/v1/chat"),
        api_key="test-key",
    )
    adapter.tool_registry = _DummyToolRegistry()
    return adapter


def _attach_stream_mock(adapter, streams):
    class _StreamResponse:
        headers = {"x-request-id": "wire-stream"}

        def __init__(self, lines):
            self.lines = lines

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            for line in self.lines:
                yield line

    class _StreamContext:
        def __init__(self, lines):
            self.response = _StreamResponse(lines)

        async def __aenter__(self):
            return self.response

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    adapter.client = SimpleNamespace(
        stream=lambda *args, **kwargs: _StreamContext(next(streams))
    )


_FRAGMENT_STREAMS = [
    [
        'data: {"id":"bad-stream","choices":[{"delta":{"tool_calls":['
        '{"index":0,"id":"call-stream","type":"function","function":'
        '{"name":"file_list","arguments":"{\\"path\\":"}}]},'
        '"finish_reason":null}]}',
        'data: {"id":"bad-stream","choices":[{"delta":{"tool_calls":['
        '{"index":0,"function":{"arguments":"}"}}]},'
        '"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ],
    [
        'data: {"id":"good-stream","choices":[{"delta":{"tool_calls":['
        '{"index":0,"id":"call-stream","type":"function","function":'
        '{"name":"file_list","arguments":"{\\"path\\":\\"/tmp\\"}"}}]},'
        '"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ],
    [
        'data: {"id":"final-stream","choices":[{"delta":{"content":"done"},'
        '"finish_reason":"stop"}]}',
        "data: [DONE]",
    ],
]


@pytest.mark.asyncio
async def test_deepseek_stream_records_no_quadratic_assembly_snapshots(tmp_path):
    adapter = _deepseek_adapter(tmp_path)
    _attach_stream_mock(adapter, iter(_FRAGMENT_STREAMS))

    async def capture(_event):
        return None

    response = await adapter.generate_response(
        "inspect", "req-stream-snapshot", on_stream_event=capture
    )

    assert response.is_success is True
    assert adapter.tool_registry.calls == [
        ("file_list", {"path": "/tmp"}, "call-stream")
    ]
    records = [
        json.loads(line)
        for line in Path(
            response.stream_metadata["provider_protocol_forensic_path"]
        ).read_text(encoding="utf-8").splitlines()
    ]
    raw = records[0]["raw_provider"]
    # The quadratic deep-copy snapshots must be gone: raw fragments are
    # preserved for lossless reconstruction while assembly snapshots stay empty.
    assert raw["assembly_snapshots"] == []
    assert [
        fragment["arguments_fragment"]
        for fragment in raw["tool_call_fragments"]
    ] == ['{"path":', "}"]


@pytest.mark.asyncio
async def test_openrouter_stream_records_no_quadratic_assembly_snapshots(tmp_path):
    adapter = _openrouter_adapter(tmp_path)
    _attach_stream_mock(adapter, iter(_FRAGMENT_STREAMS))

    async def capture(_event):
        return None

    response = await adapter.generate_response(
        "inspect", "req-openrouter-stream-snapshot", on_stream_event=capture
    )

    assert response.is_success is True
    assert adapter.tool_registry.calls == [
        ("file_list", {"path": "/tmp"}, "call-stream")
    ]
    records = [
        json.loads(line)
        for line in Path(
            response.stream_metadata["provider_protocol_forensic_path"]
        ).read_text(encoding="utf-8").splitlines()
    ]
    raw = records[0]["raw_provider"]
    assert raw["assembly_snapshots"] == []
    assert [
        fragment["arguments_fragment"]
        for fragment in raw["tool_call_fragments"]
    ] == ['{"path":', "}"]
