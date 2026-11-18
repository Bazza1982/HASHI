from __future__ import annotations

import asyncio
import hashlib
import multiprocessing
from types import SimpleNamespace

import pytest

from orchestrator.function_worker_host import FunctionWorkerHost
from orchestrator.function_worker_protocol import MAX_FRAME_BYTES, JsonConnectionPeer
from orchestrator.function_worker_supervisor import AgentRuntimeHandle


class _CountingConnection:
    def __init__(self, inner):
        self.inner = inner
        self.sent_sizes = []

    def send_bytes(self, value):
        self.sent_sizes.append(len(value))
        return self.inner.send_bytes(value)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class _Client:
    agent_name = "test-agent"

    def __init__(self, peer):
        self.peer = peer

    async def call(self, method, params=None, *, timeout=180):
        return await self.peer.request(method, params, timeout=timeout)


@pytest.mark.asyncio
async def test_large_enqueue_preserves_complete_request_and_bounded_frames():
    left_connection, right_connection = multiprocessing.Pipe(duplex=True)
    counted = _CountingConnection(left_connection)
    received = []

    async def enqueue(chat_id, prompt, source, summary, **kwargs):
        received.append((chat_id, prompt, source, summary, kwargs))
        return "request-complete"

    async def no_transcription(*args, **kwargs):
        return None

    async def emit_metadata():
        return None

    host = FunctionWorkerHost.__new__(FunctionWorkerHost)
    host.agent_name = "test-agent"
    host.phase = "ACTIVE"
    host.accepting = True
    host.runtime = SimpleNamespace(enqueue_request=enqueue)
    host._begin_native_voice_transcription = no_transcription
    host.emit_metadata = emit_metadata

    left = JsonConnectionPeer(counted, label="test-core")
    right = JsonConnectionPeer(right_connection, label="test-worker",
                               request_handler=host.handle_request)
    left.start()
    right.start()
    handle = AgentRuntimeHandle(SimpleNamespace(), _Client(left), {})
    prompt = "花" + ("x" * (MAX_FRAME_BYTES + 1024))
    try:
        result = await handle.enqueue_request(
            17, prompt, "session-api", "large request",
            request_metadata={"owner_id": "owner-a", "session_id": "session-a"},
            request_content={"kind": "text", "text": "内容"},
            idempotency_key="same-invocation",
        )
        assert result == "request-complete"
        assert len(received) == 1
        assert received[0][0] == 17
        assert hashlib.sha256(received[0][1].encode()).digest() == hashlib.sha256(prompt.encode()).digest()
        assert received[0][4]["request_metadata"] == {"owner_id": "owner-a", "session_id": "session-a"}
        assert received[0][4]["request_content"] == {"kind": "text", "text": "内容"}
        assert received[0][4]["idempotency_key"] == "same-invocation"
        assert counted.sent_sizes and max(counted.sent_sizes) <= MAX_FRAME_BYTES
    finally:
        await asyncio.gather(left.close(), right.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b"\\xff", b"{", b'{"k":NaN}', b'{"k":1,"k":2}'])
async def test_large_request_rejects_invalid_payload_before_dispatch(raw):
    from orchestrator.function_worker_large_request import (
        APPEND, BEGIN, COMMIT, LargeRequestReceiver,
    )
    from orchestrator.function_worker_protocol import FunctionWorkerProtocolError
    import base64

    receiver = LargeRequestReceiver(max_bytes=1000)
    transfer_id = "a" * 32
    dispatched = []

    async def dispatch(method, params):
        dispatched.append((method, params))

    await receiver.handle(BEGIN, {
        "transfer_id": transfer_id, "method": "runtime.enqueue_request",
        "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
    }, dispatch)
    await receiver.handle(APPEND, {
        "transfer_id": transfer_id, "offset": 0,
        "data": base64.b64encode(raw).decode(),
    }, dispatch)
    with pytest.raises(FunctionWorkerProtocolError, match="valid UTF-8 JSON"):
        await receiver.handle(COMMIT, {"transfer_id": transfer_id}, dispatch)
    assert not dispatched and not receiver._uploads
    receiver.close()


@pytest.mark.asyncio
async def test_large_request_rejects_digest_and_foreign_peer_reference():
    from orchestrator.function_worker_large_request import (
        APPEND, BEGIN, COMMIT, LargeRequestReceiver,
    )
    from orchestrator.function_worker_protocol import FunctionWorkerProtocolError
    import base64

    first = LargeRequestReceiver(max_bytes=1000)
    other = LargeRequestReceiver(max_bytes=1000)
    transfer_id = "b" * 32
    raw = b'{"owner_id":"source-owner","session_id":"session-a"}'
    dispatched = []

    async def dispatch(method, params):
        dispatched.append((method, params))

    begin = {
        "transfer_id": transfer_id, "method": "runtime.enqueue_request",
        "size": len(raw), "sha256": "0" * 64,
    }
    chunk = {"transfer_id": transfer_id, "offset": 0,
             "data": base64.b64encode(raw).decode()}
    await first.handle(BEGIN, begin, dispatch)
    with pytest.raises(FunctionWorkerProtocolError, match="unavailable"):
        await other.handle(APPEND, chunk, dispatch)
    with pytest.raises(FunctionWorkerProtocolError, match="unavailable"):
        await other.handle(COMMIT, {"transfer_id": transfer_id}, dispatch)
    await first.handle(APPEND, chunk, dispatch)
    with pytest.raises(FunctionWorkerProtocolError, match="digest mismatch"):
        await first.handle(COMMIT, {"transfer_id": transfer_id}, dispatch)
    assert not dispatched and not first._uploads and not other._uploads
    first.close()
    other.close()


