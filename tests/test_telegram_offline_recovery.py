"""Offline connectors preserve local availability and durable delivery ownership."""
from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from orchestrator.function_worker_host import FunctionWorkerStateError
from orchestrator.function_worker_supervisor import FunctionWorkerSupervisor
from orchestrator.session_store import SessionStore
from orchestrator.telegram_ingress import CoreTelegramIngress
from orchestrator.telegram_update_ack import dispatch_update, record_handler_error
from tests.test_local_ready_without_telegram import ready_host
from tests.test_telegram_ingress import _Bot


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("worker_nonce","retired"),("generation_id","retired")])
async def test_committed_connector_capability_rejects_retired_identity(tmp_path,field,value):
    host=ready_host(tmp_path)
    await host.activate()
    params={"worker_nonce":host.nonce,"generation_id":host.manifest.generation_id,field:value}
    try:
        with pytest.raises(FunctionWorkerStateError):
            await host.handle_request("worker.connectors_start",params)
        assert host.connector_task is None
    finally:
        host.runtime.process_task.cancel()
        await asyncio.gather(host.runtime.process_task,return_exceptions=True)


@pytest.mark.asyncio
async def test_quiesce_cancels_owned_connector_and_resume_retries(tmp_path):
    host=ready_host(tmp_path)
    host.runtime.queue=asyncio.Queue()
    host.runtime.is_generating=False
    host.runtime._background_tasks=set()
    entered=asyncio.Event()
    cancelled=asyncio.Event()
    attempts=[]
    async def connect():
        attempts.append(len(attempts))
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    host._prepare_telegram_application=connect
    await host.activate()
    try:
        await host.handle_request("worker.connectors_start",
            {"worker_nonce":host.nonce,"generation_id":host.manifest.generation_id})
        await asyncio.wait_for(entered.wait(),1)
        await host.quiesce(0.2)
        assert cancelled.is_set() and host.connector_task is None
        assert not host.runtime.telegram_connected and not host.accepting
        entered.clear()
        await host.resume()
        await asyncio.wait_for(entered.wait(),1)
        assert len(attempts)==2 and host.accepting
    finally:
        await host._cancel_connector_tasks()
        host.runtime.process_task.cancel()
        await asyncio.gather(host.runtime.process_task,return_exceptions=True)


@pytest.mark.asyncio
async def test_handler_error_is_not_swallowed_into_success_ack():
    update=SimpleNamespace(update_id=55)
    async def ptb_catches_handler_error(_):
        record_handler_error(update,RuntimeError("handler failed before admission"))
    runtime=SimpleNamespace(app=SimpleNamespace(process_update=ptb_catches_handler_error))
    with pytest.raises(RuntimeError,match="not acknowledged"):
        await dispatch_update(runtime,update)
    runtime.app.process_update=AsyncMock()
    await dispatch_update(runtime,update)  # Failed context cannot leak into next update.


@pytest.mark.asyncio
async def test_false_processing_ack_never_advances_offset(monkeypatch):
    monkeypatch.setattr("orchestrator.telegram_ingress.TELEGRAM_RETRY_SECONDS",0.001)
    bot=_Bot("token")
    refused=asyncio.Event()
    async def deliver(_):
        refused.set()
        return False
    writes=[]
    ingress=CoreTelegramIngress(agent_name="one",token="token",
        handle_lookup=lambda _:SimpleNamespace(deliver_telegram_update=deliver),
        bot_factory=lambda _:bot,checkpoint_callback=writes.append)
    try:
        await ingress.start(drop_pending_updates=False)
        await asyncio.wait_for(refused.wait(),1)
        await asyncio.sleep(0.01)
        assert ingress.offset is None and writes==[]
        assert all(call[1] is None for call in bot.calls if isinstance(call,tuple) and call[0]=="get_updates")
    finally:
        await ingress.stop()


@pytest.mark.asyncio
async def test_checkpoint_retry_never_reprocesses_acknowledged_update(monkeypatch):
    monkeypatch.setattr("orchestrator.telegram_ingress.TELEGRAM_RETRY_SECONDS",0.001)
    bot=_Bot("token")
    delivered=[]
    writes=[]
    saved=asyncio.Event()
    async def deliver(update):
        delivered.append(update["update_id"])
        return True
    async def checkpoint(offset):
        writes.append(offset)
        if len(writes)==1:
            assert ingress.offset is None
            raise PermissionError("temporary sharing violation")
        saved.set()
    ingress=CoreTelegramIngress(agent_name="one",token="token",
        handle_lookup=lambda _:SimpleNamespace(deliver_telegram_update=deliver),
        bot_factory=lambda _:bot,checkpoint_callback=checkpoint)
    try:
        await ingress.start(drop_pending_updates=False)
        await asyncio.wait_for(saved.wait(),1)
        assert ingress.offset==8 and delivered==[7] and writes==[8,8]
    finally:
        await ingress.stop()


