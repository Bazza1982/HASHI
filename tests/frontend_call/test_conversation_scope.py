from types import SimpleNamespace

import pytest

from orchestrator.frontend_call.contract import CallError
from orchestrator.frontend_call.ports import HashiPorts
from orchestrator.frontend_live_voice.protocol import LiveVoiceError
from orchestrator.session_store import SessionStore
from orchestrator.workbench_api import WorkbenchApiServer


def validate(kind, store, binding, owner='owner'):
    api = SimpleNamespace(session_store=store, _persistent_session_v1_ready=lambda: True,
                          _runtime_map=lambda: {'agent': object()})
    if kind == 'call':
        HashiPorts(api).validate(owner, binding)
    else:
        WorkbenchApiServer._require_live_voice_session_scope(
            api, owner_id=owner, agent_id=binding['agent_id'],
            session_id=binding['session_id'], context_generation=binding['context_generation'])


@pytest.mark.parametrize('kind', ['call', 'phone'])
def test_owned_fresh_conversation_can_call_without_changing_primary(tmp_path, kind):
    store = SessionStore(tmp_path / 'sessions.db', instance_id='TEST')
    primary = store.ensure_default_session(owner_id='owner', agent_id='agent')
    fresh = store.create_session(owner_id='owner', agent_id='agent', title='Simple conversation')
    validate(kind, store, {'agent_id': 'agent', 'session_id': fresh['session_id'],
                           'context_generation': fresh['context_generation']})
    assert store.resolve_primary_session(owner_id='owner', agent_id='agent')['session_id'] == primary['session_id']


@pytest.mark.parametrize('kind', ['call', 'phone'])
@pytest.mark.parametrize('invalid', ['owner', 'agent', 'generation', 'deleted', 'archived', 'activity'])
def test_call_scope_still_rejects_unowned_stale_and_unwritable_sessions(tmp_path, kind, invalid):
    store = SessionStore(tmp_path / 'sessions.db', instance_id='TEST')
    store.ensure_default_session(owner_id='owner', agent_id='agent')
    fresh = store.create_session(owner_id='owner', agent_id='agent',
                                 session_kind='agent_activity' if invalid == 'activity' else 'conversation')
    binding = {'agent_id': 'other-agent' if invalid == 'agent' else 'agent',
               'session_id': fresh['session_id'],
               'context_generation': fresh['context_generation'] + (invalid == 'generation')}
    if invalid in ('deleted', 'archived'):
        store.archive_session(fresh['session_id'], deleted=invalid == 'deleted')
    with pytest.raises((CallError, LiveVoiceError)):
        validate(kind, store, binding, owner='other-owner' if invalid == 'owner' else 'owner')
