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


@pytest.mark.asyncio
@pytest.mark.parametrize("exc,stage", [
    ("invalid_token", "get_updates"), ("forbidden", "get_updates"),
    ("conflict", "get_updates"), ("network", "get_updates"),
    ("timeout", "get_updates"), ("watchdog", "watchdog"),
    ("worker", "worker_delivery"), ("status", "status_propagation"),
])
async def test_poller_failures_reach_real_private_sink_and_safe_projection(tmp_path, monkeypatch, exc, stage):
    import json
    from telegram.error import InvalidToken, Forbidden, Conflict, NetworkError, TimedOut
    token = "123456789:" + "SensitiveTokenString_" * 2
    secret = f"https://api.telegram.org/bot{token}/getUpdates chat_id=928347 text=private-message Authorization: Bearer secret-credential"
    errors = {"invalid_token":InvalidToken, "forbidden":Forbidden, "conflict":Conflict, "network":NetworkError, "timeout":TimedOut, "worker":RuntimeError, "status":RuntimeError}
    bot = _Bot(token)
    failure_seen = asyncio.Event()
    async def poll(**_kwargs):
        if exc == "watchdog":
            await asyncio.Event().wait()
        if exc in {"worker", "status"}: return [bot.update]
        raise errors[exc](secret)
    bot.get_updates = poll
    class Handle:
        async def deliver_telegram_update(self, _payload):
            if exc == "worker": raise RuntimeError(secret)
            return True
    async def status(connected):
        if exc == "status" and connected: raise RuntimeError(secret)
    monkeypatch.setattr("orchestrator.telegram_ingress.TELEGRAM_POLL_WATCHDOG_SECONDS", .005)
    monkeypatch.setattr("orchestrator.telegram_ingress.TELEGRAM_RETRY_SECONDS", .02)
    ingress = CoreTelegramIngress(agent_name="alpha", token=token,
        handle_lookup=lambda _: Handle(), status_callback=status,
        bot_factory=lambda _: bot, diagnostic_home=tmp_path,
        instance_id="HASHI1", generation_id="sha256:"+"a"*64)
    await ingress.start(drop_pending_updates=False)
    for _ in range(100):
        if ingress.diagnostic_snapshot()["last_failure"]: break
        await asyncio.sleep(.002)
    snapshot = ingress.diagnostic_snapshot()
    await ingress.stop()
    records = ingress.read_diagnostics(limit=20)["records"]
    assert snapshot["last_failure"]["stage"] == stage
    assert snapshot["last_failure"]["operation_id"].startswith("tg-poll-")
    assert any(item["event"] == "failure" and item["stage"] == stage for item in records)
    raw = json.dumps(records) + json.dumps(snapshot) + "".join(p.read_text() for p in (tmp_path/"logs"/"telegram-ingress").glob("*.jsonl*"))
    for withheld in (token, "928347", "private-message", "secret-credential"):
        assert withheld not in raw
    if exc == "worker": assert ingress.offset is None
    assert snapshot["instance_id"] == "HASHI1"
    assert snapshot["generation_id"] == "sha256:"+"a"*64


@pytest.mark.asyncio
async def test_repeated_worker_error_is_bounded_then_recovery_preserves_facts(tmp_path, monkeypatch):
    bot = _Bot("private-token")
    delivered = asyncio.Event()
    count = 0
    class Handle:
        async def deliver_telegram_update(self, _payload):
            nonlocal count
            count += 1
            if count <= 10: raise RuntimeError("secret user body")
            delivered.set()
            return True
    monkeypatch.setattr("orchestrator.telegram_ingress.TELEGRAM_RETRY_SECONDS", .001)
    ingress = CoreTelegramIngress(agent_name="alpha", token="private-token",
        handle_lookup=lambda _: Handle(), bot_factory=lambda _:bot,
        diagnostic_home=tmp_path, instance_id="HASHI1", generation_id="gen-a")
    await ingress.start(drop_pending_updates=False)
    await asyncio.wait_for(delivered.wait(), 1)
    await ingress.stop()
    payload = ingress.read_diagnostics()
    failures = [r for r in payload["records"] if r["event"] == "failure"]
    recovered = [r for r in payload["records"] if r["event"] == "recovered"]
    assert len(failures) == 1
    assert recovered[-1]["consecutive_failures"] == 10
    assert payload["summary"]["last_failure"]["consecutive_failures"] == 10
    assert payload["summary"]["last_success_at"] is not None
    assert payload["summary"]["consecutive_failures"] == 0
    assert ingress.offset == 8


