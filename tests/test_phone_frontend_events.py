"""Ordering and subscriber scope for the optional /phone presenter."""
import asyncio
from types import SimpleNamespace
import pytest

from orchestrator.frontend_live_voice.protocol import CallBinding
from orchestrator.frontend_optional_egress import PhoneCallFrontendEvents


async def test_phone_return_waits_for_departure_and_retains_frozen_subscriber(monkeypatch):
    from orchestrator import frontend_optional_egress as fc
    api = SimpleNamespace()
    subscribers = ["agentpresence"]
    monkeypatch.setattr(fc, "configured_owner_subscribers", lambda *_: tuple(subscribers))
    release, entered = asyncio.Event(), asyncio.Event()
    events = []

    async def dispatch(_api, **values):
        if values["kind"] == "call.started":
            entered.set()
            await release.wait()
        events.append(values)
        return []

    monkeypatch.setattr(fc, "dispatch_call_frontend_event", dispatch)
    publish = PhoneCallFrontendEvents(api)
    binding = CallBinding("owner", "HASHI3", "gen", "agent", "session", 1, "phone-1", 1, "provider-1")
    publish(binding, "started")
    await entered.wait()
    subscribers.clear()
    publish(binding, "ended")
    await asyncio.sleep(0)
    assert events == []
    release.set()
    await asyncio.gather(*api._optional_frontend_tasks)
    assert [e["kind"] for e in events] == ["call.started", "call.ended"]
    assert all(e["subscribers"] == ("agentpresence",) for e in events)
    assert all(e["payload"] == {"mode": "phone", "instance_id": "HASHI3"} for e in events)
    assert publish.subscribers == {} and publish.tails == {}


async def test_optional_phone_presenter_failure_does_not_block_return(monkeypatch):
    from orchestrator import frontend_optional_egress as fc
    api, events = SimpleNamespace(), []
    monkeypatch.setattr(fc, "configured_owner_subscribers", lambda *_: ("agentpresence",))

    async def dispatch(_api, **values):
        events.append(values["kind"])
        if values["kind"] == "call.started":
            raise OSError("AP unavailable")
        return []

    monkeypatch.setattr(fc, "dispatch_call_frontend_event", dispatch)
    publish = PhoneCallFrontendEvents(api)
    binding = CallBinding("owner", "HASHI3", "gen", "agent", "session", 1, "phone-1", 1, "provider-1")
    publish(binding, "started")
    publish(binding, "ended")
    await asyncio.gather(*api._optional_frontend_tasks, return_exceptions=True)
    assert events == ["call.started", "call.ended"]
