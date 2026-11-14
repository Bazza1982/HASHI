import asyncio
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from orchestrator.exchange_ingress import ExchangeIngressService


def test_locked_exchange_maintenance_keeps_api_event_loop_responsive(tmp_path):
    service = ExchangeIngressService(hashi_root=tmp_path, runtime_map=lambda: {},
        session_store=SimpleNamespace(db_path=tmp_path / 'sessions.sqlite3'))
    ready = threading.Event()
    def writer():
        with sqlite3.connect(service.inbox.path) as connection:
            connection.execute('BEGIN IMMEDIATE')
            ready.set()
            time.sleep(0.4)
    thread = threading.Thread(target=writer)
    thread.start()
    assert ready.wait(2)
    async def exercise():
        beats = []
        async def heartbeat():
            for _ in range(20):
                beats.append(time.monotonic())
                await asyncio.sleep(0.025)
        beating = asyncio.create_task(heartbeat())
        await asyncio.sleep(0)
        result = await asyncio.gather(service.recover_pending(), return_exceptions=True)
        await beating
        assert len(beats) == 20
        assert max(b-a for a,b in zip(beats, beats[1:])) < 0.2
        assert not isinstance(result[0], BaseException) or isinstance(result[0], sqlite3.OperationalError)
    try:
        asyncio.run(exercise())
    finally:
        thread.join(2)


def test_inbox_closes_sqlite_connection_at_transaction_boundary(tmp_path):
    service = ExchangeIngressService(hashi_root=tmp_path, runtime_map=lambda: {},
        session_store=SimpleNamespace(db_path=tmp_path / 'sessions.sqlite3'))
    with service.inbox._connection() as connection:
        connection.execute('SELECT 1')
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connection.execute('SELECT 1')
# HASHI-Fingerprint: hfp_8a5b16fb69a6486386a78a8dec5a7e53


def test_cancelled_maintenance_drains_its_worker_before_the_next_recovery(tmp_path, monkeypatch):
    service = ExchangeIngressService(hashi_root=tmp_path, runtime_map=lambda: {},
        session_store=SimpleNamespace(db_path=tmp_path / 'sessions.sqlite3'))
    entered, release = threading.Event(), threading.Event()
    calls = []
    original = service.inbox.purge
    def held():
        calls.append('start')
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
        try:
            return original()
        finally:
            calls.append('end')
    monkeypatch.setattr(service.inbox, 'purge', held)
    async def exercise():
        first = asyncio.create_task(service.recover_pending())
        for _ in range(100):
            if entered.is_set(): break
            await asyncio.sleep(.005)
        assert entered.is_set()
        first.cancel()
        second = asyncio.create_task(service.recover_pending())
        first.cancel()
        await asyncio.sleep(.02)
        assert calls == ['start']
        release.set()
        with pytest.raises(asyncio.CancelledError): await first
        await second
        assert calls == ['start','end','start','end']
    try:
        asyncio.run(exercise())
    finally:
        release.set()
