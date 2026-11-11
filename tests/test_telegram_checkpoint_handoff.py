"""Acknowledged Telegram work survives failed checkpoints and shared handoff."""
import asyncio
import json
from types import SimpleNamespace
import pytest
from orchestrator.telegram_ingress import CoreTelegramIngress
from orchestrator import runtime_handoff
from orchestrator.telegram_delivery_state import telegram_bot_fingerprint
from tests.test_telegram_ingress import _Bot


@pytest.mark.asyncio
async def test_failed_checkpoint_is_retained_in_handoff_and_not_regressed_on_reload(tmp_path, monkeypatch):
    monkeypatch.setattr("orchestrator.telegram_ingress.TELEGRAM_RETRY_SECONDS", .001)
    failed = asyncio.Event()
    delivered = []
    async def checkpoint(offset):
        failed.set()
        raise PermissionError("checkpoint unavailable")
    async def deliver(update):
        delivered.append(update["update_id"])
        return True
    ingress = CoreTelegramIngress(agent_name="one", token="token",
        handle_lookup=lambda _: SimpleNamespace(deliver_telegram_update=deliver),
        bot_factory=_Bot, checkpoint_callback=checkpoint)
    try:
        await ingress.start(drop_pending_updates=False)
        await asyncio.wait_for(failed.wait(), 1)
        await asyncio.wait_for(ingress.pause(), 1)
        app = SimpleNamespace(runtimes=[], paths=SimpleNamespace(bridge_home=tmp_path),
            shared_generation_id="g", function_workers=SimpleNamespace(_telegram_ingress={"one": ingress}))
        handoff = runtime_handoff.snapshot(app)
        assert handoff["telegram_offsets"]["one"] == 8
        assert delivered == [7]
        folder = tmp_path/"state/instance"; folder.mkdir(parents=True)
        (folder/"function-topology.json").write_text(json.dumps(
            {"schema":1, "shared_generation_id":"g", "handoff":handoff}))
        (folder/"telegram-offsets.json").write_text('{"one": 7}')
        (folder/"telegram-bot-offsets.json").write_text(json.dumps(
            {"one":{"offset":7,"bot_fingerprint":telegram_bot_fingerprint("token")}}))
        recovered = runtime_handoff.load(app)
        assert recovered["telegram_bot_offsets"]["one"]["offset"] == 8
        assert recovered["telegram_offsets"]["one"] == 8
    finally:
        await ingress.stop()


@pytest.mark.asyncio
async def test_handoff_does_not_wait_for_offline_bot_initialization():
    entered = asyncio.Event()
    class OfflineBot(_Bot):
        async def initialize(self):
            entered.set()
            await asyncio.Event().wait()
    ingress = CoreTelegramIngress(agent_name="one", token="token",
        handle_lookup=lambda _: None, bot_factory=OfflineBot)
    try:
        await ingress.start(drop_pending_updates=False)
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(ingress.pause(), .2)
        assert not ingress.is_running
    finally:
        await ingress.stop()
