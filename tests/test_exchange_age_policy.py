from __future__ import annotations

import asyncio
import copy
import json
import time

import pytest

from orchestrator.exchange_ingress import (
    ExchangeIngressConflict, ExchangeIngressError, ExchangeIngressService,
)
from orchestrator.session_store import SessionStore
from remote.exchange_protocol import utc_timestamp
from tests.test_exchange_ingress import _configure, _delivery, _evidence


def _service(root):
    calls = []

    class Runtime:
        startup_success = True

        async def enqueue_api_text(self, text, **kwargs):
            calls.append((text, kwargs))
            return "request-exchange-age"

    store = SessionStore(root / "state" / "sessions.sqlite3", instance_id="HASHI2")
    service = ExchangeIngressService(
        hashi_root=root, runtime_map=lambda: {"reviewer": Runtime()}, session_store=store,
    )
    return service, store, calls


def _policy(root, value):
    path = root / "agents.json"
    cfg = json.loads(path.read_text(encoding="utf8"))
    cfg["global"]["hchat_request_age_policy"] = value
    path.write_text(json.dumps(cfg), encoding="utf8")


def _old_delivery(now):
    delivery = _delivery(now - 100)
    delivery["authorization_expires_at"] = utc_timestamp(now + 200)
    delivery["expires_at"] = utc_timestamp(now + 200)
    return delivery


def test_exchange_delayed_request_proceeds_once_by_default_with_age(tmp_path):
    _configure(tmp_path)
    now = time.time()
    delivery = _old_delivery(now)
    prompt, evidence = _evidence(tmp_path, delivery)
    service, _store, calls = _service(tmp_path)

    service.accept(evidence=evidence, prompt=prompt)
    scheduled = asyncio.run(service.schedule(evidence=evidence, prompt=prompt))
    assert scheduled.state == "scheduled"
    assert scheduled.confirmation_id is None
    assert len(calls) == 1
    assert calls[0][1]["request_metadata"]["_frontend_message_age"]["reason"] == "stale"

    assert service.accept(evidence=evidence, prompt=prompt).replayed
    assert asyncio.run(service.schedule(evidence=evidence, prompt=prompt)).state == "scheduled"
    assert len(calls) == 1


def test_exchange_confirmation_choice_survives_recovery_before_scheduling(tmp_path):
    _configure(tmp_path)
    _policy(tmp_path, {"mode": "confirm", "max_age_seconds": 60})
    delivery = _old_delivery(time.time())
    prompt, evidence = _evidence(tmp_path, delivery)
    service, _store, calls = _service(tmp_path)
    accepted = service.accept(evidence=evidence, prompt=prompt)
    _policy(tmp_path, {"mode": "observe"})

    recovered = asyncio.run(service.recover_pending())
    assert len(recovered) == 1
    assert recovered[0].state == "pending_confirmation"
    assert recovered[0].inbox_key == accepted.inbox_key
    assert recovered[0].confirmation_id
    assert calls == []

    _policy(tmp_path, {"mode": "observe"})
    assert asyncio.run(service.schedule(evidence=evidence, prompt=prompt)).state == "pending_confirmation"
    assert calls == []


def test_exchange_explicit_expiry_rejects_new_but_reuses_admitted_request(tmp_path, monkeypatch):
    import orchestrator.exchange_ingress as exchange_ingress

    _configure(tmp_path)
    _policy(tmp_path, {"mode": "expire", "max_age_seconds": 60})
    now = time.time()
    service, _store, calls = _service(tmp_path)
    stale = _old_delivery(now)
    prompt, evidence = _evidence(tmp_path, stale)
    with pytest.raises(ExchangeIngressError, match="age policy"):
        service.accept(evidence=evidence, prompt=prompt)
    assert service.inbox.pending() == []

    fresh = _delivery(now - 10)
    fresh["authorization_expires_at"] = utc_timestamp(now + 200)
    fresh["expires_at"] = utc_timestamp(now + 200)
    fresh_prompt, fresh_evidence = _evidence(tmp_path, fresh)
    admitted = service.accept(evidence=fresh_evidence, prompt=fresh_prompt)
    monkeypatch.setattr(exchange_ingress.time, "time", lambda: now + 100)
    fresh_prompt, fresh_evidence = _evidence(tmp_path, fresh)
    retry = service.accept(evidence=fresh_evidence, prompt=fresh_prompt)
    assert retry.replayed and retry.inbox_key == admitted.inbox_key
    scheduled = asyncio.run(service.schedule(evidence=fresh_evidence, prompt=fresh_prompt))
    assert scheduled.state == "scheduled"
    assert len(calls) == 1


