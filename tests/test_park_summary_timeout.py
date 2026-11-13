"""Park saves one recoverable topic within its summary deadline, without Engine effects."""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from pathlib import Path
import pytest
from orchestrator import flexible_agent_runtime as owner
from orchestrator.parked_topics import ParkedTopicStore

@pytest.fixture
def runtime(tmp_path, monkeypatch):
    cls = owner.FlexibleAgentRuntime
    value = NS()
    value._fallback_park_summary = lambda *a, **k: cls._fallback_park_summary(value, *a, **k)
    value._build_park_summary_prompt = lambda *a, **k: cls._build_park_summary_prompt(value, *a, **k)
    value._extract_json_object = lambda text: __import__('json').loads(text)
    value.logger = NS(warning=lambda *a, **k: None)
    value.backend_manager = NS(generate_response=AsyncMock(return_value=NS(is_success=True, text='{"title":"Useful title","summary_short":"Chosen summary","summary_long":"Complete generated summary"}')))
    builder = NS(build_recent_context_block=lambda **k: ('USER: Preserve this unfinished calculation.\nASSISTANT: Next step is verification.', 1, None), get_recent_rounds=lambda **k: [[{'role':'user','text':'Preserve this unfinished calculation.'},{'role':'assistant','text':'Next step is verification.'}]])
    monkeypatch.setattr(owner.runtime_session, 'session_handoff_builder', lambda *a, **k: builder)
    monkeypatch.setattr(owner, 'PARK_SUMMARY_TIMEOUT_SECONDS', .015, raising=False)
    value._summarize_current_topic_for_parking = lambda **k: cls._summarize_current_topic_for_parking(value, **k)
    value._is_authorized_user = lambda actor: actor == 7
    value._backend_busy = lambda: False
    value._reply_text = AsyncMock()
    value.parked_topics = ParkedTopicStore(tmp_path)
    value.session_id_dt = 'synthetic-session'
    return value

@pytest.mark.asyncio
async def test_slow_summary_falls_back_before_command_deadline_and_saves_once(runtime):
    cancelled = asyncio.Event()
    async def slow(*a, **k):
        try: await asyncio.Event().wait()
        finally: cancelled.set()
    runtime.backend_manager.generate_response.side_effect = slow
    await asyncio.wait_for(owner.FlexibleAgentRuntime.cmd_park(runtime, NS(effective_user=NS(id=7)), NS(args=['chat','Explicit title'])), .3)
    rows = runtime.parked_topics.list_topics()
    assert len(rows) == 1
    assert rows[0]['title'] == 'Explicit title'
    assert 'unfinished calculation' in rows[0]['summary_short']
    assert 'Next step is verification' in rows[0]['recent_context']
    assert cancelled.is_set()
    assert runtime.backend_manager.generate_response.await_count == 1
    assert runtime._reply_text.await_count == 2

@pytest.mark.asyncio
async def test_fast_summary_preserves_generated_fields(runtime):
    result = await runtime._summarize_current_topic_for_parking()
    assert result['title'] == 'Useful title'
    assert result['summary_short'] == 'Chosen summary'
    assert result['summary_long'] == 'Complete generated summary'

@pytest.mark.asyncio
async def test_summary_failure_preserves_context_in_fallback(runtime):
    runtime.backend_manager.generate_response.side_effect = RuntimeError('synthetic helper failure')
    result = await runtime._summarize_current_topic_for_parking()
    assert 'unfinished calculation' in result['summary_short']
    assert 'Next step is verification' in result['recent_context']

@pytest.mark.asyncio
async def test_user_cancellation_remains_cancellation_without_saving(runtime):
    entered = asyncio.Event()
    async def slow(*a, **k):
        entered.set()
        await asyncio.Event().wait()
    runtime.backend_manager.generate_response.side_effect = slow
    task = asyncio.create_task(owner.FlexibleAgentRuntime.cmd_park(runtime, NS(effective_user=NS(id=7)), NS(args=['chat'])))
    await entered.wait(); task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert runtime.parked_topics.list_topics() == []

@pytest.mark.asyncio
async def test_busy_agent_does_not_start_or_save_summary(runtime):
    runtime._backend_busy = lambda: True
    await owner.FlexibleAgentRuntime.cmd_park(runtime, NS(effective_user=NS(id=7)), NS(args=['chat']))
    runtime.backend_manager.generate_response.assert_not_awaited()
    assert runtime.parked_topics.list_topics() == []
