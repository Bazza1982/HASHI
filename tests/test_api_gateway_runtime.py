from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.api_gateway import APIGatewayServer, _AdapterPool


class _FakeAdapter:
    async def generate_response(self, prompt, request_id, is_retry=False, silent=True, on_stream_event=None):
        return SimpleNamespace(is_success=True, text=f"reply:{prompt}", error=None)


class _FakePool:
    def __init__(self):
        self._adapters = {}
        self.calls = []

    async def get(self, engine, model):
        self.calls.append((engine, model))
        self._adapters[engine] = True
        return _FakeAdapter()

    async def update_model(self, engine, model):
        self.calls.append(("update", engine, model))

    async def shutdown(self):
        return None


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.mark.asyncio
async def test_api_gateway_uses_default_model_when_request_omits_model(tmp_path: Path):
    cfg = SimpleNamespace(api_gateway_port=18801, api_host="127.0.0.1", project_root=tmp_path)
    server = APIGatewayServer(cfg, secrets={}, workspace_root=tmp_path, default_model="gpt-5.5")
    server._pool = _FakePool()

    response = await server.handle_chat_completions(
        _FakeRequest({"messages": [{"role": "user", "content": "hello"}]})
    )

    payload = json.loads(response.text)
    assert payload["model"] == "gpt-5.5"
    assert server._pool.calls[0] == ("codex-cli", "gpt-5.5")


@pytest.mark.asyncio
async def test_api_gateway_health_reports_default_model(tmp_path: Path):
    cfg = SimpleNamespace(api_gateway_port=18801, api_host="127.0.0.1", project_root=tmp_path)
    server = APIGatewayServer(cfg, secrets={}, workspace_root=tmp_path, default_model="claude-sonnet-4-6")
    response = await server.handle_health(_FakeRequest({}))

    payload = json.loads(response.text)
    assert payload["default_model"] == "claude-sonnet-4-6"
    assert payload["port"] == 18801


@pytest.mark.asyncio
async def test_api_gateway_pool_isolates_models_and_request_locks(tmp_path: Path):
    cfg = SimpleNamespace(project_root=tmp_path)
    pool = _AdapterPool(cfg, secrets={}, workspace_root=tmp_path)
    created = []

    class _Adapter:
        def __init__(self, model):
            self.config = SimpleNamespace(model=model)
            self.shutdown_called = False

        async def shutdown(self):
            self.shutdown_called = True

    async def create(engine, model):
        adapter = _Adapter(model)
        created.append((engine, model, adapter))
        return adapter

    pool._create = create

    first = await pool.get("codex-cli", "gpt-one")
    same = await pool.get("codex-cli", "gpt-one")
    second = await pool.get("codex-cli", "gpt-two")

    assert first is same
    assert first is not second
    assert first.config.model == "gpt-one"
    assert second.config.model == "gpt-two"
    assert len(created) == 2
    first_lock = pool.request_lock("codex-cli", "gpt-one")
    same_lock = pool.request_lock("codex-cli", "gpt-one")
    second_lock = pool.request_lock("codex-cli", "gpt-two")
    assert first_lock is same_lock
    assert first_lock is not second_lock

    await pool.shutdown()
    assert first.shutdown_called is True
    assert second.shutdown_called is True


@pytest.mark.asyncio
async def test_same_model_adapter_leases_overlap_and_bound_third_request(tmp_path):
    import asyncio

    pool = _AdapterPool(SimpleNamespace(project_root=tmp_path), secrets={}, workspace_root=tmp_path)
    created = []
    async def create(engine, model):
        adapter = SimpleNamespace(config=SimpleNamespace(model=model))
        async def shutdown():
            adapter.closed = True
        adapter.shutdown = shutdown
        created.append(adapter)
        return adapter
    pool._create = create
    entered = [asyncio.Event() for _ in range(3)]
    release = [asyncio.Event() for _ in range(3)]
    observed = []
    async def invoke(index):
        async with pool.lease("codex-cli", "same-model") as adapter:
            observed.append((index, adapter))
            entered[index].set()
            await release[index].wait()
    tasks = []
    try:
        tasks.append(asyncio.create_task(invoke(0)))
        await asyncio.wait_for(entered[0].wait(), 1)
        tasks.append(asyncio.create_task(invoke(1)))
        await asyncio.wait_for(entered[1].wait(), 1)
        assert observed[0][1] is not observed[1][1]
        tasks.append(asyncio.create_task(invoke(2)))
        await asyncio.sleep(0)
        assert not entered[2].is_set()
        tasks[1].cancel()
        await asyncio.gather(tasks[1], return_exceptions=True)
        await asyncio.wait_for(entered[2].wait(), 1)
        assert observed[2][1] is observed[1][1]
        assert not tasks[0].done()
        assert len(created) == 2
    finally:
        for event in release:
            event.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await pool.shutdown()
    assert all(adapter.closed for adapter in created)


def test_gateway_request_context_uses_typed_aiohttp_keys_when_available():
    from orchestrator import api_gateway

    request_key_factory = getattr(api_gateway.web, "RequestKey", None)
    if request_key_factory is None:
        assert api_gateway._GATEWAY_REQUEST_CONTEXT_KEYS == {}
    else:
        assert set(api_gateway._GATEWAY_REQUEST_CONTEXT_KEYS) == {
            api_gateway._GATEWAY_REQUEST_ID_FIELD,
            api_gateway._GATEWAY_VALIDATION_STAGE_FIELD,
        }


@pytest.mark.asyncio
async def test_gateway_engine_and_total_quotas_bound_different_models_without_losing_cancelled_slots(tmp_path):
    import asyncio
    pool = _AdapterPool(SimpleNamespace(project_root=tmp_path), {}, tmp_path,
                        capacity=2, total_capacity=2, engine_capacity=1)
    async def create(engine, model):
        return SimpleNamespace(config=SimpleNamespace(model=model))
    pool._create = create
    entered = {key:asyncio.Event() for key in ('A','B','C','D')}
    release = {key:asyncio.Event() for key in entered}
    async def call(key, engine):
        async with pool.lease(engine, key):
            entered[key].set()
            await release[key].wait()
    tasks = []
    try:
        tasks.append(asyncio.create_task(call('A','codex-cli')))
        await asyncio.wait_for(entered['A'].wait(),1)
        tasks.append(asyncio.create_task(call('B','codex-cli')))
        tasks.append(asyncio.create_task(call('C','other')))
        await asyncio.wait_for(entered['C'].wait(),1)
        tasks.append(asyncio.create_task(call('D','third')))
        await asyncio.sleep(.05)
        assert not entered['B'].is_set() and not entered['D'].is_set()
        tasks[1].cancel()
        await asyncio.gather(tasks[1],return_exceptions=True)
        release['C'].set()
        await asyncio.wait_for(entered['D'].wait(),1)
        assert not tasks[0].done()
    finally:
        for event in release.values():event.set()
        await asyncio.gather(*tasks,return_exceptions=True)
    assert not pool._busy
