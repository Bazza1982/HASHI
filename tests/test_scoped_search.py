import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.flexible_backend_manager import FlexibleBackendManager
from tools.gateway.context import GatewayContext
from tools.registry import ToolRegistry


def pause_command(seconds):
    if os.name == 'nt':
        return "& '" + sys.executable.replace("'", "''") + "' -c 'import time; time.sleep(" + str(seconds) + ")'"
    import shlex
    return shlex.quote(sys.executable) + " -c 'import time; time.sleep(" + str(seconds) + ")'"


def registry_for(tmp_path: Path, *, options=None):
    home = tmp_path / 'workspaces' / 'agent'
    repo = tmp_path / 'repo'
    home.mkdir(parents=True, exist_ok=True)
    repo.mkdir(exist_ok=True)
    manager = FlexibleBackendManager.__new__(FlexibleBackendManager)
    manager.current_backend = SimpleNamespace(tool_registry=None)
    manager.secrets = {}
    manager.global_config = SimpleNamespace(authorized_id=123)
    manager.config = SimpleNamespace(name='agent', telegram_token_key='agent')
    manager.logger = SimpleNamespace(error=lambda *a, **k: None, info=lambda *a, **k: None)
    cfg = SimpleNamespace(name='agent', workspace_dir=home,
                          extra={'workzone_state': {'revision': 7, 'slots': [
                              {'slot_id': 'main', 'path': str(repo), 'enabled': True}]}},
                          resolve_access_root=lambda: tmp_path)
    manager._attach_tool_registry({'allowed': ['file_search', 'file_read', 'log_query',
                                             'file_list', 'file_write', 'shell'],
                                  **(options or {})}, cfg)
    return manager.current_backend.tool_registry, repo, home


def payload(result):
    assert not result.is_error, result.output
    value = json.loads(result.output)
    return value['data'] if 'status' in value else value


@pytest.mark.asyncio
async def test_search_defaults_include_own_home_and_support_read_without_write(tmp_path):
    registry, repo, home = registry_for(tmp_path)
    for root in (repo, home):
        (root / 'proposal.txt').write_text('needle', encoding='utf-8')
    colocated = repo / 'workspaces' / 'other'
    colocated.mkdir(parents=True)
    (colocated / 'proposal.txt').write_text('unrelated Agent fixture', encoding='utf-8')
    data = payload(await registry.execute('file_search', {'query': 'proposal'}))
    assert {m['path'] for m in data['matches']} == {
        str(repo / 'proposal.txt'), str(home / 'proposal.txt')}
    assert data['coverage_complete'] and data['scope_exhausted']
    read = await registry.execute('file_read', {'path': str(home / 'proposal.txt')})
    assert not read.is_error and 'needle' in read.output
    log = await registry.execute('log_query', {'path': str(home / 'proposal.txt'), 'terms': ['needle']})
    assert payload(log)['matches']
    write = await registry.execute('file_write', {'path': str(home / 'new.txt'), 'content': 'no'})
    assert write.is_error and not (home / 'new.txt').exists()
    assert registry.access_roots == (repo.resolve(),)


@pytest.mark.asyncio
async def test_pao_home_read_capability_is_not_a_write_grant(tmp_path):
    registry, repo, home = registry_for(tmp_path)
    (home / 'saved.txt').write_text('saved', encoding='utf-8')
    read = await registry.execute('file_read', {'path': str(home / 'saved.txt')})
    assert not read.is_error, read.output
    outside = tmp_path / 'outside.txt'
    outside.write_text('outside', encoding='utf-8')
    assert (await registry.execute('file_read', {'path': str(outside)})).is_error


