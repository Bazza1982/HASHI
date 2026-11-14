import asyncio
import json
import threading
from types import SimpleNamespace

from orchestrator.exchange_ingress import ExchangeIngressService
from orchestrator.session_store import SessionStore
from orchestrator.workbench_api import WorkbenchApiServer
from tests.test_exchange_ingress import _configure, _delivery, _evidence


def test_http_accept_offloads_the_complete_durable_acceptance_transaction(tmp_path, monkeypatch):
    _configure(tmp_path)
    prompt, evidence = _evidence(tmp_path, _delivery())
    store = SessionStore(tmp_path / 'state' / 'sessions.sqlite3', instance_id='HASHI2')
    service = ExchangeIngressService(hashi_root=tmp_path,
        runtime_map=lambda: {'reviewer': SimpleNamespace(startup_success=True)}, session_store=store)
    original = service.accept
    request_thread = threading.get_ident()
    workers = []
    def observe(**kwargs):
        workers.append(threading.get_ident())
        return original(**kwargs)
    monkeypatch.setattr(service, 'accept', observe)
    api = object.__new__(WorkbenchApiServer)
    api._exchange_ingress = service
    async def payload(_request): return prompt, evidence
    api._exchange_request_payload = payload
    response = asyncio.run(api.handle_exchange_accept(None))
    assert response.status == 200
    body = json.loads(response.text)
    assert body['state'] == 'accepted'
    assert service.inbox.get(body['inbox_key']).state == 'accepted'
    assert workers and workers[0] != request_thread


def test_cancelled_factory_waiter_does_not_start_a_second_sqlite_owner():
    api = object.__new__(WorkbenchApiServer)
    api._exchange_ingress = None
    entered, release = threading.Event(), threading.Event()
    marker = object()
    calls = []
    def factory():
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(2)
        api._exchange_ingress = marker
        return marker
    api._exchange_ingress_service = factory
    async def exercise():
        first = asyncio.create_task(api._get_exchange_ingress_service())
        for _ in range(100):
            if entered.is_set(): break
            await asyncio.sleep(.005)
        assert entered.is_set()
        first.cancel()
        second = asyncio.create_task(api._get_exchange_ingress_service())
        release.set()
        try: await first
        except asyncio.CancelledError: pass
        assert await second is marker
        assert await api._get_exchange_ingress_service() is marker
        assert len(calls) == 1
    try: asyncio.run(exercise())
    finally: release.set()
# HASHI-Fingerprint: hfp_d785cc8b726c45e39446901b16e1b70d