@pytest.mark.asyncio
async def test_large_request_rejects_bad_chunks_and_unsupported_method():
    from orchestrator.function_worker_large_request import (
        APPEND, BEGIN, CHUNK_BYTES, LargeRequestReceiver,
    )
    from orchestrator.function_worker_protocol import FunctionWorkerProtocolError
    import base64

    receiver = LargeRequestReceiver(max_bytes=2 * CHUNK_BYTES)
    transfer_id = "c" * 32

    async def dispatch(method, params):
        pytest.fail("invalid data reached dispatch")

    common = {"transfer_id": transfer_id, "size": CHUNK_BYTES + 1,
              "sha256": "0" * 64}
    with pytest.raises(FunctionWorkerProtocolError, match="unsupported"):
        await receiver.handle(BEGIN, {**common, "method": "worker.shutdown"}, dispatch)
    await receiver.handle(BEGIN, {**common, "method": "runtime.enqueue_api_text"}, dispatch)
    stream = receiver._uploads[transfer_id].stream
    with pytest.raises(FunctionWorkerProtocolError, match="out of order"):
        await receiver.handle(APPEND, {
            "transfer_id": transfer_id, "offset": 1, "data": "eA==",
        }, dispatch)
    assert stream.closed and not receiver._uploads
    await receiver.handle(BEGIN, {**common, "method": "runtime.enqueue_api_text"}, dispatch)
    with pytest.raises(FunctionWorkerProtocolError, match="too large|length is invalid"):
        await receiver.handle(APPEND, {
            "transfer_id": transfer_id, "offset": 0,
            "data": base64.b64encode(b"x" * (CHUNK_BYTES + 1)).decode(),
        }, dispatch)
    assert not receiver._uploads
    receiver.close()