@pytest.mark.asyncio
async def test_tools_emit_activity_before_return_without_model_commentary(tmp_path):
    registry, _, _ = registry_for(tmp_path, options={
        'tool_activity': {'long_threshold_seconds': 0.02, 'snapshot_interval_seconds': 0.01}})
    events = []
    task = asyncio.create_task(registry.execute_with_audit_context(
        'shell', {'command': pause_command(.4)}, 'call',
        audit_context={'request_id': 'run', 'tool_activity_observer': events.append}))
    try:
        await asyncio.sleep(0.2)
        assert not task.done()
        assert any(e.kind == 'tool_activity' and e.metadata['state'] == 'running'
                   for e in events)
    finally:
        await task


@pytest.mark.asyncio
async def test_explicit_roots_and_gateway_keep_exact_scope(tmp_path):
    registry, repo, home = registry_for(tmp_path)
    (repo / 'wanted.txt').touch()
    (home / 'wanted.txt').touch()
    other = tmp_path / 'workspaces' / 'other'
    other.mkdir()
    (other / 'wanted.txt').touch()
    gateway = GatewayContext.from_registry(registry, backend='codex-cli').build_registry()
    data = payload(await gateway.execute('file_search', {'query': 'wanted', 'roots': [str(repo)]}))
    assert [m['path'] for m in data['matches']] == [str(repo / 'wanted.txt')]
    assert [r['path'] for r in data['selected_roots']] == [str(repo)]
    assert data['selected_roots'][0]['provenance'] == 'tool-explicit'
    assert gateway.access_roots == registry.access_roots
    read = await gateway.execute('file_read', {'path': str(home / 'wanted.txt')})
    assert not read.is_error
    denied = await gateway.execute('file_search', {'query': 'wanted', 'roots': [str(other)]})
    assert denied.is_error


@pytest.mark.asyncio
async def test_no_workzone_prefers_cwd_over_broad_access_root(tmp_path):
    home = tmp_path / 'agent'
    home.mkdir()
    (home / 'target.txt').touch()
    (tmp_path / 'target.txt').touch()
    registry = ToolRegistry(['file_search'], tmp_path, home, {})
    data = payload(await registry.execute('file_search', {'query': 'target'}))
    assert [m['path'] for m in data['matches']] == [str(home / 'target.txt')]


@pytest.mark.asyncio
async def test_content_pagination_has_no_lost_or_duplicate_long_record_hits(tmp_path):
    registry, repo, _ = registry_for(tmp_path, options={'smart_registry': {'enabled': True}})
    text = ('x' * 65533 + ' TOKEN\n') * 6
    (repo / 'long.jsonl').write_text(text, encoding='utf-8')
    text = (repo / 'long.jsonl').read_bytes().decode('utf-8')
    query = {'query': 'TOKEN', 'mode': 'content', 'roots': [str(repo)],
             'max_results': 2, 'context_chars': 24}
    offsets = []
    data = None
    for _ in range(12):
        result = await registry.execute_with_audit_context('file_search', query, audit_context={'request_id': 'page-run'})
        assert len(result.output) < 20000
        envelope = json.loads(result.output)
        data = payload(result)
        offsets.extend(m['character_offset'] for m in data['matches'])
        if not data['can_continue']:
            assert envelope['status'] == 'success'
            break
        assert envelope['status'] == 'partial' and not data['scope_exhausted']
        query['cursor'] = data['next_cursor']
    assert data['coverage_complete']
    assert offsets == [i for i in range(len(text)) if text.startswith('TOKEN', i)]


@pytest.mark.asyncio
async def test_cursor_bound_to_run_and_file_tree_revision(tmp_path):
    registry, repo, _ = registry_for(tmp_path)
    (repo / 'a.txt').write_text('hit hit hit', encoding='utf-8')
    args = {'query': 'hit', 'mode': 'content', 'roots': [str(repo)], 'max_results': 1}
    data = payload(await registry.execute_with_audit_context('file_search', args,
                   audit_context={'request_id': 'one'}))
    args['cursor'] = data['next_cursor']
    wrong = await registry.execute_with_audit_context('file_search', args, audit_context={'request_id': 'two'})
    assert wrong.is_error and json.loads(wrong.output)['error']['code'] == 'cursor_stale'
    (repo / 'a.txt').write_text('changed', encoding='utf-8')
    stale = await registry.execute_with_audit_context('file_search', args, audit_context={'request_id': 'one'})
    assert stale.is_error
    assert json.loads(stale.output)['data']['stop_reason'] == 'cursor_stale'
    assert not json.loads(stale.output)['data']['can_continue']


