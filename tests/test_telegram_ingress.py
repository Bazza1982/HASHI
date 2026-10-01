from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator.telegram_ingress import CoreTelegramIngress
from orchestrator.function_worker_supervisor import AgentRuntimeHandle


class _Update:
    def __init__(self, update_id: int) -> None:
        self.update_id = update_id

    def to_dict(self):
        return {"update_id": self.update_id, "message": {"text": "hello"}}


class _Bot:
    def __init__(self, token: str) -> None:
        self.token = token
        self.calls: list[object] = []
        self.release = asyncio.Event()
        self.update = _Update(7)

    async def initialize(self):
        self.calls.append("initialize")

    async def delete_webhook(self, *, drop_pending_updates):
        self.calls.append(("delete_webhook", drop_pending_updates))

    async def get_updates(self, **kwargs):
        self.calls.append(("get_updates", kwargs["offset"]))
        if kwargs["offset"] is None:
            return [self.update]
        await self.release.wait()
        return []

    async def shutdown(self):
        self.calls.append("shutdown")


@pytest.mark.asyncio
async def test_core_ingress_advances_offset_only_after_worker_accepts(monkeypatch):
    bot = _Bot("token")
    accepted = asyncio.Event()
    attempts = 0
    statuses = []

    class _Handle:
        async def deliver_telegram_update(self, payload):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("route temporarily unavailable")
            assert payload["update_id"] == 7
            accepted.set()
            return True

    async def status(connected):
        statuses.append(connected)

    monkeypatch.setattr(
        "orchestrator.telegram_ingress.TELEGRAM_RETRY_SECONDS",
        0.0,
    )
    ingress = CoreTelegramIngress(
        agent_name="alpha",
        token="token",
        handle_lookup=lambda _name: _Handle(),
        status_callback=status,
        bot_factory=lambda _token: bot,
    )

    await ingress.start(drop_pending_updates=False)
    await asyncio.wait_for(accepted.wait(), timeout=1.0)

    assert attempts == 2
    assert ingress.offset == 8
    assert bot.calls[:2] == ["initialize", ("delete_webhook", False)]
    assert ("get_updates", None) in bot.calls
    assert statuses[:3] == [True, False, True]

    await ingress.stop()
    assert ingress.is_running is False
    assert statuses[-1] is False
    assert bot.calls[-1] == "shutdown"


@pytest.mark.asyncio
async def test_core_ingress_shutdown_can_skip_worker_status_callback():
    bot = _Bot("token")
    statuses = []

    class _Handle:
        async def deliver_telegram_update(self, _payload):
            return True

    async def status(connected):
        statuses.append(connected)

    ingress = CoreTelegramIngress(
        agent_name="alpha",
        token="token",
        handle_lookup=lambda _name: _Handle(),
        status_callback=status,
        bot_factory=lambda _token: bot,
    )

    await ingress.start(drop_pending_updates=False)
    for _ in range(100):
        if statuses:
            break
        await asyncio.sleep(0.001)
    await ingress.stop(notify_status=False)

    assert statuses == [True]
    assert ingress.connected is False
    assert ingress.is_running is False