@pytest.mark.asyncio
async def test_large_request_commit_consumes_reference_and_expiry_closes_file():
    from orchestrator.function_worker_large_request import (
        APPEND, BEGIN, COMMIT, LargeRequestReceiver,
    )
    from orchestrator.function_worker_protocol import FunctionWorkerProtocolError
    import base64

    receiver = LargeRequestReceiver(max_bytes=1000, idle_seconds=0.05)
    transfer_id = "d" * 32
    raw = b'{"owner_id":"owner-a","text":"whole"}'
    calls = []

    async def dispatch(method, params):
        calls.append((method, params))
        return "accepted"

    begin = {"transfer_id": transfer_id, "method": "runtime.enqueue_api_text",
             "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    await receiver.handle(BEGIN, begin, dispatch)
    stream = receiver._uploads[transfer_id].stream
    await receiver.handle(APPEND, {
        "transfer_id": transfer_id, "offset": 0,
        "data": base64.b64encode(raw).decode(),
    }, dispatch)
    assert await receiver.handle(COMMIT, {"transfer_id": transfer_id}, dispatch) == "accepted"
    assert stream.closed
    with pytest.raises(FunctionWorkerProtocolError, match="unavailable"):
        await receiver.handle(COMMIT, {"transfer_id": transfer_id}, dispatch)
    assert calls == [("runtime.enqueue_api_text", {"owner_id": "owner-a", "text": "whole"})]
    await receiver.handle(BEGIN, {**begin, "transfer_id": "e" * 32}, dispatch)
    expired_stream = receiver._uploads["e" * 32].stream
    await asyncio.sleep(0.1)
    assert expired_stream.closed and not receiver._uploads
    receiver.close()


@pytest.mark.asyncio
async def test_large_request_cancellation_cleans_up_and_cutover_waits():
    from orchestrator.function_worker_large_request import APPEND

    left_connection, right_connection = multiprocessing.Pipe(duplex=True)
    host = FunctionWorkerHost.__new__(FunctionWorkerHost)
    host.agent_name = "test-agent"
    host.phase = "ACTIVE"
    host.accepting = True
    admitted = []

    async def enqueue(*args, **kwargs):
        admitted.append((args, kwargs))
        return "should-not-admit"

    host.runtime = SimpleNamespace(enqueue_request=enqueue)
    left = JsonConnectionPeer(left_connection, label="cancel-core")
    right = JsonConnectionPeer(right_connection, label="cancel-worker",
                               request_handler=host.handle_request)
    left.start()
    right.start()
    chunk_sent = asyncio.Event()
    never = asyncio.Event()

    class BlockingClient(_Client):
        async def call(self, method, params=None, *, timeout=180):
            result = await super().call(method, params, timeout=timeout)
            if method == APPEND:
                chunk_sent.set()
                await never.wait()
            return result

    client = BlockingClient(left)
    handle = AgentRuntimeHandle(SimpleNamespace(), client, {})
    task = asyncio.create_task(handle.enqueue_request(
        17, "x" * (MAX_FRAME_BYTES + 1024), "session-api", "cancel me",
        request_metadata={"owner_id": "owner-a"},
    ))
    cutover = None
    try:
        await asyncio.wait_for(chunk_sent.wait(), 30)
        receiver = host._large_request_store()
        assert len(receiver._uploads) == 1
        stream = next(iter(receiver._uploads.values())).stream
        cutover = asyncio.create_task(handle.begin_cutover())
        await asyncio.sleep(0)
        assert not cutover.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.wait_for(cutover, 5) is client
        assert stream.closed and not receiver._uploads and not admitted
        await handle.abort_cutover()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if cutover is not None and not cutover.done():
            cutover.cancel()
            await asyncio.gather(cutover, return_exceptions=True)
        if hasattr(host, "_large_request_uploads"):
            host._large_request_uploads.close()
        await asyncio.gather(left.close(), right.close())


@pytest.mark.asyncio
async def test_large_request_preserves_remote_failure_method_without_retry():
    from orchestrator.function_worker_protocol import FunctionWorkerRemoteError

    left_connection, right_connection = multiprocessing.Pipe(duplex=True)
    host = FunctionWorkerHost.__new__(FunctionWorkerHost)
    host.agent_name = "test-agent"
    host.phase = "ACTIVE"
    host.accepting = True
    calls = []

    async def enqueue(text, **kwargs):
        calls.append(len(text))
        raise ValueError("rejected once")

    host.runtime = SimpleNamespace(enqueue_api_text=enqueue)
    left = JsonConnectionPeer(left_connection, label="error-core")
    right = JsonConnectionPeer(right_connection, label="error-worker",
                               request_handler=host.handle_request)
    left.start()
    right.start()
    handle = AgentRuntimeHandle(SimpleNamespace(), _Client(left), {})
    try:
        with pytest.raises(FunctionWorkerRemoteError) as caught:
            await handle.enqueue_api_text("x" * (MAX_FRAME_BYTES + 1024))
        assert caught.value.method == "runtime.enqueue_api_text"
        assert caught.value.error["type"] == "ValueError"
        assert calls == [MAX_FRAME_BYTES + 1024]
        assert not host._large_request_store()._uploads
    finally:
        host._large_request_store().close()
        await asyncio.gather(left.close(), right.close())


@pytest.mark.asyncio
async def test_large_capability_result_arrives_once_via_bounded_frames():
    from orchestrator.function_worker_host import WorkerKernelFacade
    from orchestrator.function_worker_supervisor import FunctionWorkerClient

    core_connection, worker_connection = multiprocessing.Pipe(duplex=True)
    counted = _CountingConnection(core_connection)
    invoked = []
    expected = "结果" + "r" * (MAX_FRAME_BYTES + 1024)

    class Supervisor:
        async def handle_worker_request(self, client, method, params):
            invoked.append((client.agent_name, method, params["task_id"]))
            assert method == "core.capability.invoke"
            return {"text": expected, "nested": {"ok": True}}

    client = FunctionWorkerClient.__new__(FunctionWorkerClient)
    client.agent_name = "agent-a"
    client.supervisor = Supervisor()
    core = JsonConnectionPeer(counted, label="capability-core",
                              request_handler=client._handle_request)
    worker = JsonConnectionPeer(worker_connection, label="capability-worker")
    core.start()
    worker.start()
    facade = WorkerKernelFacade.__new__(WorkerKernelFacade)
    facade.peer = worker
    facade._topology = {"worker_large_capability_results": {"version": 1}}
    try:
        result = await facade.invoke_capability(
            "files", "read", {"path": "synthetic.txt"}, task_id="task-a",
            request_id="request-a",
        )
        assert result["nested"] == {"ok": True}
        assert hashlib.sha256(result["text"].encode()).digest() == hashlib.sha256(expected.encode()).digest()
        assert invoked == [("agent-a", "core.capability.invoke", "task-a")]
        assert counted.sent_sizes and max(counted.sent_sizes) <= MAX_FRAME_BYTES
    finally:
        if hasattr(client, "_large_capability_results"):
            client._large_capability_results.close()
        await asyncio.gather(core.close(), worker.close())


@pytest.mark.asyncio
async def test_capability_cancel_before_result_does_not_leave_temporary_data():
    from orchestrator.function_worker_host import WorkerKernelFacade
    from orchestrator.function_worker_supervisor import FunctionWorkerClient

    core_connection, worker_connection = multiprocessing.Pipe(duplex=True)
    entered = asyncio.Event()
    finish = asyncio.Event()

    class Supervisor:
        async def handle_worker_request(self, client, method, params):
            entered.set()
            await finish.wait()
            return {"text": "x" * (MAX_FRAME_BYTES + 1024)}

    client = FunctionWorkerClient.__new__(FunctionWorkerClient)
    client.agent_name = "agent-a"
    client.supervisor = Supervisor()
    core = JsonConnectionPeer(core_connection, label="cancel-result-core",
                              request_handler=client._handle_request)
    worker = JsonConnectionPeer(worker_connection, label="cancel-result-worker")
    client.peer = core
    core.start()
    worker.start()
    facade = WorkerKernelFacade.__new__(WorkerKernelFacade)
    facade.peer = worker
    facade._topology = {"worker_large_capability_results": {"version": 1}}
    task = asyncio.create_task(facade.invoke_capability(
        "files", "read", {}, task_id="task-a", request_id="request-a"
    ))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        finish.set()
        await asyncio.sleep(0.15)
        assert not client._large_capability_result_store()._results
    finally:
        finish.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if hasattr(client, "_large_capability_results"):
            client._large_capability_results.close()
        await asyncio.gather(core.close(), worker.close())


@pytest.mark.asyncio
async def test_capability_result_corruption_fails_once_and_releases_reference():
    from orchestrator.function_worker_host import WorkerKernelFacade
    from orchestrator.function_worker_large_request import CAPABILITY_READ
    from orchestrator.function_worker_protocol import FunctionWorkerProtocolError
    from orchestrator.function_worker_supervisor import FunctionWorkerClient
    import base64

    core_connection, worker_connection = multiprocessing.Pipe(duplex=True)
    calls = []

    class Supervisor:
        async def handle_worker_request(self, client, method, params):
            calls.append(method)
            return {"text": "x" * (MAX_FRAME_BYTES + 1024)}

    client = FunctionWorkerClient.__new__(FunctionWorkerClient)
    client.agent_name = "agent-a"
    client.supervisor = Supervisor()
    core = JsonConnectionPeer(core_connection, label="corrupt-result-core",
                              request_handler=client._handle_request)
    worker = JsonConnectionPeer(worker_connection, label="corrupt-result-worker")
    core.start()
    worker.start()

    class CorruptPeer:
        async def request(self, method, params=None, **kwargs):
            result = await worker.request(method, params, **kwargs)
            if method == CAPABILITY_READ and params["offset"] == 0:
                raw = bytearray(base64.b64decode(result["data"]))
                raw[0] ^= 1
                result = {**result, "data": base64.b64encode(raw).decode()}
            return result

    facade = WorkerKernelFacade.__new__(WorkerKernelFacade)
    facade.peer = CorruptPeer()
    facade._topology = {"worker_large_capability_results": {"version": 1}}
    try:
        with pytest.raises(FunctionWorkerProtocolError, match="digest mismatch") as raised:
            await facade.invoke_capability("files", "read", {}, task_id="task-a")
        assert "may have occurred" in str(raised.value)
        assert "do not automatically retry" in str(raised.value)
        assert calls == ["core.capability.invoke"]
        assert not client._large_capability_result_store()._results
    finally:
        client._large_capability_result_store().close()
        await asyncio.gather(core.close(), worker.close())


@pytest.mark.asyncio
async def test_capability_older_supervisor_uses_existing_method_once():
    from orchestrator.function_worker_host import WorkerKernelFacade

    calls = []

    class OldPeer:
        async def request(self, method, params=None, **kwargs):
            calls.append(method)
            return {"ok": True}

    facade = WorkerKernelFacade.__new__(WorkerKernelFacade)
    facade.peer = OldPeer()
    facade._topology = {}
    assert await facade.invoke_capability("files", "read", {}, task_id="task-a") == {"ok": True}
    assert calls == ["core.capability.invoke"]


@pytest.mark.asyncio
async def test_capability_result_reference_is_bound_to_one_worker_and_release_closes_file():
    from orchestrator.function_worker_large_request import LargeCapabilityResultStore
    from orchestrator.function_worker_protocol import FunctionWorkerProtocolError

    first = LargeCapabilityResultStore()
    other = LargeCapabilityResultStore()
    transfer_id = "f" * 32
    first.begin(transfer_id)
    descriptor = first.publish(transfer_id, {"text": "x" * (MAX_FRAME_BYTES + 1024)})
    assert descriptor["kind"] == "reference"
    stream = first._results[transfer_id].stream
    with pytest.raises(FunctionWorkerProtocolError, match="unavailable"):
        other.read({"transfer_id": transfer_id, "offset": 0})
    assert first.release({"transfer_id": transfer_id})["removed"]
    assert stream.closed and not first._results
    first.close()
    other.close()


@pytest.mark.asyncio
async def test_cancel_arriving_before_capability_dispatch_prevents_late_publish():
    from orchestrator.function_worker_large_request import LargeCapabilityResultStore
    from orchestrator.function_worker_protocol import FunctionWorkerProtocolError

    store = LargeCapabilityResultStore(idle_seconds=0.05)
    transfer_id = "9" * 32
    store.release({"transfer_id": transfer_id})
    with pytest.raises(FunctionWorkerProtocolError, match="unavailable or repeated"):
        store.begin(transfer_id)
    await asyncio.sleep(0.1)
    assert not store._abandoned
    store.close()


@pytest.mark.asyncio
async def test_capability_result_transport_preserves_existing_authority_resolution():
    from orchestrator.function_worker_host import WorkerKernelFacade
    from orchestrator.function_worker_supervisor import FunctionWorkerClient, FunctionWorkerSupervisor

    core_connection, worker_connection = multiprocessing.Pipe(duplex=True)
    calls = []

    class Broker:
        async def invoke(self, kind, action, args, **kwargs):
            calls.append((kind, action, args, kwargs))
            return {"read": True}

    kernel = SimpleNamespace(
        capability_broker=Broker(),
        agent_authority_roots={"agent-a": "authorized-root"},
        _runtime_map=lambda: {},
    )
    supervisor = FunctionWorkerSupervisor.__new__(FunctionWorkerSupervisor)
    supervisor.kernel = kernel
    client = FunctionWorkerClient.__new__(FunctionWorkerClient)
    client.agent_name = "agent-a"
    client.supervisor = supervisor
    core = JsonConnectionPeer(core_connection, label="authority-core",
                              request_handler=client._handle_request)
    worker = JsonConnectionPeer(worker_connection, label="authority-worker")
    client.peer = core
    core.start()
    worker.start()
    facade = WorkerKernelFacade.__new__(WorkerKernelFacade)
    facade.peer = worker
    facade._topology = {"worker_large_capability_results": {"version": 1}}
    try:
        result = await facade.invoke_capability(
            "files", "read", {"_authorized_roots": ["untrusted"], "path": "input"},
            task_id="task-a", request_id="request-a",
        )
        assert result == {"read": True}
        assert len(calls) == 1
        assert calls[0][2]["_authorized_roots"] == ["authorized-root"]
        assert calls[0][3]["agent_id"] == "agent-a"
        assert calls[0][3]["task_id"] == "task-a"
    finally:
        client._large_capability_result_store().close()
        await asyncio.gather(core.close(), worker.close())


def _large_transport_process(connection):
    import asyncio
    import hashlib
    from types import SimpleNamespace
    from orchestrator.function_worker_host import FunctionWorkerHost, WorkerKernelFacade
    from orchestrator.function_worker_protocol import MAX_FRAME_BYTES, JsonConnectionPeer

    async def run():
        host = FunctionWorkerHost.__new__(FunctionWorkerHost)
        host.agent_name = "process-agent"
        host.phase = "ACTIVE"
        host.accepting = True

        async def enqueue(chat_id, prompt, source, summary, **kwargs):
            assert kwargs["request_metadata"]["owner_id"] == "owner-a"
            return hashlib.sha256(prompt.encode()).hexdigest()

        async def no_transcription(*args, **kwargs):
            return None

        async def emit_metadata():
            return None

        host.runtime = SimpleNamespace(enqueue_request=enqueue)
        host._begin_native_voice_transcription = no_transcription
        host.emit_metadata = emit_metadata
        peer = JsonConnectionPeer(connection, label="separate-worker",
                                  request_handler=host.handle_request)
        peer.start()
        facade = WorkerKernelFacade.__new__(WorkerKernelFacade)
        facade.peer = peer
        facade._topology = {"worker_large_capability_results": {"version": 1}}
        try:
            result = await facade.invoke_capability("files", "read", {}, task_id="task-a")
            assert len(result["text"]) == MAX_FRAME_BYTES + 1024
            await peer.emit("fixture.capability", {
                "sha256": hashlib.sha256(result["text"].encode()).hexdigest(),
            })
            await peer.wait_closed()
        finally:
            if hasattr(host, "_large_request_uploads"):
                host._large_request_uploads.close()
            await peer.close()

    asyncio.run(run())


@pytest.mark.asyncio
async def test_large_request_and_capability_result_cross_separate_processes():
    from orchestrator.function_worker_supervisor import FunctionWorkerClient

    context = multiprocessing.get_context("spawn")
    core_connection, worker_connection = context.Pipe(duplex=True)
    process = context.Process(target=_large_transport_process, args=(worker_connection,))
    process.start()
    worker_connection.close()
    events = asyncio.Queue()
    calls = []

    class Supervisor:
        async def handle_worker_request(self, client, method, params):
            calls.append((client.agent_name, method))
            return {"text": "y" * (MAX_FRAME_BYTES + 1024)}

    client = FunctionWorkerClient.__new__(FunctionWorkerClient)
    client.agent_name = "process-agent"
    client.supervisor = Supervisor()

    async def event(name, payload):
        await events.put((name, payload))

    counted = _CountingConnection(core_connection)
    peer = JsonConnectionPeer(counted, label="separate-core",
                              request_handler=client._handle_request, event_handler=event)
    client.peer = peer
    peer.start()
    routed_client = _Client(peer)
    routed_client.agent_name = "process-agent"
    handle = AgentRuntimeHandle(SimpleNamespace(), routed_client, {})
    prompt = "z" * (MAX_FRAME_BYTES + 1024)
    try:
        result = await asyncio.wait_for(handle.enqueue_request(
            17, prompt, "session-api", "cross process",
            request_metadata={"owner_id": "owner-a"},
        ), timeout=45)
        assert result == hashlib.sha256(prompt.encode()).hexdigest()
        name, payload = await asyncio.wait_for(events.get(), timeout=45)
        assert name == "fixture.capability"
        assert payload["sha256"] == hashlib.sha256(
            ("y" * (MAX_FRAME_BYTES + 1024)).encode()
        ).hexdigest()
        assert calls == [("process-agent", "core.capability.invoke")]
        assert counted.sent_sizes and max(counted.sent_sizes) <= MAX_FRAME_BYTES
    finally:
        await peer.close()
        await asyncio.to_thread(process.join, 10)
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 5)
        if hasattr(client, "_large_capability_results"):
            client._large_capability_results.close()
        exitcode = process.exitcode
        process.close()
    assert exitcode == 0