@pytest.mark.asyncio
async def test_zero_partial_and_exclusions_are_distinct(tmp_path):
    registry, repo, _ = registry_for(tmp_path)
    (repo / '.env').write_text('SECRET', encoding='utf-8')
    dep = repo / 'node_modules'
    dep.mkdir()
    (dep / 'needle.txt').write_text('SECRET', encoding='utf-8')
    (repo / 'paper.pdf').write_bytes(b'fake PDF SECRET')
    (repo / 'binary.txt').write_bytes(b'\0 SECRET')
    query = {'query': 'SECRET', 'mode': 'content', 'roots': [str(repo)]}
    data = payload(await registry.execute('file_search', query))
    assert not data['matches'] and data['coverage_complete']
    assert 'node_modules' in data['coverage']['default_directory_exclusions']
    assert 'tool_action_audit.jsonl' in data['coverage']['default_file_exclusions']
    assert '.pdf' in data['coverage']['excluded_content_suffixes']
    shown = await registry.execute('file_list', {'path': str(repo)})
    hidden = await registry.execute('file_list', {'path': str(repo), 'include_hidden': False})
    assert '.env' in shown.output and '.env' not in hidden.output
    expanded = payload(await registry.execute('file_search', {
        **query, 'include_hidden': True, 'profile': 'expanded', 'include': ['*.txt', '.env']}))
    assert {Path(m['path']).name for m in expanded['matches']} == {'.env', 'needle.txt'}
    path = payload(await registry.execute('file_search', {'query': '*.pdf', 'match': 'glob', 'roots': [str(repo)]}))
    assert len(path['matches']) == 1
    unavailable = await registry.execute('file_search', {**query, 'match': 'regex'})
    assert unavailable.is_error and json.loads(unavailable.output)['status'] == 'unavailable'


@pytest.mark.asyncio
async def test_links_cannot_silently_expand_selected_scope(tmp_path):
    registry, repo, _ = registry_for(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'target.txt').touch()
    try:
        (repo / 'escape').symlink_to(outside, target_is_directory=True)
        (repo / 'cycle').symlink_to(repo, target_is_directory=True)
    except OSError:
        pytest.skip('This host does not grant symlink creation')
    data = payload(await registry.execute('file_search', {'query': 'target', 'roots': [str(repo)]}))
    assert not data['matches'] and data['coverage_complete']
    follow = payload(await registry.execute('file_search', {'query': 'target', 'roots': [str(repo)],
                     'options': {'follow_links': True}}))
    assert not follow['matches'] and not follow['coverage_complete']
    assert follow['counters']['errors'] >= 2


@pytest.mark.asyncio
async def test_multi_root_coverage_preserves_missing_workzone(tmp_path):
    from orchestrator.workzone import build_search_scope, normalize_workzone_state
    registry, repo, home = registry_for(tmp_path)
    missing = tmp_path / 'missing'
    state = normalize_workzone_state({'revision': 8, 'slots': [
        {'slot_id': 'main', 'path': str(repo), 'enabled': True},
        {'slot_id': '1', 'path': str(missing), 'enabled': True}]})
    registry.set_search_scope(build_search_scope(state, owner_id='123', agent_id='agent',
        agent_home=home, execution_cwd=repo, access_roots=registry.access_roots))
    (home / 'match.txt').touch()
    result = await registry.execute('file_search', {'query': 'match'})
    data = payload(result)
    assert json.loads(result.output)['status'] == 'partial'
    assert any(m['path'] == str(home / 'match.txt') for m in data['matches'])
    assert any(r['path'] == str(missing) and r['failed'] for r in data['coverage']['roots'])
    assert not data['coverage_complete']


