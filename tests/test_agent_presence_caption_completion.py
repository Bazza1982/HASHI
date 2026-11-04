import asyncio
import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from orchestrator import agent_presence_notify, runtime_session, runtime_media, runtime_debug_reporting
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.frontend_delivery import freeze_run_delivery_route
from orchestrator import terminal_console, frontend_whatsapp_mirror
from tests.test_agent_presence_captions import fixture

@pytest.mark.asyncio
async def test_real_async_listener_wakes_caption_after_threaded_terminal_commit(tmp_path,monkeypatch):
    runtime, owner, _, _ = fixture(tmp_path)
    store=runtime.session_store
    session=store.resolve_primary_session(owner_id=owner,agent_id=runtime.name,establish=False)
    route=freeze_run_delivery_route(message_source_id='workbench',session_surface='workbench',
        session_channel_key='default',chat_id='default',telegram_requested=False,
        additional_mirrors=[{'surface':'agentpresence','channel_key':owner}])
    store.accept_run(session_id=session['session_id'],owner_id=owner,agent_id=runtime.name,
        request_id='threaded-caption',text='question',source='workbench',
        idempotency_key='threaded-caption',delivery_route=route)
    store.mark_request_running('threaded-caption',worker_id='test-worker')
    run=store.finish_request('threaded-caption',success=True,assistant_text='threaded result')
    thread=threading.get_ident();commits=[]
    def committed(*_):
        commits.append(threading.get_ident())
        return SimpleNamespace(run=run,payload={'success':True},continuity_status='captured',side_errors=())
    monkeypatch.setattr(runtime_session,'finish_request_from_listener',committed)
    monkeypatch.setattr(runtime_media,'finish_native_voice_transcript_path',AsyncMock())
    monkeypatch.setattr(runtime_debug_reporting,'schedule_terminal_diagnostic',lambda *_:None)
    monkeypatch.setattr(terminal_console,'finish_request',lambda *a,**k:None)
    monkeypatch.setattr(frontend_whatsapp_mirror,'deliver_whatsapp_mirror',AsyncMock())
    runtime.request_activity=SimpleNamespace(complete=lambda *a,**k:None)
    runtime.logger=logging.getLogger('caption-test')
    runtime._request_listeners={};runtime._pending_request_results={}
    posts=[]
    monkeypatch.setattr(agent_presence_notify,'_token',lambda:'local-test')
    monkeypatch.setattr(agent_presence_notify,'_post_json',lambda path,body:posts.append((path,body)) or True)
    await FlexibleAgentRuntime._notify_request_listeners(runtime,'threaded-caption',{'success':True})
    await asyncio.gather(*getattr(runtime,'_optional_frontend_tasks',()))
    assert commits and commits[0]!=thread
    assert len(posts)==1 and posts[0][1]['caption']['text']=='threaded result'
    assert runtime._pending_request_results['threaded-caption']['success'] is True
# HASHI-Fingerprint: hfp_defa2f9fa8c6458b97124e408e770b1d