@pytest.mark.asyncio
async def test_old_worker_rejects_large_request_before_admission_with_clear_error():
    from orchestrator.function_worker_large_request import BEGIN
    from orchestrator.function_worker_protocol import FunctionWorkerProtocolError

    left_connection, right_connection = multiprocessing.Pipe(duplex=True)
    calls = []

    class FunctionWorkerStateError(RuntimeError):
        pass

    async def old_worker(method, params):
        calls.append(method)
        if method == BEGIN:
            raise FunctionWorkerStateError(f"unknown Worker request: {method}")
        if method == "runtime.enqueue_request":
            return "accepted"
        raise FunctionWorkerStateError(f"unknown Worker request: {method}")

    left = JsonConnectionPeer(left_connection, label="old-worker-core")
    right = JsonConnectionPeer(right_connection, label="old-worker",
                               request_handler=old_worker)
    left.start()
    right.start()
    handle = AgentRuntimeHandle(SimpleNamespace(), _Client(left), {})
    try:
        assert await handle.enqueue_request(1, "small", "api", "small") == "accepted"
        with pytest.raises(FunctionWorkerProtocolError, match="does not support bounded large"):
            await handle.enqueue_request(1, "x" * (MAX_FRAME_BYTES + 1024), "api", "large")
        assert calls.count("runtime.enqueue_request") == 1
        assert calls.count(BEGIN) == 1
    finally:
        await asyncio.gather(left.close(), right.close())


