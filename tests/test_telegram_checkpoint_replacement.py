"""Transport replacement and durable recovery retain only the same Bot's ACKs."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
import pytest
from orchestrator.function_worker_supervisor import FunctionWorkerSupervisor
from orchestrator import runtime_handoff


@pytest.mark.asyncio
async def test_same_bot_transport_replacement_retains_pending_ack(tmp_path, monkeypatch):
    previous = SimpleNamespace(token="123:fixture", offset=7, acknowledged_offset=8,
        api_root="old endpoint", is_running=True, stop=AsyncMock())
    class Replacement:
        def __init__(self, **kwargs): self.offset = None
        async def start(self, **kwargs): pass
    monkeypatch.setattr("orchestrator.function_worker_supervisor.CoreTelegramIngress", Replacement)
    supervisor = object.__new__(FunctionWorkerSupervisor)
    supervisor._telegram_ingress = {"one":previous}
    supervisor._telegram_ingress_diagnostics = {}
    supervisor.kernel = SimpleNamespace(_runtime_map=lambda:{}, paths=SimpleNamespace(bridge_home=tmp_path),
        global_cfg=SimpleNamespace(instance_id="HASHI3"), _shared_committed=False)
    assert await supervisor._start_telegram_ingress("one", "123:fixture", drop_pending_updates=True)
    assert supervisor._telegram_ingress["one"].offset == 8
    assert supervisor._telegram_ingress["one"].drop_pending_on_start is False
    previous.stop.assert_awaited_once()


def test_different_bot_offsets_are_not_combined(tmp_path):
    folder=tmp_path/"state/instance";folder.mkdir(parents=True)
    handoff={"telegram_offsets":{"one":100},
        "telegram_bot_offsets":{"one":{"offset":100,"bot_fingerprint":"old-bot"}}}
    (folder/"function-topology.json").write_text(json.dumps(
        {"schema":1,"shared_generation_id":"g","handoff":handoff}))
    (folder/"telegram-offsets.json").write_text('{"one":2}')
    (folder/"telegram-bot-offsets.json").write_text(json.dumps(
        {"one":{"offset":2,"bot_fingerprint":"new-bot"}}))
    app=SimpleNamespace(shared_generation_id="g",paths=SimpleNamespace(bridge_home=tmp_path))
    recovered=runtime_handoff.load(app)
    assert recovered["telegram_bot_offsets"]["one"] == {"offset":2,"bot_fingerprint":"new-bot"}
    assert recovered["telegram_offsets"]["one"] == 2
# HASHI-Fingerprint: hfp_e5350c1156614bbeaf2f4c515c6c3f24