@pytest.mark.asyncio
async def test_native_unicode_space_and_quote_paths_are_data(tmp_path, monkeypatch):
    # Native Python 3.12 can inherit an ANSI stdout encoding for pipes.
    monkeypatch.setenv('PYTHONIOENCODING', 'cp1252')
    registry, repo, _ = registry_for(tmp_path)
    nested = repo / "中文 folder John's"
    nested.mkdir()
    file = nested / '方案.txt'
    file.write_text('第一行\n目标词\n', encoding='utf-8')
    data = payload(await registry.execute('file_search', {'query': '方案', 'roots': [str(nested)]}))
    assert [item['path'] for item in data['matches']] == [str(file)]
    content = payload(await registry.execute('file_search', {
        'query': '目标词', 'mode': 'content', 'roots': [str(file)]}))
    assert content['matches'][0]['line'] == 2
    assert not (await registry.execute('file_read', {'path': data['matches'][0]['path']})).is_error


@pytest.mark.asyncio
async def test_gateway_preserves_live_read_denial_and_available_results(tmp_path):
    registry, repo, _ = registry_for(tmp_path)
    secret = repo / 'protected.txt'
    secret.write_text('needle', encoding='utf-8')
    ordinary = repo / 'ordinary.txt'
    ordinary.write_text('needle', encoding='utf-8')
    registry.audit_context['global_config'] = SimpleNamespace(
        project_root=repo, bridge_home=repo, secrets_path=secret, instance_id='fixture')
    gateway = GatewayContext.from_registry(registry, backend='codex-cli').build_registry()
    result = await gateway.execute('file_search', {'query': 'needle', 'mode': 'content'})
    data = payload(result)
    assert [m['path'] for m in data['matches']] == [str(ordinary)]
    assert json.loads(result.output)['status'] == 'partial'
    assert any(e['code'] == 'permission_denied' for e in data['coverage']['errors'])
    direct = await gateway.execute('file_search', {'query': 'needle', 'roots': [str(secret)]})
    assert direct.is_error and 'denied' in direct.output
    assert (await gateway.execute('file_read', {'path': str(secret)})).is_error


@pytest.mark.asyncio
async def test_tool_and_request_allowlists_and_read_only_classification(tmp_path):
    from tools.gateway.mcp_stdio import ToolGateway
    registry, _, _ = registry_for(tmp_path)
    assert registry.is_read_only('file_search') and not registry.is_read_only('shell')
    names = {d['function']['name'] for d in registry.get_tool_definitions(tiers=['core'])}
    assert 'file_search' in names and 'shell' in names
    gateway = ToolGateway(GatewayContext.from_registry(registry, backend='codex-cli'))
    assert any(d['name'] == 'file_search' for d in gateway.tool_definitions())
    denied = await registry.execute_with_audit_context('file_search', {'query': 'x'},
        audit_context={'request_tool_allowlist': ['file_read']})
    assert denied.is_error
    restricted = ToolRegistry(['file_read'], tmp_path, tmp_path, {})
    assert (await restricted.execute('file_search', {'query': 'x'})).is_error


@pytest.mark.asyncio
async def test_configuration_rollback_revokes_home_without_widening_writes(tmp_path):
    registry, _, home = registry_for(tmp_path, options={'file_search': {'enabled': False}})
    (home / 'saved.txt').write_text('saved', encoding='utf-8')
    assert registry.search_scope['agent_home'] == str(home)
    assert registry.read_access_roots == registry.access_roots
    assert not any(d['function']['name'] == 'file_search' for d in registry.get_tool_definitions())
    assert (await registry.execute('file_read', {'path': str(home / 'saved.txt')})).is_error
    assert (await registry.execute('file_write', {'path': str(home / 'new.txt'), 'content': 'x'})).is_error
    known = registry.workspace_dir / 'existing.txt'
    known.write_text('needle', encoding='utf-8')
    assert not (await registry.execute('file_list', {'path': str(registry.workspace_dir)})).is_error
    assert payload(await registry.execute('log_query', {'path': str(known), 'terms': ['needle']}))['matches']