@pytest.mark.asyncio
async def test_capability_result_reference_closes_when_private_peer_disconnects():
    from orchestrator.function_worker_supervisor import FunctionWorkerClient

    core_connection, worker_connection = multiprocessing.Pipe(duplex=True)
    client = FunctionWorkerClient.__new__(FunctionWorkerClient)
    peer = JsonConnectionPeer(core_connection, label="disconnect-core")
    other = JsonConnectionPeer(worker_connection, label="disconnect-worker")
    client.peer = peer
    peer.start()
    other.start()
    store = client._large_capability_result_store()
    transfer_id = "8" * 32
    store.begin(transfer_id)
    store.publish(transfer_id, {"text": "x" * (MAX_FRAME_BYTES + 1024)})
    stream = store._results[transfer_id].stream
    try:
        await other.close()
        await asyncio.wait_for(peer.wait_closed(), 5)
        await asyncio.wait_for(client._large_result_cleanup_task, 5)
        assert stream.closed and not store._results
    finally:
        store.close()
        await asyncio.gather(peer.close(), other.close())


@pytest.mark.asyncio
async def test_worker_can_route_large_agent_prompt_and_capability_arguments(tmp_path):
    from orchestrator.function_generation import build_source_manifest_from_entries
    from orchestrator.function_worker_host import RemoteRuntimeReference, WorkerKernelFacade
    from orchestrator.function_worker_supervisor import FunctionWorkerClient
    from orchestrator.runtime_contract import RuntimeFingerprint

    integer_fields = {"pointer_bits", "core_api", "function_api", "worker_protocol", "generation_schema"}
    fingerprint = {
        name: (1 if name in integer_fields else "fixture")
        for name in RuntimeFingerprint.__dataclass_fields__
    }
    fingerprint["role"] = "functions"
    fingerprint["installation_digest"] = ""
    bootstrap = {
        "agent_name": "source-agent", "nonce": "fixture",
        "code_root": str(tmp_path), "bridge_home": str(tmp_path),
        "generation_root": str(tmp_path), "runtime": fingerprint,
        "manifest": build_source_manifest_from_entries([]).to_dict(),
        "topology": {
            "worker_large_requests_to_core": {"version": 1},
            "worker_large_capability_results": {"version": 1},
        },
    }
    core_connection, worker_connection = multiprocessing.Pipe(duplex=True)
    calls = []
    text = "提示" + "x" * (MAX_FRAME_BYTES + 1024)

    class Supervisor:
        async def handle_worker_request(self, client, method, params):
            calls.append((client.agent_name, method))
            if method == "core.route.enqueue_api_text":
                assert params["agent_name"] == "target-agent"
                assert hashlib.sha256(params["text"].encode()).digest() == hashlib.sha256(text.encode()).digest()
                return "target-request"
            if method == "core.capability.invoke":
                assert hashlib.sha256(params["args"]["value"].encode()).digest() == hashlib.sha256(text.encode()).digest()
                return {"text": text}
            raise AssertionError(method)

    client = FunctionWorkerClient.__new__(FunctionWorkerClient)
    client.agent_name = "source-agent"
    client.supervisor = Supervisor()
    core = JsonConnectionPeer(core_connection, label="large-outbound-core",
                              request_handler=client._handle_request)
    client.peer = core
    host = FunctionWorkerHost(worker_connection, bootstrap)
    core.start()
    host.peer.start()
    remote = RemoteRuntimeReference(host.peer, {"name": "target-agent"})
    facade = WorkerKernelFacade.__new__(WorkerKernelFacade)
    facade.peer = host.peer
    facade._topology = bootstrap["topology"]
    try:
        assert await remote.enqueue_api_text(text, source="api") == "target-request"
        result = await facade.invoke_capability("files", "write", {"value": text}, task_id="task-a")
        assert hashlib.sha256(result["text"].encode()).digest() == hashlib.sha256(text.encode()).digest()
        assert calls == [
            ("source-agent", "core.route.enqueue_api_text"),
            ("source-agent", "core.capability.invoke"),
        ]
    finally:
        for name in ("_large_capability_results", "_worker_large_request_uploads"):
            resource = getattr(client, name, None)
            if resource is not None:
                resource.close()
        await asyncio.gather(core.close(), host.peer.close())