@pytest.mark.asyncio
async def test_ingress_is_not_healthy_until_poll_succeeds_and_stalled_poll_expires(
    monkeypatch,
):
    bot = _Bot("token")
    entered = asyncio.Event()
    expired = asyncio.Event()
    statuses = []

    async def stalled_poll(**_kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            expired.set()
            raise

    bot.get_updates = stalled_poll
    monkeypatch.setattr(
        "orchestrator.telegram_ingress.TELEGRAM_POLL_WATCHDOG_SECONDS",
        0.01,
        raising=False,
    )
    monkeypatch.setattr("orchestrator.telegram_ingress.TELEGRAM_RETRY_SECONDS", 0.1)
    ingress = CoreTelegramIngress(
        agent_name="alpha",
        token="token",
        handle_lookup=lambda _: object(),
        status_callback=lambda connected: statuses.append(connected),
        bot_factory=lambda _: bot,
    )

    await ingress.start(drop_pending_updates=False)
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert ingress.connected is False
    assert statuses == []
    await asyncio.wait_for(expired.wait(), timeout=1)
    await ingress.stop()
    assert statuses == []


@pytest.mark.asyncio
async def test_core_ingress_waits_at_route_gate_then_uses_committed_worker():
    bot = _Bot("token")
    old_deliveries = []
    new_deliveries = []

    class _Process:
        def __init__(self, pid):
            self.pid = pid

        def is_alive(self):
            return True

    class _Client:
        def __init__(self, pid, deliveries, generation):
            self.agent_name = "alpha"
            self.process = _Process(pid)
            self.deliveries = deliveries
            self.generation = SimpleNamespace(
                manifest=SimpleNamespace(generation_id=generation)
            )

        @property
        def pid(self):
            return self.process.pid

        @property
        def generation_id(self):
            return self.generation.manifest.generation_id

        async def call(self, method, params=None, **_kwargs):
            assert method == "runtime.telegram_update"
            self.deliveries.append(params["update"]["update_id"])
            return True

    kernel = SimpleNamespace(
        global_cfg=SimpleNamespace(),
        skill_manager=object(),
        runtime_fingerprint=SimpleNamespace(runtime_id="core"),
    )
    old = _Client(101, old_deliveries, "sha256:" + "a" * 64)
    new = _Client(202, new_deliveries, "sha256:" + "b" * 64)
    metadata = {
        "name": "alpha",
        "worker_pid": 101,
        "worker_phase": "ACTIVE",
        "worker_accepting": True,
    }
    handle = AgentRuntimeHandle(kernel, old, metadata)
    await handle.begin_cutover()

    ingress = CoreTelegramIngress(
        agent_name="alpha",
        token="token",
        handle_lookup=lambda _name: handle,
        bot_factory=lambda _token: bot,
    )
    await ingress.start(drop_pending_updates=False)
    await asyncio.sleep(0)
    assert old_deliveries == []
    assert new_deliveries == []

    await handle.commit_cutover(
        new,
        {**metadata, "worker_pid": 202},
    )
    for _attempt in range(100):
        if ingress.offset == 8:
            break
        await asyncio.sleep(0.01)

    await ingress.stop()

    assert old_deliveries == []
    assert new_deliveries == [7]
    assert ("delete_webhook", False) in bot.calls


@pytest.mark.asyncio
async def test_shared_handoff_finishes_acceptance_before_preserving_offset():
    bot = _Bot("token")
    entered, finish = asyncio.Event(), asyncio.Event()

    class Handle:
        async def deliver_telegram_update(self, payload):
            entered.set()
            await finish.wait()
            return True

    ingress = CoreTelegramIngress(agent_name="alpha", token="token",
        handle_lookup=lambda _: Handle(), bot_factory=lambda _: bot)
    await ingress.start(drop_pending_updates=False)
    await asyncio.wait_for(entered.wait(), timeout=1)
    pause = asyncio.create_task(ingress.pause())
    await asyncio.sleep(0)
    assert not pause.done()
    assert ingress.offset is None
    finish.set()
    await asyncio.wait_for(pause, timeout=1)
    assert ingress.offset == 8
    assert not ingress.is_running


@pytest.mark.asyncio
async def test_idle_long_poll_does_not_delay_shared_handoff():
    bot = _Bot("token")
    checkpointed = asyncio.Event()

    class Handle:
        async def deliver_telegram_update(self, _payload):
            return True

    ingress = CoreTelegramIngress(
        agent_name="alpha",
        token="token",
        handle_lookup=lambda _: Handle(),
        checkpoint_callback=lambda _offset: checkpointed.set(),
        bot_factory=lambda _: bot,
    )
    await ingress.start(drop_pending_updates=False)
    await asyncio.wait_for(checkpointed.wait(), timeout=1)
    assert ingress.offset == 8
    for _ in range(100):
        if ("get_updates", 8) in bot.calls:
            break
        await asyncio.sleep(0.001)

    await asyncio.wait_for(ingress.pause(), timeout=0.2)

    assert not ingress.is_running
    assert bot.calls[-1] == "shutdown"


@pytest.mark.asyncio
async def test_reboot_gate_does_not_hold_telegram_handoff_open():
    bot = _Bot("token")

    class Handle:
        route_is_gated = True

        async def deliver_telegram_update(self, _payload):
            raise AssertionError("A fenced Worker received a new update")

    ingress = CoreTelegramIngress(
        agent_name="alpha", token="token",
        handle_lookup=lambda _: Handle(), bot_factory=lambda _: bot,
    )
    await ingress.start(drop_pending_updates=False)
    await asyncio.wait_for(ingress.pause(), timeout=0.2)

    assert ingress.offset is None
    assert not ingress.is_running
