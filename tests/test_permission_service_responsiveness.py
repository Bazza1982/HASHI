"""A durable permission commit must not freeze the service event loop."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from threading import Event, Thread

import pytest

from orchestrator.permissions.wire import PermissionServiceRouter, decode, request_frame
from tests.test_permission_actions import _issue, setup_actions as setup_actions


@pytest.mark.asyncio
async def test_real_ledger_commit_does_not_starve_service_responses(setup_actions, monkeypatch):
    authority, _, _, _, context, scope, request = setup_actions
    entered, release, responded = Event(), Event(), Event()
    advance = authority._advance_continuity
    observed = []

    def held_commit(connection):
        advance(connection)
        entered.set()
        assert release.wait(10), "commit was not released"

    def release_after_observation():
        assert entered.wait(10)
        observed.append(responded.wait(1))
        release.set()

    class Receiver:
        def render(self):
            return _issue(authority, context, request, [scope]).action_id

        def health(self):
            return "responsive"

    monkeypatch.setattr(authority, "_advance_continuity", held_commit)
    router = PermissionServiceRouter(Receiver(), methods={"render", "health"})
    releaser = Thread(target=release_after_observation)
    releaser.start()

    async def observe():
        while not entered.is_set():
            await asyncio.sleep(.001)
        response = await router.dispatch_async(request_frame("health", {}, request_id="health"))
        assert decode(response["result"]) == "responsive"
        responded.set()

    try:
        await asyncio.gather(
            router.dispatch_async(request_frame("render", {}, request_id="render")),
            observe(),
        )
    finally:
        release.set()
        await asyncio.to_thread(releaser.join, 10)
    assert observed == [True], "a durable SQLite commit starved an independent service response"
    assert authority.decision_count(request.request_id) == 0


@pytest.mark.asyncio
async def test_cancelled_dispatch_keeps_ownership_until_mutation_settles():
    entered, release = Event(), Event()
    effects = []
    peer = ContextVar("authenticated_test_peer", default=None)

    class Receiver:
        def mutate(self):
            assert peer.get() == "actual-authenticated-peer"
            entered.set()
            assert release.wait(10)
            effects.append("one")

    # The service uses a separate executor, so pipe receive/monitor workers
    # remain available while database operations queue.
    with ThreadPoolExecutor(max_workers=2) as executor:
        router = PermissionServiceRouter(Receiver(), methods={"mutate"}, executor=executor)
        token = peer.set("actual-authenticated-peer")
        try:
            dispatch = asyncio.create_task(router.dispatch_async(request_frame("mutate", {}, request_id="one")))
        finally:
            peer.reset(token)
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            dispatch.cancel()
            await asyncio.sleep(.05)
            assert not dispatch.done(), "cancellation abandoned a still-mutating service thread"
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await dispatch
    assert effects == ["one"]
# HASHI-Fingerprint: hfp_0994563f58f146afad216f4f86b7d808
