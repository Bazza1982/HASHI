"""Selected-model inference crosses the broker; keys and alternate targets do not."""
from dataclasses import replace

import pytest

from adapters.openrouter_api import _APIResult
from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.host_provider import BrokeredProviderFactory
from orchestrator.permissions.model_broker import SelectedModelBroker
from orchestrator.permissions.models import RuntimeExecutionBinding
from orchestrator.permissions.pao_run_fence import PaoRunFence

from tests.test_permission_decision_commands import control as control


@pytest.fixture
def environment(control):
    store, _, authority, context, _, request, subject, _, _, _ = control
    binding = authority.get_request_runtime_binding(subject, request.request_id)
    guard = PaoRunFence(store, context=context, request_id=binding.request_id,
                        fencing_token=binding.fencing_token, worker_id=binding.worker_id)
    return authority, context, binding, guard


@pytest.mark.asyncio
async def test_actual_api_adapter_uses_broker_and_never_receives_service_key(environment, tmp_path):
    authority, context, binding, guard = environment
    calls = []

    class Driver:
        def _request_headers(self):
            return {"Authorization": "Bearer service-only-canary"}

        async def _call_api_once(self, payload, headers, callback):
            calls.append((payload, headers))
            return _APIResult("Brokered answer", None, "stop", prompt_tokens=12, completion_tokens=3)

    broker = SelectedModelBroker(authority, binding, run_guard=guard,
                                 drivers={("deepseek-api", "deepseek-v4-pro"): Driver()})

    class Connection:
        async def call_async(self, method, arguments, *, on_event=None):
            assert method == "provider_call"
            return await broker.provider_call(**arguments)

    factory = BrokeredProviderFactory(Connection(), profile_config=[
        {"engine": "deepseek-api", "model": "deepseek-v4-pro"}], scratch_root=tmp_path, privacy_level=1)
    adapter = factory.create_ephemeral_backend("deepseek-api", "deepseek-v4-pro")
    try:
        assert await adapter.initialize()
        response = await adapter.generate_response("Summarise the report", "request")
        assert response.is_success, response.error
        assert response.text == "Brokered answer"
        assert len(calls) == 1
        assert calls[0][1]["Authorization"] == "Bearer service-only-canary"
        assert adapter.api_key != "service-only-canary"
        assert "service-only-canary" not in repr(response)
    finally:
        await adapter.shutdown()


@pytest.mark.asyncio
async def test_model_broker_rejects_other_destinations_remote_tools_and_inactive_epochs(environment):
    authority, context, binding, guard = environment

    class Driver:
        def _request_headers(self):
            pytest.fail("invalid call reached credential use")

    broker = SelectedModelBroker(authority, binding, run_guard=guard, drivers={("deepseek-api", "model"): Driver()})
    base = {"engine": "deepseek-api", "model": "model", "use_stream": False,
            "payload": {"model": "model", "messages": []}}
    for arguments in (
        {**base, "engine": "other-api"},
        {**base, "payload": {**base["payload"], "plugins": [{"id": "web"}]}},
        {**base, "payload": {**base["payload"], "tools": [{"type": "mcp", "server_url": "http://admin"}]}},
        {**base, "payload": {**base["payload"], "model": "other-model"}},
    ):
        with pytest.raises(PermissionDenied):
            await broker.provider_call(**arguments)
    authority.fence_execution(context)
    with pytest.raises(PermissionDenied):
        await broker.provider_call(**base)
    with pytest.raises(ValueError):
        RuntimeExecutionBinding(replace(context, owner_id=""), "request", 1, "worker")