@pytest.mark.asyncio
async def test_large_escaped_results_page_without_invalid_json_or_lost_hits(tmp_path):
    registry, repo, _ = registry_for(tmp_path, options={'smart_registry': {'enabled': True}})
    expected = set()
    for i in range(36):
        file = repo / (('long-name-' * 14) + str(i) + '.txt')
        file.write_text('needle ' + '\\' * 1600, encoding='utf-8')
        expected.add(str(file))
    query = {'query': 'needle', 'mode': 'content', 'roots': [str(repo)],
             'max_results': 200, 'context_chars': 2000}
    found = []
    for _ in range(80):
        result = await registry.execute_with_audit_context('file_search', query,
                        audit_context={'request_id': 'budget'})
        assert len(result.output) <= 16000
        data = payload(result)
        found.extend(item['path'] for item in data['matches'])
        if not data['can_continue']:
            assert data['coverage_complete']
            break
        query['cursor'] = data['next_cursor']
    assert set(found) == expected and len(found) == len(expected)


@pytest.mark.asyncio
async def test_repeat_advisory_uses_search_evidence_not_operation_identity(tmp_path):
    registry, repo, _ = registry_for(tmp_path, options={'smart_registry': {'enabled': True}})
    args = {'query': 'missing', 'roots': [str(repo)]}
    for _ in range(3):
        result = await registry.execute_with_audit_context('file_search', args,
                           audit_context={'task_id': 'repeat', 'request_id': 'repeat'})
    envelope = json.loads(result.output)
    assert envelope['status'] == 'success' and envelope['data']['coverage_complete']
    assert envelope['warning']['code'] == 'same_result_repeated'
    # A genuinely changed tree is new evidence; the warning never blocks.
    (repo / 'new.txt').touch()
    changed = await registry.execute_with_audit_context('file_search', args,
                        audit_context={'task_id': 'repeat', 'request_id': 'repeat'})
    assert json.loads(changed.output)['warning'] is None and not changed.is_error


@pytest.mark.asyncio
async def test_herv3_wrapper_keeps_run_scope_after_menu_changes(tmp_path):
    from adapters.her_v2_provider import _EvidenceRecordingToolRegistry
    from orchestrator.her_v2.models import StageRequest, Stage, Effort
    from orchestrator.workzone import build_search_scope, normalize_workzone_state
    registry, original, home = registry_for(tmp_path)
    (original / 'saved.txt').write_text('original', encoding='utf-8')
    request = StageRequest(turn_id='frozen-run', request_ref='fixture', stage=Stage.DIRECT,
        role='main', attempt=1, goal='locate', classification=None, effort=Effort.HIGH)
    wrapper = _EvidenceRecordingToolRegistry(registry, request)
    replacement = tmp_path / 'replacement'
    replacement.mkdir()
    (replacement / 'saved.txt').write_text('replacement', encoding='utf-8')
    state = normalize_workzone_state({'revision': 8, 'slots': [
        {'slot_id': 'main', 'path': str(replacement), 'enabled': True}]})
    registry.access_roots = (replacement,)
    registry.workspace_dir = replacement
    registry.set_search_scope(build_search_scope(state, owner_id='123', agent_id='agent',
        agent_home=home, execution_cwd=replacement, access_roots=(replacement,)))
    data = payload(await wrapper.execute('file_search', {'query': 'saved'}))
    assert data['workzone_revision'] == 7
    assert [m['path'] for m in data['matches']] == [str(original / 'saved.txt')]
    read = await wrapper.execute('file_read', {'path': 'saved.txt'})
    assert 'original' in read.output and 'replacement' not in read.output
    next_run = payload(await registry.execute('file_search', {'query': 'saved'}))
    assert next_run['workzone_revision'] == 8
    assert [m['path'] for m in next_run['matches']] == [str(replacement / 'saved.txt')]
