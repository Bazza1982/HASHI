from __future__ import annotations

import asyncio
import multiprocessing

import pytest

from orchestrator.function_worker_protocol import (
    FUNCTION_WORKER_PROTOCOL_VERSION,
    MAX_FRAME_BYTES,
    FunctionWorkerProtocolError,
    FunctionWorkerRemoteError,
    JsonConnectionPeer,
    decode_envelope,
    encode_envelope,
    json_value,
)


def _request(**overrides):
    value = {
        "version": FUNCTION_WORKER_PROTOCOL_VERSION,
        "kind": "request",
        "id": "req-1",
        "method": "echo",
        "params": {"value": 42},
    }
    value.update(overrides)
    return value


def test_protocol_round_trip_is_strict_json_not_pickle():
    encoded = encode_envelope(_request())

    assert encoded.startswith(b"{")
    assert decode_envelope(encoded) == _request()
    assert json_value((1, 2)) == [1, 2]


@pytest.mark.parametrize(
    "envelope",
    [
        _request(version="1"),
        _request(version=True),
        _request(params=[]),
        _request(extra="not-negotiated"),
        {
            "version": FUNCTION_WORKER_PROTOCOL_VERSION,
            "kind": "response",
            "id": "req-1",
            "ok": True,
        },
        {
            "version": FUNCTION_WORKER_PROTOCOL_VERSION,
            "kind": "response",
            "id": "req-1",
            "ok": False,
            "result": None,
            "error": {"type": "Failure", "message": "bad"},
        },
    ],
)
def test_protocol_rejects_ambiguous_or_unnegotiated_frames(envelope):
    with pytest.raises(FunctionWorkerProtocolError):
        encode_envelope(envelope)


def test_protocol_rejects_invalid_utf8_and_oversize_frames():
    with pytest.raises(FunctionWorkerProtocolError, match="UTF-8 JSON"):
        decode_envelope(b"\xff")
    with pytest.raises(FunctionWorkerProtocolError, match="exceeds"):
        decode_envelope(b"x" * (MAX_FRAME_BYTES + 1))


@pytest.mark.asyncio
async def test_bidirectional_peer_returns_results_and_typed_remote_errors():
    left_connection, right_connection = multiprocessing.Pipe(duplex=True)

    async def handle(method, params):
        if method == "fail":
            raise ValueError("rejected by worker")
        return {"method": method, "value": params["value"]}

    left = JsonConnectionPeer(left_connection, label="core")
    right = JsonConnectionPeer(
        right_connection,
        label="worker",
        request_handler=handle,
    )
    left.start()
    right.start()
    try:
        assert await left.request("echo", {"value": 7}) == {
            "method": "echo",
            "value": 7,
        }
        with pytest.raises(FunctionWorkerRemoteError, match="ValueError"):
            await left.request("fail", {"value": 0})
    finally:
        await asyncio.gather(left.close(), right.close())
