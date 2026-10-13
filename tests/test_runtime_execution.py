from __future__ import annotations
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from orchestrator import runtime_execution as execution
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime


@pytest.mark.asyncio
async def test_session_dispatch_overlaps_and_preserves_same_session_order(monkeypatch, tmp_path):
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.name = "one-agent"
    runtime.config = SimpleNamespace(extra={}, active_backend="codex-cli", project_root=tmp_path)
    runtime.global_config = SimpleNamespace(project_root=tmp_path)
    runtime.queue = execution.SessionQueue()
    runtime._execution_admissions = {}
    runtime.error_logger = SimpleNamespace(exception=lambda *args: None)
    runtime._publish_worker_metadata = AsyncMock()
    runtime.current_request_meta = None
    runtime.is_generating = False
    runtime.backend_manager = SimpleNamespace(current_backend=None)
    handles = {}
    async def create(runtime, item, frozen):
        session = item.session_id
        if session not in handles:
            handles[session] = execution.Execution(runtime, session, "frozen", {
                "backend_manager": SimpleNamespace(current_backend=object(), shutdown=AsyncMock()),
                "current_request_meta": None, "is_generating": False,
            })
        runtime._session_executions[session] = handles[session]
        return handles[session]
    monkeypatch.setattr(execution, "create_execution", create)
    monkeypatch.setattr(execution, "snapshot", lambda runtime: {"config":runtime.config,"settings":{}})
    entered = {key: asyncio.Event() for key in ("A1", "A2", "B1", "C1")}
    release = {key: asyncio.Event() for key in entered}
    observed = []
    completed = []
    async def process(runtime, item):
        runtime.current_request_meta = {"request_id": item.request_id, "hashi_session_id": item.session_id}
        runtime.is_generating = True
        observed.append((item.request_id, runtime.backend_manager.current_backend, tuple(completed)))
        entered[item.request_id].set()
        try:
            await release[item.request_id].wait()
            assert runtime.current_request_meta["request_id"] == item.request_id
            completed.append(item.request_id)
        finally:
            runtime.is_generating = False
            runtime.current_request_meta = None
            runtime.queue.task_done()
    for key in entered:
        await runtime.queue.put(SimpleNamespace(request_id=key, session_id=key[0]))
    dispatcher = asyncio.create_task(execution.process_sessions(runtime, process))
    try:
        await asyncio.wait_for(entered["A1"].wait(), 1)
        await asyncio.wait_for(entered["B1"].wait(), 1)
        assert not entered["A2"].is_set() and not entered["C1"].is_set()
        assert execution.queue_reasons(runtime) == {'A2':'session_order', 'C1':'agent_capacity'}
        assert observed[0][1] is not observed[1][1]
        assert runtime.is_generating
        release["B1"].set()
        await asyncio.wait_for(entered["C1"].wait(), 1)
        assert not entered["A2"].is_set()
        assert runtime.is_generating
        release["A1"].set()
        await asyncio.wait_for(entered["A2"].wait(), 1)
        assert "A1" in next(row[2] for row in observed if row[0] == "A2")
        assert runtime.is_generating
        for event in release.values():
            event.set()
        await asyncio.wait_for(runtime.queue.join(), 1)
        await asyncio.sleep(0)
        assert not runtime.is_generating
        assert not runtime._session_execution_tasks
    finally:
        dispatcher.cancel()
        await asyncio.gather(dispatcher, return_exceptions=True)
    assert all(value.values["backend_manager"].shutdown.await_count == 1 for value in handles.values())


@pytest.mark.asyncio
async def test_two_tasks_cannot_overwrite_other_session_callback_context():
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.current_request_meta = None
    runtime.backend_manager = object()
    views = [execution.Execution(runtime, str(i), "fixed", {"current_request_meta": None}) for i in range(2)]
    first_ready = asyncio.Event()
    second_ready = asyncio.Event()
    async def first():
        with execution.bind(views[0]):
            runtime.current_request_meta = {"request_id": "first"}
            first_ready.set()
            await second_ready.wait()
            await asyncio.sleep(0)
            assert runtime.current_request_meta == {"request_id": "first"}
    async def second():
        await first_ready.wait()
        with execution.bind(views[1]):
            runtime.current_request_meta = {"request_id": "second"}
            second_ready.set()
            assert await asyncio.to_thread(lambda: runtime.current_request_meta) == {"request_id": "second"}
    await asyncio.gather(first(), second())
    assert runtime.current_request_meta is None


