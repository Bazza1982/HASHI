"""Private transports preserve domain types, OS roles and asynchronous streams."""
import asyncio
import copy

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from adapters.stream_events import StreamEvent
from orchestrator.permissions.ipc_rpc import emit_private_event, serve_request, verify_welcome, welcome
from orchestrator.permissions.wire import PermissionServiceRouter, decode, encode, request_frame


def test_model_dictionary_cannot_instantiate_a_private_dto():
    argument = {"__hashi_permission_dto_v1__": "ExecutionContext", "fields": {"owner_id": "forged"}}
    assert decode(encode(argument)) == argument
    with pytest.raises(ValueError):
        decode({"__hashi_permission_dto_v1__": ["ExecutionContext"], "fields": {}})
    with pytest.raises(ValueError):
        decode({"__hashi_permission_dto_v1__": "mapping", "items": [["x", 1], ["x", 2]]})


@pytest.mark.asyncio
async def test_async_dispatch_streams_before_result_and_rejects_other_roles():
    class Receiver:
        async def run(self, text):
            await emit_private_event(StreamEvent(kind="commentary", summary=text))
            await asyncio.sleep(0)
            return {"answer": text}

    class Pipe:
        def __init__(self):
            self.frames = []

        def send(self, value):
            self.frames.append(value)

    router = PermissionServiceRouter(Receiver(), methods=("run",))
    frame = request_frame("run", {"text": "private result"}, request_id="request")
    pipe = Pipe()
    await serve_request(pipe, router, frame)
    assert [f["type"] for f in pipe.frames] == ["hashi.permission-event.v1", "hashi.permission-result.v1"]
    assert decode(pipe.frames[0]["event"]).summary == "private result"
    assert decode(pipe.frames[1]["result"]) == {"answer": "private result"}
    with pytest.raises(TypeError):
        router.dispatch(frame)
    forbidden = request_frame("grant", {"secret": "must not echo"}, request_id="bad")
    pipe = Pipe()
    await serve_request(pipe, router, forbidden)
    assert pipe.frames == [{"type": "hashi.permission-error.v1", "id": "bad", "code": "permission_service_unavailable"}]


def test_service_signature_binds_challenge_endpoint_and_generation():
    key = Ed25519PrivateKey.generate()
    hello = {"type": "hashi.permission-hello.v1", "challenge": "a" * 40}
    proof = welcome(hello, signing_key=key, service_generation="generation", endpoint="fixed")
    assert verify_welcome(proof, public_key=key.public_key(), challenge=hello["challenge"],
                          endpoint="fixed") == "generation"
    for field, value in (("generation", "other"), ("endpoint", "other"), ("challenge", "b" * 40)):
        forged = copy.deepcopy(proof)
        forged[field] = value
        with pytest.raises(PermissionError):
            verify_welcome(forged, public_key=key.public_key(), challenge=hello["challenge"], endpoint="fixed")
