"""Durable model observations are billing evidence, never transport retries."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from adapters.openrouter_api import _APIResult
from orchestrator.permissions.model_broker import SelectedModelBroker
from orchestrator.permissions.model_receipts import ModelReceiptJournal
from orchestrator.permissions.models import RuntimeExecutionBinding
from orchestrator.permissions.pao_run_fence import PaoRunFence
from tests.test_permission_pao_fence import setup_run


@pytest.mark.parametrize("lost_response", [False, True])
@pytest.mark.asyncio
async def test_intent_precedes_transport_and_restart_never_replays(tmp_path, lost_response):
    store, _, accepted, fence, context = setup_run(tmp_path)
    binding = RuntimeExecutionBinding(context, accepted.request_id, fence, "INSTANCE:agent")
    guard = PaoRunFence(store, context=context, request_id=accepted.request_id,
                        fencing_token=fence, worker_id="INSTANCE:agent")
    path = tmp_path / "observations.sqlite"
    journal = ModelReceiptJournal(path)
    calls = []

    class Driver:
        def _request_headers(self):
            return {"Authorization": "service-secret-never-persisted"}

        async def _call_api_once(self, payload, headers, callback):
            calls.append(1)
            assert ModelReceiptJournal(path).observations(binding)[0]["status"] == "unknown"
            if lost_response:
                raise OSError("response lost")
            return _APIResult("private-response-never-persisted", None, "stop",
                              prompt_tokens=12, completion_tokens=3, cost_usd=.05,
                              provider_response_id="provider-observation")

    authority = SimpleNamespace(execution_status=lambda _: SimpleNamespace(task_state="active", binding_state="active"))
    broker = SelectedModelBroker(authority, binding, run_guard=guard,
                                 drivers={("deepseek-api", "model"): Driver()}, receipt_store=journal)
    arguments = dict(engine="deepseek-api", model="model", payload={
        "model": "model", "messages": [{"role": "user", "content": "private-request-never-persisted"}],
    }, use_stream=False)
    if lost_response:
        with pytest.raises(OSError):
            await broker.provider_call(**arguments)
    else:
        await broker.provider_call(**arguments)
    # A newly adopted epoch sees the same physical receipt. No driver is
    # consulted by recovery, and unrelated turns cannot inherit its costs.
    renewed = replace(binding, context=replace(context, execution_epoch="new-epoch"))
    observed = ModelReceiptJournal(path).observations(renewed)
    assert len(calls) == 1 and len(observed) == 1
    assert observed[0]["status"] == ("unknown" if lost_response else "completed")
    assert observed[0]["cost_usd"] == (None if lost_response else .05)
    assert not journal.observations(replace(binding, context=replace(context, turn_id="other-turn")))
    disk = path.read_bytes()
    for secret in (b"service-secret-never-persisted", b"private-response-never-persisted",
                   b"private-request-never-persisted"):
        assert secret not in disk


def test_projection_is_durable_before_observer_and_replayed_without_new_labels(tmp_path):
    from copy import deepcopy
    from orchestrator.her_v2.interfaces import StageInvocationError
    from orchestrator.permissions.host_usage import accept_host_usage
    from tests.test_permission_installed_boundaries import receiver, usage_response

    _, _, accepted, fence, context = setup_run(tmp_path)
    binding = RuntimeExecutionBinding(context, accepted.request_id, fence, "INSTANCE:agent")
    path = tmp_path / "receipts.sqlite"
    journal = ModelReceiptJournal(path)
    response = usage_response()
    response["usage"][0]["phase"] = "review"
    completed = deepcopy(response["provider_evidence"][0])
    journal.reserve(binding, {**completed, "status": "unknown"})
    journal.complete(binding, completed)
    first = receiver()
    first._permission_projection_commit = lambda rows: journal.commit_projections(binding, rows)
    def unavailable(_):
        raise OSError("the canonical accounting observer stopped")
    first.usage_observer = unavailable
    with pytest.raises(StageInvocationError):
        accept_host_usage(first, response)
    renewed = replace(binding, context=replace(context, execution_epoch="new-epoch"))
    response["accounting_epoch"] = "new-epoch"
    response["usage"] = []
    response["provider_evidence"] = ModelReceiptJournal(path).observations(renewed)
    second = receiver()
    second._permission_projection_commit = lambda rows: journal.commit_projections(renewed, rows)
    accept_host_usage(second, response)
    assert second.usage_line_items[0].phase == "review"
    assert second.usage_line_items[0].request_id == "permission-provider:service-call"
    assert second.usage.input_tokens == 23 and second.cost_usd == .125