def test_current_schema_reopen_skips_ddl_but_keeps_recovery(tmp_path,monkeypatch):
    path=tmp_path/"sessions.sqlite3"
    SessionStore(path,instance_id="test")
    monkeypatch.setattr(SessionStore,"_initialize_schema",
        lambda _:pytest.fail("current schema must not migrate again"))
    repair=[]
    monkeypatch.setattr(SessionStore,"_repair_pending_audio_projections",lambda _:repair.append(True))
    SessionStore(path,instance_id="test")
    assert repair==[True]


def test_old_schema_still_migrates_and_future_schema_is_rejected(tmp_path):
    path=tmp_path/"sessions.sqlite3"
    SessionStore(path,instance_id="test")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE schema_metadata SET value='35' WHERE key='schema_version'")
    SessionStore(path,instance_id="test")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM schema_metadata WHERE key='schema_version'").fetchone()[0]=="36"
        db.execute("UPDATE schema_metadata SET value='999' WHERE key='schema_version'")
    with pytest.raises(RuntimeError,match="newer schema"):
        SessionStore(path,instance_id="test")


@pytest.mark.asyncio
async def test_offline_final_result_survives_store_reopen_without_rerunning_business(tmp_path,monkeypatch):
    from tests.test_frontend_attachment_tool import _running_session, _ToolRuntime
    from orchestrator.frontend_delivery import freeze_run_delivery_route
    from orchestrator.frontend_incremental_delivery import dispatch_pending_telegram_deliverables
    from orchestrator import runtime_delivery, telegram_delivery_failover
    monkeypatch.setattr(telegram_delivery_failover,"handle_blocked_send",AsyncMock(return_value=False))
    route=freeze_run_delivery_route(message_source_id="session_api",session_surface="workbench",
        session_channel_key="default",chat_id=7,telegram_requested=True)
    store,owner,session,accepted=_running_session(tmp_path,delivery_route=route)
    store.finish_request(accepted.request_id,success=True,assistant_text="One durable answer")
    runtime=_ToolRuntime(tmp_path,store)
    runtime.telegram_connected=False
    assert await runtime_delivery.send_long_message(runtime,chat_id=7,text="One durable answer",
        request_id=accepted.request_id,frontend_outbox=True)==(0.0,0)
    assert store.pending_incremental_telegram_deliveries(agent_id="agent1")
    reopened=SessionStore(store.db_path,instance_id=store.instance_id,
        attachment_root=store.attachment_files_root)
    runtime.session_store=reopened
    runtime.telegram_connected=True
    assert await dispatch_pending_telegram_deliverables(runtime)==1
    assert await dispatch_pending_telegram_deliverables(runtime)==0
    assert reopened.get_run_by_request(accepted.request_id,owner_id=owner)["state"]=="completed"
    assert len(runtime.app.bot.messages)==1


@pytest.mark.asyncio
async def test_topology_schedule_and_retirement_do_not_hold_caller():
    supervisor=FunctionWorkerSupervisor.__new__(FunctionWorkerSupervisor)
    supervisor._shutting_down=False
    supervisor._topology_revision=0
    supervisor._topology_task=None
    supervisor._retirement_tasks={}
    entered=asyncio.Event()
    release=asyncio.Event()
    async def broadcast():
        entered.set()
        await release.wait()
    supervisor.broadcast_topology=broadcast
    supervisor.schedule_topology()
    await asyncio.wait_for(entered.wait(),1)
    client=SimpleNamespace()
    class Client:
        agent_name="old"
        async def shutdown(self,force):
            await release.wait()
    client=Client()
    supervisor.retire_workers([client])
    assert not supervisor._topology_task.done()
    assert client in supervisor._retirement_tasks
    tasks=[supervisor._topology_task,*supervisor._retirement_tasks.values()]
    release.set()
    await asyncio.wait_for(asyncio.gather(*tasks),1)
    assert not supervisor._retirement_tasks
# HASHI-Fingerprint: hfp_437534c1d8bf4caa9cacfe801dff1876