@pytest.mark.asyncio
async def test_large_agent_message_crosses_both_worker_routes_without_changing_owner(tmp_path):
    from orchestrator.function_generation import build_source_manifest_from_entries
    from orchestrator.function_worker_host import RemoteRuntimeReference
    from orchestrator.function_worker_supervisor import FunctionWorkerClient, FunctionWorkerSupervisor
    from orchestrator.runtime_contract import RuntimeFingerprint

    integer_fields = {"pointer_bits", "core_api", "function_api", "worker_protocol", "generation_schema"}
    fingerprint = {name: (1 if name in integer_fields else "fixture")
                   for name in RuntimeFingerprint.__dataclass_fields__}
    fingerprint.update(role="functions", installation_digest="")
    bootstrap = {
        "agent_name": "source-agent", "nonce": "fixture", "code_root": str(tmp_path),
        "bridge_home": str(tmp_path), "generation_root": str(tmp_path),
        "runtime": fingerprint, "manifest": build_source_manifest_from_entries([]).to_dict(),
        "topology": {"worker_large_requests_to_core": {"version": 1}},
    }
    source_core_connection, source_worker_connection = multiprocessing.Pipe(duplex=True)
    target_core_connection, target_worker_connection = multiprocessing.Pipe(duplex=True)
    source_worker_counted = _CountingConnection(source_worker_connection)
    target_core_counted = _CountingConnection(target_core_connection)
    admissions = []

    async def target_enqueue(text, **kwargs):
        admissions.append((hashlib.sha256(text.encode()).hexdigest(), kwargs))
        return "accepted-target"

    async def emit_metadata():
        return None

    target_host = FunctionWorkerHost.__new__(FunctionWorkerHost)
    target_host.agent_name = "target-agent"
    target_host.phase = "ACTIVE"
    target_host.accepting = True
    target_host.runtime = SimpleNamespace(enqueue_api_text=target_enqueue)
    target_host.emit_metadata = emit_metadata
    target_core = JsonConnectionPeer(target_core_counted, label="target-core")
    target_worker = JsonConnectionPeer(target_worker_connection, label="target-worker",
                                       request_handler=target_host.handle_request)
    target_client = _Client(target_core)
    target_client.agent_name = "target-agent"
    target_handle = AgentRuntimeHandle(SimpleNamespace(), target_client, {})
    supervisor = FunctionWorkerSupervisor.__new__(FunctionWorkerSupervisor)
    supervisor.kernel = SimpleNamespace(_runtime_map=lambda: {"target-agent": target_handle})
    source_client = FunctionWorkerClient.__new__(FunctionWorkerClient)
    source_client.agent_name = "source-agent"
    source_client.supervisor = supervisor
    source_core = JsonConnectionPeer(source_core_connection, label="source-core",
                                     request_handler=source_client._handle_request)
    source_client.peer = source_core
    source_host = FunctionWorkerHost(source_worker_counted, bootstrap)
    for peer in (target_core, target_worker, source_core, source_host.peer):
        peer.start()
    remote = RemoteRuntimeReference(source_host.peer, {"name": "target-agent"})
    text = "长消息" + "q" * (MAX_FRAME_BYTES + 1024)
    try:
        result = await remote.enqueue_api_text(
            text, source="session-api", request_metadata={"owner_id": "owner-a"},
            idempotency_key="one-message",
        )
        assert result == "accepted-target"
        assert len(admissions) == 1
        assert admissions[0][0] == hashlib.sha256(text.encode()).hexdigest()
        assert admissions[0][1]["request_metadata"]["owner_id"] == "owner-a"
        assert admissions[0][1]["idempotency_key"] == "one-message"
        assert max(source_worker_counted.sent_sizes) <= MAX_FRAME_BYTES
        assert max(target_core_counted.sent_sizes) <= MAX_FRAME_BYTES
        assert not source_client._worker_large_request_store()._uploads
        assert not target_host._large_request_store()._uploads
    finally:
        for owner, name in ((source_client, "_worker_large_request_uploads"),
                            (target_host, "_large_request_uploads")):
            resource = getattr(owner, name, None)
            if resource is not None:
                resource.close()
        await asyncio.gather(target_core.close(), target_worker.close(),
                             source_core.close(), source_host.peer.close())


