"""Cancellation must settle owned storage and leave no fresh scheduling lease."""
import asyncio
import threading

import pytest

from orchestrator.exchange_ingress import ExchangeIngressService
from orchestrator.session_store import SessionStore
from tests.test_exchange_ingress import _configure, _delivery, _evidence


def fixture(tmp_path):
    _configure(tmp_path)
    store = SessionStore(tmp_path / "state/sessions.sqlite3", instance_id="HASHI2")
    calls, executions = [], []
    entered = asyncio.Event()
    gate = asyncio.Event()
    block = {"at": None}

    class Runtime:
        startup_success = True

        async def enqueue_api_text(self, text, **kwargs):
            calls.append(kwargs["idempotency_key"])
            if block["at"] == "before":
                block["at"] = None
                entered.set()
                await gate.wait()
            metadata = kwargs["request_metadata"]
            accepted = store.accept_run(
                session_id=metadata["session_id"], owner_id=metadata["owner_id"],
                agent_id="reviewer", request_id=f"request-{len(calls)}",
                text=text, source=kwargs["source"],
                idempotency_key=kwargs["idempotency_key"],
            )
            if not accepted.replayed:
                store.mark_request_running(accepted.request_id, worker_id="synthetic-worker")
                executions.append(accepted.run_id)
            if block["at"] == "after":
                block["at"] = None
                entered.set()
                await gate.wait()
            return accepted.request_id

    service = ExchangeIngressService(hashi_root=tmp_path,
        runtime_map=lambda: {"reviewer": Runtime()}, session_store=store)
    delivery = _delivery()
    prompt, evidence = _evidence(tmp_path, delivery)
    record = service.accept(evidence=evidence, prompt=prompt)
    return service, store, record, calls, executions, entered, gate, block


async def thread_entered(event):
    for _ in range(100):
        if event.is_set():
            return
        await asyncio.sleep(.005)
    pytest.fail("storage claim did not reach deterministic barrier")


def test_cancelled_claim_drains_before_releasing_its_own_lease(tmp_path, monkeypatch):
    async def exercise():
        service, store, record, calls, executions, *_ = fixture(tmp_path)
        entered, release = threading.Event(), threading.Event()
        original = service.inbox.claim

        def held(*args, **kwargs):
            result = original(*args, **kwargs)
            entered.set()
            assert release.wait(3)
            return result

        monkeypatch.setattr(service.inbox, "claim", held)
        task = asyncio.create_task(service._schedule_key(record.inbox_key))
        try:
            await thread_entered(entered)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            assert not task.done()
            assert service.inbox.get(record.inbox_key).state == "scheduling"
            assert calls == []
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert service.inbox.get(record.inbox_key).state == "accepted"
            monkeypatch.setattr(service.inbox, "claim", original)
            recovered = await service.recover_pending()
            assert len(recovered) == 1 and recovered[0].state == "scheduled"
            assert len(executions) == 1
            assert len(store.list_active_runs(owner_id="exchange:authority_1:actor_alice")) == 1
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())


@pytest.mark.parametrize("boundary", ["before", "after"])
def test_cancelled_enqueue_recovers_immediately_with_one_durable_run(tmp_path, boundary):
    async def exercise():
        service, store, record, calls, executions, entered, gate, block = fixture(tmp_path)
        block["at"] = boundary
        task = asyncio.create_task(service._schedule_key(record.inbox_key))
        try:
            await asyncio.wait_for(entered.wait(), timeout=3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert service.inbox.get(record.inbox_key).state == "accepted"
            recovered = await service.recover_pending()
            assert len(recovered) == 1 and recovered[0].state == "scheduled"
            assert len(executions) == 1
            run = store.get_run_by_request(recovered[0].request_id)
            assert run["run_id"] == executions[0]
            assert len(store.list_active_runs(owner_id=store.get_session(run["session_id"])["owner_id"])) == 1
            assert calls == ["hchat:message_1", "hchat:message_1"]
            assert (await service._schedule_key(record.inbox_key)).request_id == run["request_id"]
            assert len(executions) == 1
        finally:
            gate.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())


def test_cancelled_unsuccessful_claim_does_not_release_another_owner(tmp_path, monkeypatch):
    async def exercise():
        service, _, record, calls, _, *_ = fixture(tmp_path)
        owner = service.inbox.claim(record.inbox_key)
        entered, release = threading.Event(), threading.Event()
        original = service.inbox.claim

        def held(*args, **kwargs):
            result = original(*args, **kwargs)
            assert result is None
            entered.set()
            assert release.wait(3)
            return result

        monkeypatch.setattr(service.inbox, "claim", held)
        task = asyncio.create_task(service._schedule_key(record.inbox_key))
        try:
            await thread_entered(entered)
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            current = service.inbox.get(record.inbox_key)
            assert current.state == "scheduling" and current.updated_at == owner.updated_at
            assert calls == []
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())
# HASHI-Fingerprint: hfp_8c3fae14875e40c0a235e3e977b02c92

def test_repeated_cancellation_waits_for_release_transaction(tmp_path, monkeypatch):
    async def exercise():
        service, _, record, calls, _, entered, gate, block = fixture(tmp_path)
        block["at"] = "before"
        releasing, finish = threading.Event(), threading.Event()
        original = service.inbox.release

        def held(*args, **kwargs):
            releasing.set()
            assert finish.wait(3)
            return original(*args, **kwargs)

        monkeypatch.setattr(service.inbox, "release", held)
        task = asyncio.create_task(service._schedule_key(record.inbox_key))
        try:
            await asyncio.wait_for(entered.wait(), timeout=3)
            task.cancel()
            await thread_entered(releasing)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert service.inbox.get(record.inbox_key).state == "accepted"
            assert calls == ["hchat:message_1"]
        finally:
            finish.set()
            gate.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())


def test_cancelled_claim_cannot_release_a_replacement_lease(tmp_path, monkeypatch):
    async def exercise():
        service, _, record, calls, _, *_ = fixture(tmp_path)
        entered, finish = threading.Event(), threading.Event()
        original = service.inbox.claim

        def held(*args, **kwargs):
            claimed = original(*args, **kwargs)
            entered.set()
            assert finish.wait(3)
            return claimed

        monkeypatch.setattr(service.inbox, "claim", held)
        task = asyncio.create_task(service._schedule_key(record.inbox_key))
        try:
            await thread_entered(entered)
            previous = service.inbox.get(record.inbox_key)
            replacement = original(record.inbox_key, recover_leased=True,
                                   now=previous.updated_at + 1)
            task.cancel()
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            current = service.inbox.get(record.inbox_key)
            assert current.state == "scheduling"
            assert current.updated_at == replacement.updated_at
            assert calls == []
        finally:
            finish.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())