def test_diagnostics_rotate_bound_filter_agents_generations_and_reject_extensions(tmp_path, monkeypatch):
    import json
    from orchestrator.telegram_ingress_diagnostics import TelegramIngressDiagnostics
    monkeypatch.setattr("orchestrator.telegram_ingress_diagnostics.MAX_BYTES", 2000)
    monkeypatch.setattr("orchestrator.telegram_ingress_diagnostics.SUMMARY_INTERVAL_SECONDS", 0)
    def make(agent, generation):
        return TelegramIngressDiagnostics(bridge_home=tmp_path, instance_id="HASHI1",
            agent=agent, generation_id=generation, token="safe-token")
    alpha = make("alpha", "gen-a")
    alpha.begin("get_updates")
    alpha.failure(RuntimeError("secret-a"), stage="get_updates", retry_seconds=2)
    beta = make("beta", "gen-b")
    beta.begin("get_updates")
    beta.failure(RuntimeError("secret-b"), stage="get_updates", retry_seconds=2)
    alpha2 = make("alpha", "gen-b")
    alpha2.begin("get_updates")
    alpha2.failure(RuntimeError("secret-c"), stage="get_updates", retry_seconds=2)
    records = alpha2.read()["records"]
    assert {r["generation_id"] for r in records} == {"gen-a", "gen-b"}
    assert {r["agent"] for r in records} == {"alpha"}
    path = tmp_path / alpha.relative_path
    with path.open("a") as stream:
        stream.write(json.dumps({**alpha2.identity,"event":"failure","stage":"get_updates","at":alpha2.snapshot()["last_failure_at"],
            "error":{"message":"hidden update body", "reason":"unknown"}, "private":"secret payload"})+"\n")
    assert "hidden update body" not in json.dumps(alpha2.read())
    assert "secret payload" not in json.dumps(alpha2.read())
    for _ in range(20): alpha2.failure(RuntimeError("secret"), stage="get_updates", retry_seconds=2)
    files = list(path.parent.glob(path.name+"*"))
    assert len(files) <= 4
    assert all(f.stat().st_size <= 2000 for f in files)
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_real_poller_sink_failure_never_replays_delivery_or_reports_fake_recovery(tmp_path, monkeypatch):
    # A file where the private log directory belongs forces a real I/O failure.
    (tmp_path/"logs").write_text("blocked")
    bot = _Bot("token")
    seen = asyncio.Event()
    calls = 0
    class Handle:
        async def deliver_telegram_update(self, _payload):
            nonlocal calls
            calls += 1
            seen.set()
            raise RuntimeError("private update")
    monkeypatch.setattr("orchestrator.telegram_ingress.TELEGRAM_RETRY_SECONDS", .2)
    ingress = CoreTelegramIngress(agent_name="alpha",token="token",
        handle_lookup=lambda _:Handle(),bot_factory=lambda _:bot,
        diagnostic_home=tmp_path,instance_id="HASHI1",generation_id="gen-a")
    await ingress.start(drop_pending_updates=False)
    await asyncio.wait_for(seen.wait(),1)
    await asyncio.sleep(.001)
    snapshot=ingress.diagnostic_snapshot()
    assert snapshot["sink"]["last_error"] == "NotADirectoryError"
    assert snapshot["last_failure"]["stage"] == "worker_delivery"
    assert ingress.offset is None
    await ingress.stop()
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage",["bot_initialization","webhook_initialization"])
async def test_failed_initialization_has_private_durable_diagnosis(tmp_path,stage):
    from orchestrator.telegram_ingress import TelegramIngressInitializationError
    from telegram.error import InvalidToken
    bot=_Bot("token")
    async def fail(**_kwargs): raise InvalidToken("private credential text")
    if stage=="bot_initialization": bot.initialize=fail
    else: bot.delete_webhook=fail
    ingress=CoreTelegramIngress(agent_name="alpha",token="token",handle_lookup=lambda _:None,
        bot_factory=lambda _:bot,diagnostic_home=tmp_path,instance_id="HASHI1",generation_id="gen-a")
    with pytest.raises(TelegramIngressInitializationError) as outcome:
        await ingress.start(drop_pending_updates=False)
    assert "private" not in str(outcome.value)
    assert ingress.read_diagnostics()["records"][-1]["stage"]==stage
    assert ingress.task is None


def test_telegram_diagnostic_retention_is_bounded_across_process_generations(tmp_path,monkeypatch):
    import os,time
    from orchestrator.telegram_ingress_diagnostics import TelegramIngressDiagnostics,RETENTION_SECONDS
    monkeypatch.setattr("orchestrator.telegram_ingress_diagnostics._MAINTENANCE_INTERVAL_SECONDS",0)
    def make(gen):
        item=TelegramIngressDiagnostics(bridge_home=tmp_path,instance_id="HASHI1",agent="alpha",generation_id=gen,token="token")
        item.begin("get_updates")
        item.failure(RuntimeError("private message"),stage="get_updates",retry_seconds=2)
        return item
    old=make("old-generation")
    old_path=tmp_path/old.relative_path
    expired=time.time()-RETENTION_SECONDS-1
    os.utime(old_path,(expired,expired))
    current=make("new-generation")
    assert not old_path.exists()
    assert {r["generation_id"] for r in current.read()["records"]}=={"new-generation"}
    for index in range(15): current=make("gen-"+str(index))
    files=current._streams((tmp_path/current.relative_path).parent)
    assert len(files)<=8
    assert all(p.stat().st_mode & 0o777==0o600 for p in files)


def test_recovered_quiet_poller_expires_owned_logs_without_another_failure(tmp_path,monkeypatch):
    import os,time
    from orchestrator.telegram_ingress_diagnostics import TelegramIngressDiagnostics,RETENTION_SECONDS
    monkeypatch.setattr("orchestrator.telegram_ingress_diagnostics._MAINTENANCE_INTERVAL_SECONDS",0)
    diagnostic=TelegramIngressDiagnostics(bridge_home=tmp_path,instance_id="HASHI1",agent="alpha",generation_id="gen-a",token="token")
    diagnostic.begin("get_updates")
    diagnostic.failure(RuntimeError("private"),stage="get_updates",retry_seconds=2)
    path=tmp_path/diagnostic.relative_path
    expired=time.time()-RETENTION_SECONDS-1
    os.utime(path,(expired,expired))
    diagnostic.poll_succeeded()
    diagnostic.recovered()
    assert all(r["event"]!="failure" for r in diagnostic.read()["records"])
    assert diagnostic.snapshot()["last_success_at"]