@pytest.mark.asyncio
async def test_older_shared_functions_keeps_single_frame_behavior_without_retry():
    from orchestrator.function_worker_large_request import BoundedWorkerPeer
    from orchestrator.function_worker_protocol import FunctionWorkerProtocolError

    left_connection, right_connection = multiprocessing.Pipe(duplex=True)
    calls = []

    async def old_shared(method, params):
        calls.append(method)
        return "accepted"

    raw = JsonConnectionPeer(left_connection, label="old-shared-worker")
    other = JsonConnectionPeer(right_connection, label="old-shared-core",
                               request_handler=old_shared)
    worker = BoundedWorkerPeer(raw, {})
    worker.start()
    other.start()
    try:
        assert await worker.request("core.route.enqueue_api_text", {"text": "small"}) == "accepted"
        with pytest.raises(FunctionWorkerProtocolError, match="IPC frame exceeds"):
            await worker.request("core.route.enqueue_api_text", {"text": "x" * (MAX_FRAME_BYTES + 1024)})
        assert calls == ["core.route.enqueue_api_text"]
    finally:
        await asyncio.gather(worker.close(), other.close())


@pytest.mark.asyncio
async def test_large_request_configured_aggregate_budget_is_reported_and_released():
    from orchestrator.function_worker_large_request import ABORT, BEGIN, LargeRequestReceiver
    from orchestrator.function_worker_protocol import FunctionWorkerProtocolError

    receiver = LargeRequestReceiver(max_bytes=30, max_transfers=2)

    async def dispatch(method, params):
        pytest.fail("budget test must not dispatch")

    first = {"transfer_id": "1" * 32, "method": "runtime.enqueue_request",
             "size": 20, "sha256": "0" * 64}
    second = {**first, "transfer_id": "2" * 32, "size": 15}
    assert receiver.capability()["max_spooled_bytes"] == 30
    await receiver.handle(BEGIN, first, dispatch)
    with pytest.raises(FunctionWorkerProtocolError, match="configured logical byte budget"):
        await receiver.handle(BEGIN, second, dispatch)
    await receiver.handle(ABORT, {"transfer_id": first["transfer_id"]}, dispatch)
    await receiver.handle(BEGIN, second, dispatch)
    receiver.close()
    assert not receiver._uploads and receiver._reserved_bytes == 0


@pytest.mark.asyncio
async def test_selected_transfer_budget_is_separate_from_operation_timeout(monkeypatch):
    from orchestrator.function_worker_large_request import (
        APPEND, LargeRequestReceiver, send_large_request,
    )

    monkeypatch.setenv("HASHI_FUNCTION_LARGE_TRANSFER_TIMEOUT_SECONDS", "2")
    receiver = LargeRequestReceiver(max_bytes=10000)
    calls = []

    async def dispatch(method, params):
        calls.append((method, params))
        return "accepted"

    async def call(method, params=None, *, timeout):
        async def remote():
            if method == APPEND:
                await asyncio.sleep(0.06)
            return await receiver.handle(method, params or {}, dispatch)
        return await asyncio.wait_for(remote(), timeout)

    try:
        result = await send_large_request(
            call, "runtime.enqueue_api_text", {"text": "whole"}, timeout=0.02
        )
        assert result == "accepted"
        assert calls == [("runtime.enqueue_api_text", {"text": "whole"})]
    finally:
        receiver.close()


@pytest.mark.asyncio
async def test_transfer_timeout_bounds_upload_even_with_longer_operation_timeout(monkeypatch):
    from orchestrator import function_worker_large_request as transport
    monkeypatch.setattr(transport, "_transfer_timeout", lambda: 0.02)
    receiver = transport.LargeRequestReceiver(max_bytes=10000)
    calls = []

    async def dispatch(method, params):
        calls.append(method)
        return "accepted"

    async def call(method, params=None, *, timeout):
        if method == transport.APPEND:
            await asyncio.sleep(0.06)
        return await receiver.handle(method, params or {}, dispatch)

    try:
        with pytest.raises(TimeoutError):
            await transport.send_large_request(
                call, "runtime.enqueue_api_text", {"text": "whole"}, timeout=1
            )
        assert not calls and not receiver._uploads
    finally:
        receiver.close()