def test_exchange_schedule_cannot_swap_payload_of_accepted_message(tmp_path):
    _configure(tmp_path)
    delivery = _delivery()
    prompt, evidence = _evidence(tmp_path, delivery)
    service, _store, calls = _service(tmp_path)
    service.accept(evidence=evidence, prompt=prompt)

    changed = copy.deepcopy(delivery)
    changed["content"]["text"] = "Altered request"
    changed_prompt, changed_evidence = _evidence(tmp_path, changed)
    with pytest.raises(ExchangeIngressConflict):
        asyncio.run(service.schedule(evidence=changed_evidence, prompt=changed_prompt))
    assert calls == []



def test_exchange_legacy_accepted_request_keeps_its_prior_confirmation_behavior(tmp_path):
    _configure(tmp_path)
    delivery = _old_delivery(time.time())
    service, _store, calls = _service(tmp_path)
    # Simulate a row committed by an older generation before age-policy storage.
    accepted = service.inbox.accept(delivery)
    assert accepted.age_policy is None

    recovered = asyncio.run(service.recover_pending())
    assert len(recovered) == 1
    assert recovered[0].state == "pending_confirmation"
    assert calls == []



def test_exchange_http_handlers_report_selected_expiry_before_acceptance(tmp_path):
    from orchestrator.workbench_api import WorkbenchApiServer

    _configure(tmp_path)
    _policy(tmp_path, {"mode": "expire", "max_age_seconds": 60})
    delivery = _old_delivery(time.time())
    prompt, evidence = _evidence(tmp_path, delivery)
    service, _store, calls = _service(tmp_path)
    api = object.__new__(WorkbenchApiServer)
    api._exchange_ingress = service

    async def payload(_request):
        return prompt, evidence

    api._exchange_request_payload = payload
    response = asyncio.run(api.handle_exchange_accept(None))
    assert response.status == 409
    assert json.loads(response.text)["code"] == "MESSAGE_AGE_EXPIRED"
    assert service.inbox.pending() == []
    assert calls == []



@pytest.mark.parametrize("already_scheduled", [False, True])
def test_exchange_signed_expired_retry_only_returns_exact_previously_accepted_receipt(
    tmp_path, monkeypatch, already_scheduled,
):
    import orchestrator.exchange_ingress as exchange_ingress
    from orchestrator.exchange_ingress import ExchangeIngressAuthenticationError

    _configure(tmp_path)
    now = time.time()
    delivery = _delivery(now)
    delivery["authorization_expires_at"] = utc_timestamp(now + 200)
    delivery["expires_at"] = utc_timestamp(now + 200)
    prompt, evidence = _evidence(tmp_path, delivery)
    service, _store, calls = _service(tmp_path)
    admitted = service.accept(evidence=evidence, prompt=prompt)
    if already_scheduled:
        assert asyncio.run(service.schedule(evidence=evidence, prompt=prompt)).state == "scheduled"
    assert len(calls) == int(already_scheduled)

    monkeypatch.setattr(exchange_ingress.time, "time", lambda: now + 400)
    prompt, evidence = _evidence(tmp_path, delivery)
    retry = service.accept(evidence=evidence, prompt=prompt)
    assert retry.replayed and retry.inbox_key == admitted.inbox_key
    result = asyncio.run(service.schedule(evidence=evidence, prompt=prompt))
    assert result.state == "scheduled"
    assert len(calls) == 1

    changed = copy.deepcopy(delivery)
    changed["content"]["text"] = "different request"
    changed_prompt, changed_evidence = _evidence(tmp_path, changed)
    with pytest.raises(ExchangeIngressConflict):
        service.accept(evidence=changed_evidence, prompt=changed_prompt)
    fresh_identity = copy.deepcopy(delivery)
    fresh_identity["message_id"] = "never-admitted"
    new_prompt, new_evidence = _evidence(tmp_path, fresh_identity)
    with pytest.raises(ExchangeIngressError, match="expired"):
        service.accept(evidence=new_evidence, prompt=new_prompt)
    forged = copy.deepcopy(evidence)
    forged["claims"]["delivery"]["content"]["text"] = "forged"
    with pytest.raises(ExchangeIngressAuthenticationError):
        service.accept(evidence=forged, prompt=prompt)
    assert len(calls) == 1
