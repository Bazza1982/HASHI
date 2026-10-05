import asyncio
import json
import logging
import sys
from dataclasses import replace

import pytest

from adapters.stream_events import StreamEvent
from orchestrator.her_message_router import HERMessageRouter
from orchestrator.request_activity import RequestActivityStore
from orchestrator.her_v2.turn_services import TurnServices
from tools.tool_activity import ToolActivity
from tools.registry import ToolRegistry
from tests.test_scoped_search import pause_command


@pytest.mark.asyncio
async def test_verbose_activity_survives_commentary_off_and_live_toggle(tmp_path):
    store = RequestActivityStore()
    store.start('run')
    switches = {'verbose': False, 'commentary': False}
    store.bind_presentation_settings('run', lambda: switches)
    shown = []
    router = HERMessageRouter(request_id='run', logger=logging.getLogger(__name__),
                              verbose_enabled=lambda: switches['verbose'],
                              commentary_enabled=lambda: False,
                              technical_presenter=shown.append,
                              persist_event=lambda e: store.publish_stream('run', e))
    registry = ToolRegistry(['shell'], tmp_path, tmp_path, {}, tool_options={
        'tool_activity': {'long_threshold_seconds': .02, 'snapshot_interval_seconds': .01}})
    task = asyncio.create_task(registry.execute_with_audit_context(
        'shell', {'command': pause_command(.4)}, 'call',
        audit_context={'request_id': 'run', 'tool_activity_observer': router.route}))
    try:
        await asyncio.sleep(.12)
        off = store.poll('run')
        assert not shown and not off['active_tool_activities']
        after = off['latest_sequence']
        switches['verbose'] = True
        on = store.poll('run', after_sequence=after)
        assert not task.done()
        event = next(e for e in on['events'] if e['kind'] == 'tool_activity')
        assert event['presentation_enabled'] and event['presentation_channel'] == 'verbose'
        assert event['tool_activity']['liveness'] == 'alive'
        assert event['tool_activity']['progress'] == 'unknown'
        assert event['tool_activity']['counters']['files_enumerated'] is None
        assert on['active_tool_activities']
    finally:
        await task


@pytest.mark.asyncio
async def test_activity_fences_duplicate_late_and_old_generation_events():
    store = RequestActivityStore()
    store.start('run')
    events = []
    async with ToolActivity('file_search', 'call', {'request_id': 'run', 'tool_activity_observer': events.append}, {}) as activity:
        activity.update(state='running', counters={'files_enumerated': 3}, worker_response=True)
        await activity.emit(visible=True)
        await activity.finish('completed', partial=True, coverage_complete=False)
    for event in events:
        store.publish_stream('run', event)
    before = store.poll('run')['latest_sequence']
    store.publish_stream('run', events[0])
    late = replace(events[0], event_id='late', metadata={**events[0].metadata,
                   'update_sequence': 999, 'state': 'running', 'query': 'secret'})
    store.publish_stream('run', late)
    other = replace(late, metadata={**late.metadata, 'operation_id': 'other', 'function_generation': 'old'})
    store.publish_stream('run', other)
    assert store.poll('run')['latest_sequence'] == before
    final = store.poll('run')['events'][-1]['tool_activity']
    assert final['partial'] and not final['coverage_complete']
    assert 'query' not in final
    store.complete('run', success=True)
    store.publish_stream('run', late)
    assert store.poll('run')['terminal']


@pytest.mark.asyncio
async def test_actual_progress_clock_is_not_reset_by_heartbeat():
    now = [0.]
    services = TurnServices(turn_id='run', downstream=None, clock=lambda: now[0])
    services.tool_started('file_search', {}, 'call')
    facts = {'tool_call_id': 'call', 'state': 'running', 'liveness': 'alive',
             'counters': {'files_enumerated': 2}, 'selected_roots': ['private path']}
    now[0] = 2.
    services.observe_tool_activity(facts)
    now[0] = 20.
    services.observe_tool_activity(facts)
    snapshot = services.snapshot()
    assert snapshot['last_progress_age_s'] == 18.
    assert snapshot['active_tools'][0]['activity']['root_count'] == 1
    assert 'private path' not in json.dumps(snapshot)
    await services.close()