@pytest.mark.asyncio
async def test_real_backend_managers_create_independent_native_handles_without_state_writes(tmp_path,monkeypatch):
    from tests.test_flexible_backend_state import _make_manager
    from adapters.registry import get_backend_class
    CodexCliAdapter=get_backend_class('codex-cli')
    from orchestrator.session_backend_manager import SessionBackendManager
    owner=_make_manager(tmp_path/'agent')
    runtime=FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.config=owner.config;runtime.backend_manager=owner;runtime._session_executions={}
    owner.runtime=runtime
    monkeypatch.setattr(CodexCliAdapter,'initialize',AsyncMock(return_value=True))
    monkeypatch.setattr(CodexCliAdapter,'shutdown',AsyncMock())
    before=owner.state_file.read_bytes() if owner.state_file.exists() else None
    first=await execution.create_execution(runtime,SimpleNamespace(session_id='A',context_generation=1),execution.snapshot(runtime))
    second=await execution.create_execution(runtime,SimpleNamespace(session_id='B',context_generation=1),execution.snapshot(runtime))
    a=first.values['backend_manager'];b=second.values['backend_manager']
    assert isinstance(a,SessionBackendManager) and isinstance(b,SessionBackendManager)
    assert a.current_backend is not b.current_backend
    assert a.config is not b.config and a.current_backend.config is not b.current_backend.config
    a.current_backend._session_id='native-A';b.current_backend._session_id='native-B'
    assert a.current_backend._session_id=='native-A'
    with pytest.raises(RuntimeError,match='cannot write'):
        a.persist_state()
    assert (owner.state_file.read_bytes() if owner.state_file.exists() else None)==before
    await a.shutdown();await b.shutdown()


@pytest.mark.asyncio
async def test_control_thread_interrupts_only_the_selected_session_adapter():
    from orchestrator.out_of_band_control import AgentControlLane
    runtime=FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.name='one-agent';runtime.backend_manager=SimpleNamespace(current_backend=None)
    calls=[]
    class Backend:
        def __init__(self,session): self.session=session
        def interrupt_nowait(self,reason): calls.append((self.session,reason));return 1
    views=[execution.Execution(runtime,key,'fixed',{'backend_manager':SimpleNamespace(current_backend=Backend(key))}) for key in ('A','B')]
    lane=AgentControlLane(runtime)
    try:
        with execution.bind(views[1]):
            result=await lane.interrupt('USER_STOP')
        assert result.interrupted==1 and calls==[('B','USER_STOP')]
        with execution.bind(views[0]):
            result=await lane.interrupt('USER_STEER')
        assert calls==[('B','USER_STOP'),('A','USER_STEER')]
    finally:
        await asyncio.to_thread(lane.close)


@pytest.mark.asyncio
async def test_cancel_while_waiting_instance_budget_cleans_queue_without_starting_backend(tmp_path):
    from orchestrator.execution_resources import instance_state, leases
    runtime=FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.name='one-agent';runtime.config=SimpleNamespace(extra={},active_backend='codex-cli',project_root=tmp_path)
    runtime.global_config=SimpleNamespace(project_root=tmp_path)
    runtime.backend_manager=SimpleNamespace(current_backend=None)
    runtime.queue=execution.SessionQueue();runtime.current_request_meta=None;runtime.is_generating=False
    runtime._publish_worker_metadata=AsyncMock();runtime.error_logger=SimpleNamespace(exception=lambda *a:None)
    runtime._notify_request_listeners=AsyncMock()
    runtime._execution_admissions={'waiting':{'config':runtime.config,'settings':{'_agents_json_global':{'execution_limits':{'instance_sessions':1}}}}}
    process=AsyncMock()
    async with leases(instance_state(runtime),[('instance-sessions',1,False)]):
        await runtime.queue.put(SimpleNamespace(request_id='waiting',session_id='A',source='session-api',summary='queued'))
        dispatcher=asyncio.create_task(execution.process_sessions(runtime,process))
        try:
            for _ in range(100):
                if getattr(runtime,'_execution_queue_reasons',{}).get('waiting')=='instance_capacity':break
                await asyncio.sleep(.01)
            assert runtime._execution_queue_reasons['waiting']=='instance_capacity'
            waiting=runtime._session_execution_tasks['waiting']
            waiting.cancel();await asyncio.gather(waiting,return_exceptions=True)
            await asyncio.wait_for(runtime.queue.join(),.3)
            process.assert_not_awaited()
            assert not runtime._session_execution_tasks and not runtime._execution_queue_reasons
        finally:
            dispatcher.cancel();await asyncio.gather(dispatcher,return_exceptions=True)