@pytest.mark.asyncio
async def test_abort_overtaking_large_request_begin_prevents_orphaned_upload():
    from orchestrator.function_worker_large_request import ABORT, BEGIN, LargeRequestReceiver
    from orchestrator.function_worker_protocol import FunctionWorkerProtocolError

    receiver = LargeRequestReceiver(max_bytes=1000, idle_seconds=0.05)
    transfer_id = "7" * 32

    async def dispatch(method, params):
        pytest.fail("cancelled request must not dispatch")

    try:
        await receiver.handle(ABORT, {"transfer_id": transfer_id}, dispatch)
        with pytest.raises(FunctionWorkerProtocolError, match="cancelled|unavailable"):
            await receiver.handle(BEGIN, {
                "transfer_id": transfer_id, "method": "runtime.enqueue_request",
                "size": 10, "sha256": "0" * 64,
            }, dispatch)
        assert not receiver._uploads
        await asyncio.sleep(0.1)
        assert not receiver._abandoned
    finally:
        receiver.close()


@pytest.mark.asyncio
async def test_capability_post_effect_result_storage_failure_reports_unknown_effect_once():
    from orchestrator.function_worker_host import WorkerKernelFacade
    from orchestrator.function_worker_large_request import LargeCapabilityResultStore
    from orchestrator.function_worker_protocol import FunctionWorkerRemoteError
    from orchestrator.function_worker_supervisor import FunctionWorkerClient

    calls = []

    class Supervisor:
        async def handle_worker_request(self, client, method, params):
            calls.append((method, params["task_id"]))
            return {"effect": "completed"}

    class FailingStore(LargeCapabilityResultStore):
        def publish(self, transfer_id, value):
            raise OSError("synthetic private temporary location")

    core_connection, worker_connection = multiprocessing.Pipe(duplex=True)
    client = FunctionWorkerClient.__new__(FunctionWorkerClient)
    client.agent_name = "agent-a"
    client.supervisor = Supervisor()
    client._large_capability_results = FailingStore()
    core = JsonConnectionPeer(core_connection, label="failed-result-core",
                              request_handler=client._handle_request)
    worker = JsonConnectionPeer(worker_connection, label="failed-result-worker")
    core.start()
    worker.start()
    facade = WorkerKernelFacade.__new__(WorkerKernelFacade)
    facade.peer = worker
    facade._topology = {"worker_large_capability_results": {"version": 1}}
    try:
        with pytest.raises(FunctionWorkerRemoteError) as raised:
            await facade.invoke_capability("files", "write", {}, task_id="task-a")
        error = str(raised.value)
        assert "may have occurred" in error
        assert "do not automatically retry" in error
        assert "synthetic private temporary location" not in error
        assert calls == [("core.capability.invoke", "task-a")]
        assert not client._large_capability_results._pending
    finally:
        client._large_capability_results.close()
        await asyncio.gather(core.close(), worker.close())


@pytest.mark.asyncio
async def test_lost_capability_reply_reports_unknown_effect_without_replay():
    from orchestrator.function_worker_host import WorkerKernelFacade
    from orchestrator.function_worker_large_request import CAPABILITY_INVOKE, CAPABILITY_RELEASE
    from orchestrator.function_worker_protocol import FunctionWorkerProtocolError

    calls = []

    class LostReply:
        async def request(self, method, params=None, **kwargs):
            calls.append(method)
            if method == CAPABILITY_INVOKE:
                raise TimeoutError("synthetic private transport detail")
            if method == CAPABILITY_RELEASE:
                return {"removed": False}
            pytest.fail("unexpected capability request")

    facade = WorkerKernelFacade.__new__(WorkerKernelFacade)
    facade.peer = LostReply()
    facade._topology = {"worker_large_capability_results": {"version": 1}}
    with pytest.raises(FunctionWorkerProtocolError, match="may have occurred") as raised:
        await facade.invoke_capability("files", "write", {}, task_id="task-a")
    assert "do not automatically retry" in str(raised.value)
    assert "synthetic private transport detail" not in str(raised.value)
    assert calls == [CAPABILITY_INVOKE, CAPABILITY_RELEASE]


@pytest.mark.asyncio
async def test_capability_explicit_broker_denial_keeps_its_error_without_retry():
    from orchestrator.function_worker_host import WorkerKernelFacade
    from orchestrator.function_worker_large_request import CAPABILITY_INVOKE, CAPABILITY_RELEASE
    from orchestrator.function_worker_protocol import FunctionWorkerRemoteError

    calls = []

    class DeniedPeer:
        async def request(self, method, params=None, **kwargs):
            calls.append(method)
            if method == CAPABILITY_INVOKE:
                raise FunctionWorkerRemoteError(method, {"type": "PermissionError", "message": "denied"})
            if method == CAPABILITY_RELEASE:
                return {"removed": False}
            pytest.fail("unexpected capability request")

    facade = WorkerKernelFacade.__new__(WorkerKernelFacade)
    facade.peer = DeniedPeer()
    facade._topology = {"worker_large_capability_results": {"version": 1}}
    with pytest.raises(FunctionWorkerRemoteError) as raised:
        await facade.invoke_capability("files", "write", {}, task_id="task-a")
    assert raised.value.method == "core.capability.invoke"
    assert raised.value.error["type"] == "PermissionError"
    assert calls == [CAPABILITY_INVOKE, CAPABILITY_RELEASE]
