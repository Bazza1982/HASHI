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
                                             'file_list', 'repo_discover', 'file_write', 'shell'],
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
    regex = payload(await registry.execute('file_search', {**query, 'match': 'regex'}))
    assert not regex['matches'] and regex['coverage_complete']


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


@pytest.mark.asyncio
async def test_regex_path_and_streaming_content_preserve_scope_unicode_and_pages(tmp_path):
    import re

    registry, repo, _ = registry_for(tmp_path)
    (repo / "alpha.txt").write_text("not the query", encoding="utf-8")
    (repo / "\u62a5\u544a.txt").write_text("not the query", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "alpha.txt").write_text("cats 999", encoding="utf-8")
    path_query = r"(alpha|\u62a5\u544a)\.txt$".replace(r"\u62a5\u544a", "\u62a5\u544a")
    found = payload(await registry.execute("file_search", {
        "query": path_query, "match": "regex", "mode": "path", "roots": [str(repo)],
    }))
    assert {Path(row["path"]).name for row in found["matches"]} == {"alpha.txt", "\u62a5\u544a.txt"}
    literal = payload(await registry.execute("file_search", {
        "query": path_query, "match": "literal", "mode": "path", "roots": [str(repo)],
    }))
    assert not literal["matches"] and literal["coverage_complete"]

    content = "\u5f00\u5934 \U0001f338 cats 12\n" + ("x" * 70000) + " \u72d7 34\ncat 56\n"
    source = repo / "events.log"
    source.write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))
    query = {"query": r"(cats?|\u72d7)\s+\d+".replace(r"\u72d7", "\u72d7"),
             "match": "regex", "mode": "content", "roots": [str(source)],
             "max_results": 2, "context_chars": 12}
    expected = [match.start() for match in re.finditer(query["query"], content, re.IGNORECASE)]
    observed = []
    for _ in range(8):
        result = await registry.execute_with_audit_context(
            "file_search", query, audit_context={"request_id": "regex-page-run"},
        )
        data = payload(result)
        observed.extend(row["character_offset"] for row in data["matches"])
        if not data["can_continue"]:
            assert data["coverage_complete"]
            break
        query["cursor"] = data["next_cursor"]
    else:
        pytest.fail("regex scan did not finish")
    assert observed == expected


@pytest.mark.asyncio
async def test_regex_syntax_and_unsupported_constructs_have_specific_failure(tmp_path):
    registry, repo, _ = registry_for(tmp_path)
    (repo / "text.txt").write_text("secretX", encoding="utf-8")
    for pattern in ("[",):
        result = await registry.execute("file_search", {
            "query": pattern, "match": "regex", "mode": "content", "roots": [str(repo)],
        })
        value = json.loads(result.output)
        assert result.is_error
        assert value["error"]["code"] == "invalid_regex"
        assert value["status"] == "failed"
        assert not value["data"]["coverage_complete"]

    lookbehind = payload(await registry.execute("file_search", {
        "query": r"(?<=secret)X", "match": "regex", "mode": "content", "roots": [str(repo)],
    }))
    assert len(lookbehind["matches"]) == 1
    assert lookbehind["effective_policy"]["regex_engine"] == "python"


@pytest.mark.asyncio
async def test_regex_invalid_encoding_and_missing_backend_are_reported_without_silent_coverage(tmp_path, monkeypatch):
    import tools.file_search as search

    registry, repo, _ = registry_for(tmp_path)
    (repo / "bad.txt").write_bytes(b"cats 1 \xff cats 2")
    (repo / "good.txt").write_text("cats 3", encoding="utf-8")
    query = {"query": r"cats\s+\d+", "match": "regex", "mode": "content", "roots": [str(repo)]}
    result = await registry.execute("file_search", query)
    data = payload(result)
    assert {Path(row["path"]).name for row in data["matches"]} == {"good.txt"}
    assert not data["coverage_complete"]
    assert any("invalid_utf8" in row["code"] for row in data["coverage"]["errors"])

    invalid_path = await registry.execute("file_search", {
        "query": "[", "match": "regex", "mode": "path", "roots": [str(repo)],
    })
    assert invalid_path.is_error and json.loads(invalid_path.output)["error"]["code"] == "invalid_regex"

    original_which = search.shutil.which
    monkeypatch.setattr(search.shutil, "which", lambda name, *a, **k: None if name == "rg" else original_which(name, *a, **k))
    fallback = payload(await registry.execute("file_search", query))
    assert fallback["effective_policy"]["regex_engine"] == "python"
    assert {Path(row["path"]).name for row in fallback["matches"]} == {"good.txt"}
    unavailable = await registry.execute("file_search", {
        **query, "options": {"regex_engine": "ripgrep"},
    })
    assert unavailable.is_error and json.loads(unavailable.output)["error"]["code"] == "regex_unavailable"
    path_result = await registry.execute("file_search", {
        "query": r"good\.txt$", "match": "regex", "mode": "path", "roots": [str(repo)],
    })
    assert Path(payload(path_result)["matches"][0]["path"]).name == "good.txt"
    literal = await registry.execute("file_search", {
        "query": "cats 3", "match": "literal", "mode": "content", "roots": [str(repo)],
    })
    assert any(Path(row["path"]).name == "good.txt" for row in payload(literal)["matches"])


@pytest.mark.asyncio
async def test_regex_line_memory_budget_is_visible_adjustable_and_never_claims_complete(tmp_path):
    registry, repo, _ = registry_for(tmp_path)
    source = repo / "long-line.txt"
    source.write_text(("x" * 512) + " needle\n", encoding="utf-8")
    query = {"query": "needle", "match": "regex", "mode": "content", "roots": [str(source)],
             "options": {"regex_max_line_bytes": 128}}
    constrained = await registry.execute("file_search", query)
    envelope = json.loads(constrained.output)
    assert envelope["status"] == "partial"
    assert not envelope["data"]["coverage_complete"]
    assert envelope["data"]["effective_policy"]["regex_max_line_bytes"] == 128
    assert any("regex_line_budget_exceeded" in row["code"]
               for row in envelope["data"]["coverage"]["errors"])

    query["options"]["regex_max_line_bytes"] = 0
    unlimited = payload(await registry.execute("file_search", query))
    assert unlimited["coverage_complete"]
    assert unlimited["effective_policy"]["regex_max_line_bytes"] == 0
    assert len(unlimited["matches"]) == 1


@pytest.mark.asyncio
async def test_regex_automatic_budget_is_frozen_across_cursor_pages(tmp_path, monkeypatch):
    import tools.office.limits as limits

    registry, repo, _ = registry_for(tmp_path)
    source = repo / "pages.txt"
    source.write_text("cat 1\ncat 2\ncat 3\n", encoding="utf-8")
    monkeypatch.setattr(limits, "_available_memory", lambda: 800)
    query = {"query": r"cat\s+\d", "match": "regex", "mode": "content",
             "roots": [str(source)], "max_results": 1}
    first = payload(await registry.execute_with_audit_context(
        "file_search", query, audit_context={"request_id": "regex-budget-page"},
    ))
    assert first["effective_policy"]["regex_max_line_bytes"] == 100
    assert first["can_continue"]
    monkeypatch.setattr(limits, "_available_memory", lambda: 1600)
    query["cursor"] = first["next_cursor"]
    second = payload(await registry.execute_with_audit_context(
        "file_search", query, audit_context={"request_id": "regex-budget-page"},
    ))
    assert second["effective_policy"]["regex_max_line_bytes"] == 100
    assert second["matches"][0]["character_offset"] > first["matches"][0]["character_offset"]

    invalid = await registry.execute("file_search", {
        "query": "one\nanother", "match": "regex", "mode": "content", "roots": [str(source)],
    })
    assert invalid.is_error and json.loads(invalid.output)["error"]["code"] == "invalid_regex"


@pytest.mark.asyncio
async def test_regex_long_match_preview_and_zero_width_pages_are_truthful(tmp_path):
    registry, repo, _ = registry_for(tmp_path)
    source = repo / "long.txt"
    source.write_text(("a" * 200000) + "END\n\u03b2eta\n", encoding="utf-8")
    result = payload(await registry.execute("file_search", {
        "query": r"a+END", "match": "regex", "mode": "content", "roots": [str(source)],
        "context_chars": 8, "options": {"regex_max_line_bytes": 0},
    }))
    assert result["coverage_complete"]
    assert len(result["matches"]) == 1
    assert result["matches"][0]["character_offset"] == 0
    assert result["matches"][0]["excerpt_scope"] == "bounded_from_match_start"
    assert len(result["matches"][0]["excerpt"]) <= 168

    source.write_bytes("alpha\n\u03b2eta\n".encode("utf-8"))
    query = {"query": "^", "match": "regex", "mode": "content", "roots": [str(source)],
             "max_results": 1}
    offsets = []
    for _ in range(5):
        page = payload(await registry.execute_with_audit_context(
            "file_search", query, audit_context={"request_id": "regex-zero-width"},
        ))
        offsets.extend(row["character_offset"] for row in page["matches"])
        if not page["can_continue"]:
            assert page["coverage_complete"]
            break
        query["cursor"] = page["next_cursor"]
    else:
        pytest.fail("zero-width regex did not finish")
    assert offsets == [0, 6]


@pytest.mark.asyncio
async def test_python_regex_engine_handles_unicode_paging_and_explicit_resource_budget(tmp_path):
    import re

    registry, repo, _ = registry_for(tmp_path)
    source = repo / "unicode.txt"
    content = "\u5f00\u5934 cats 12\n\u72d7 34\ncat 56\n"
    source.write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))
    query = {"query": r"(cats?|\u72d7)\s+\d+".replace(r"\u72d7", "\u72d7"),
             "match": "regex", "mode": "content", "roots": [str(source)],
             "max_results": 1, "options": {"regex_engine": "python", "regex_max_line_bytes": 128}}
    expected = [m.start() for m in re.finditer(query["query"], content, re.IGNORECASE)]
    offsets = []
    for _ in range(8):
        page = payload(await registry.execute_with_audit_context(
            "file_search", query, audit_context={"request_id": "python-regex-pages"},
        ))
        assert page["effective_policy"]["regex_engine"] == "python"
        offsets.extend(row["character_offset"] for row in page["matches"])
        if not page["can_continue"]:
            assert page["coverage_complete"]
            break
        query["cursor"] = page["next_cursor"]
    else:
        pytest.fail("Python regex did not finish")
    assert offsets == expected


@pytest.mark.asyncio
async def test_regex_engine_selection_rejects_invalid_options_and_reports_selected_syntax(tmp_path):
    registry, repo, _ = registry_for(tmp_path)
    (repo / "text.txt").write_text("secretX", encoding="utf-8")
    invalid = await registry.execute("file_search", {
        "query": "secret", "match": "regex", "mode": "content", "roots": [str(repo)],
        "options": {"regex_engine": []},
    })
    assert invalid.is_error and json.loads(invalid.output)["error"]["code"] == "invalid_search"
    if __import__("shutil").which("rg") is not None:
        unsupported = await registry.execute("file_search", {
            "query": r"(?<=secret)X", "match": "regex", "mode": "content", "roots": [str(repo)],
            "options": {"regex_engine": "ripgrep"},
        })
        assert unsupported.is_error and json.loads(unsupported.output)["error"]["code"] == "invalid_regex"


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["auto", "python"])
async def test_line_oriented_regex_reports_explicit_cross_line_pattern(tmp_path, engine):
    registry, repo, _ = registry_for(tmp_path)
    source = repo / "lines.txt"
    source.write_bytes(b"foo\nbar\n")
    result = await registry.execute("file_search", {
        "query": r"foo\nbar", "match": "regex", "mode": "content",
        "roots": [str(source)], "options": {"regex_engine": engine},
    })
    value = json.loads(result.output)
    assert result.is_error and value["error"]["code"] == "invalid_regex"
    assert "line" in value["error"]["message"].lower()

    same_line = payload(await registry.execute("file_search", {
        "query": r"[^\n]+", "match": "regex", "mode": "content",
        "roots": [str(source)], "options": {"regex_engine": engine},
    }))
    assert len(same_line["matches"]) == 2
    assert same_line["coverage"]["regex_matching_scope"] == "single_lf_delimited_lines"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["path", "content"])
async def test_oversized_regex_repeat_is_a_typed_syntax_failure(tmp_path, mode):
    registry, repo, _ = registry_for(tmp_path)
    (repo / "text.txt").write_text("aaa", encoding="utf-8")
    result = await registry.execute("file_search", {
        "query": "a{" + ("9" * 50) + "}", "match": "regex", "mode": mode,
        "roots": [str(repo)], "options": {"regex_engine": "python"},
    })
    assert result.is_error
    assert json.loads(result.output)["error"]["code"] == "invalid_regex"


@pytest.mark.asyncio
async def test_deep_directory_search_and_continuation_exceed_previous_depth_limit(tmp_path):
    registry, repo, _ = registry_for(tmp_path)
    deepest = repo
    for _ in range(140):
        deepest = deepest / 'd'
        deepest.mkdir()
    wanted = deepest / 'needle.txt'
    wanted.write_text('found', encoding='utf-8')
    args = {'query': 'needle', 'roots': [str(repo)], 'max_results': 1}
    first = payload(await registry.execute_with_audit_context(
        'file_search', args, audit_context={'request_id': 'deep-search'}))
    assert [match['path'] for match in first['matches']] == [str(wanted)]
    assert first['can_continue'] and not first['coverage_complete']
    args['cursor'] = first['next_cursor']
    last = payload(await registry.execute_with_audit_context(
        'file_search', args, audit_context={'request_id': 'deep-search'}))
    assert not last['matches'] and last['coverage_complete']
    assert not last['coverage']['errors']


@pytest.mark.asyncio
async def test_expired_cursor_offers_restart_and_preserves_run_binding(tmp_path):
    registry, repo, _ = registry_for(tmp_path, options={
        'file_search': {'continuations': {'ttl_seconds': 900}}})
    file = repo / 'hits.txt'
    file.write_text('hit hit hit', encoding='utf-8')
    args = {'query': 'hit', 'mode': 'content', 'roots': [str(repo)], 'max_results': 1}
    context = {'request_id': 'recovery-run'}
    first = payload(await registry.execute_with_audit_context('file_search', args, audit_context=context))
    token = first['next_cursor']
    args['cursor'] = token
    wrong = await registry.execute_with_audit_context(
        'file_search', args, audit_context={'request_id': 'another-run'})
    assert wrong.is_error and json.loads(wrong.output)['error']['code'] == 'cursor_stale'
    assert token in registry.search_continuations.items
    original = registry.search_continuations.items[token]
    registry.search_continuations.items[token] = (original[0] - 901, *original[1:])
    expired = await registry.execute_with_audit_context('file_search', args, audit_context=context)
    envelope = json.loads(expired.output)
    assert expired.is_error and envelope['error']['code'] == 'cursor_stale'
    recovery = envelope['data']['recovery']
    assert recovery['action'] == 'restart_without_cursor'
    assert not recovery['preserves_unreturned_results']
    assert 'changed files' in recovery['revision']
    assert token not in registry.search_continuations.items
    del args['cursor']
    seen = {
        (match['path'], match['type'], tuple(match['file_signature']),
         match.get('character_offset'), match.get('term'))
        for match in first['matches']
    }
    for _ in range(6):
        page = payload(await registry.execute_with_audit_context(
            'file_search', args, audit_context=context))
        seen.update(
            (match['path'], match['type'], tuple(match['file_signature']),
             match.get('character_offset'), match.get('term'))
            for match in page['matches']
        )
        if not page['can_continue']:
            assert page['coverage_complete']
            break
        args['cursor'] = page['next_cursor']
    else:
        pytest.fail('restarted scan did not finish')
    assert sorted(item[3] for item in seen) == [0, 4, 8]


@pytest.mark.asyncio
@pytest.mark.parametrize('tool', ['file_list', 'log_query'])
async def test_shared_search_wrappers_return_cursor_recovery_without_crashing(tmp_path, tool):
    registry, repo, _ = registry_for(tmp_path)
    (repo / 'one.txt').write_text('hit hit hit', encoding='utf-8')
    (repo / 'two.txt').write_text('hit', encoding='utf-8')
    if tool == 'file_list':
        args = {'path': str(repo), 'recursive': True, 'max_results': 1}
    else:
        args = {'path': str(repo / 'one.txt'), 'terms': ['hit'], 'max_results': 1}
    first = await registry.execute_with_audit_context(
        tool, args, audit_context={'request_id': 'wrapper-original'})
    assert not first.is_error, first.output
    data = json.loads(first.output.splitlines()[-1]) if tool == 'file_list' else payload(first)
    args['cursor'] = data['next_cursor']
    second = await registry.execute_with_audit_context(
        tool, args, audit_context={'request_id': 'wrapper-other'})
    assert second.is_error and 'restart_without_cursor' in second.output
    assert 'unexpected failure' not in second.output


@pytest.mark.asyncio
async def test_cursor_default_has_no_time_expiry_during_registry_lifetime(tmp_path):
    registry, repo, _ = registry_for(tmp_path)
    (repo / 'hits.txt').write_text('hit hit', encoding='utf-8')
    args = {'query': 'hit', 'mode': 'content', 'roots': [str(repo)], 'max_results': 1}
    context = {'request_id': 'long-review'}
    first = payload(await registry.execute_with_audit_context('file_search', args, audit_context=context))
    assert first['effective_policy']['continuations']['ttl_seconds'] == 0
    token = first['next_cursor']
    original = registry.search_continuations.items[token]
    registry.search_continuations.items[token] = (original[0] - 3600, *original[1:])
    args['cursor'] = token
    second = payload(await registry.execute_with_audit_context('file_search', args, audit_context=context))
    assert second['matches'][0]['character_offset'] == 4


def test_continuation_capacity_is_configurable_and_never_exceeds_selected_byte_budget():
    from tools.file_search import Continuations

    store = Continuations()
    store.configure({'continuations': {'max_entries': 0, 'max_bytes': 0, 'max_state_bytes': 0}})
    tokens = [store.put('run', {'ordinal': index}, 'op') for index in range(70)]
    assert all(tokens) and store.get(tokens[0], 'run')[0]['ordinal'] == 0
    store.configure({'continuations': {'max_entries': 1, 'max_bytes': 100, 'max_state_bytes': 0}})
    assert len(store.items) == 1 and store.bytes <= 100
    assert store.put('run', {'oversized': 'x' * 200}, 'op') is None
    assert len(store.items) == 1
    token = store.put('run', {'new': 1}, 'op')
    assert token and len(store.items) == 1 and store.bytes <= 100
    with pytest.raises(ValueError):
        store.get(tokens[0], 'run')
    with pytest.raises(ValueError):
        store.configure({'continuations': {'ttl_seconds': -1}})


@pytest.mark.asyncio
async def test_long_literal_query_can_page_hits_beyond_previous_1024_character_limit(tmp_path):
    registry, repo, _ = registry_for(tmp_path)
    term = 'A' * 2100
    content = 'start ' + term + '!!' + term + ' end'
    source = repo / 'long-query.txt'
    source.write_bytes(content.encode('utf-8'))
    args = {'query': term, 'mode': 'content', 'roots': [str(source)],
            'max_results': 1, 'context_chars': 8}
    offsets = []
    for _ in range(5):
        page = payload(await registry.execute_with_audit_context(
            'file_search', args, audit_context={'request_id': 'long-query'}))
        offsets.extend(match['character_offset'] for match in page['matches'])
        if not page['can_continue']:
            assert page['coverage_complete']
            break
        args['cursor'] = page['next_cursor']
    else:
        pytest.fail('long query did not finish')
    assert offsets == [6, 2108]


@pytest.mark.asyncio
async def test_long_path_regex_compiles_in_managed_worker(tmp_path):
    registry, repo, _ = registry_for(tmp_path)
    (repo / 'target.txt').touch()
    pattern = '(?:x?)' * 250 + r'target\.txt$'
    assert len(pattern) > 1024
    page = payload(await registry.execute('file_search', {
        'query': pattern, 'mode': 'path', 'match': 'regex', 'roots': [str(repo)]}))
    assert [Path(match['path']).name for match in page['matches']] == ['target.txt']


async def _read_scope_metadata(registry, reference, context):
    parts = []
    offset = 0
    for _ in range(20):
        result = await registry.execute_with_audit_context(
            'tool_output_read', {'reference': reference, 'offset': offset}, audit_context=context)
        assert not result.is_error, result.output
        page = payload(result)
        parts.append(page['text'])
        if page['complete']:
            return json.loads(''.join(parts))
        offset = page['next_offset']
    pytest.fail('scope metadata did not finish')


@pytest.mark.asyncio
async def test_large_query_pages_scope_metadata_and_matches_with_run_binding(tmp_path):
    registry, repo, _ = registry_for(tmp_path)
    term = 'Z' * 6000
    content = 'start ' + term + '!' + term
    source = repo / 'large-query.txt'
    source.write_bytes(content.encode('utf-8'))
    context = {'request_id': 'large-query', 'request_tool_allowlist': ['file_search']}
    args = {'query': term, 'mode': 'content', 'roots': [str(source)],
            'max_results': 1, 'context_chars': 8}
    offsets = []
    references = []
    for _ in range(5):
        page = payload(await registry.execute_with_audit_context(
            'file_search', args, audit_context=context))
        assert page['query'] is None
        reference = page['scope_reference']['reference']
        references.append(reference)
        metadata = await _read_scope_metadata(registry, reference, context)
        assert metadata['query'] == term
        assert metadata['selected_roots'][0]['path'] == str(source)
        assert page['selected_roots'][0]['root_id'] == metadata['selected_roots'][0]['root_id']
        for match in page['matches']:
            assert match['term_ref'] == 'query' and match['excerpt_truncated']
            assert match['root'] == metadata['selected_roots'][0]['root_id']
            offsets.append(match['character_offset'])
        denied = await registry.execute_with_audit_context(
            'tool_output_read', {'reference': reference, 'offset': 0},
            audit_context={'request_id': 'other', 'request_tool_allowlist': ['file_search']})
        assert denied.is_error
        if not page['can_continue']:
            assert page['coverage_complete']
            break
        args['cursor'] = page['next_cursor']
    else:
        pytest.fail('large query did not finish')
    assert offsets == [6, 6007]
    assert len(set(references)) == 1


@pytest.mark.asyncio
async def test_large_scope_metadata_and_trimmed_matches_resume_without_loss(tmp_path, monkeypatch):
    import tools.file_search as search

    registry, repo, _ = registry_for(tmp_path)
    expected = set()
    for index in range(30):
        file = repo / (('name-' * 20) + str(index) + '.txt')
        file.touch()
        expected.add(str(file))
    exclusions = ['never-' + str(index) + '-' + 'q' * 150 for index in range(28)]
    args = {'query': '.txt', 'roots': [str(repo)], 'exclude': exclusions, 'max_results': 100}
    context = {'request_id': 'large-filters'}
    monkeypatch.setattr(search, 'MAX_ENVELOPE_CHARS', 6000)
    found = []
    saw_output_budget = False
    for _ in range(12):
        page = payload(await registry.execute_with_audit_context('file_search', args, audit_context=context))
        metadata = await _read_scope_metadata(registry, page['scope_reference']['reference'], context)
        assert metadata['filters']['exclude'] == exclusions
        assert metadata['selected_roots'][0]['path'] == str(repo)
        found.extend(match['path'] for match in page['matches'])
        saw_output_budget |= page['stop_reason'] == 'output_budget'
        if not page['can_continue']:
            assert page['coverage_complete']
            break
        args['cursor'] = page['next_cursor']
    else:
        pytest.fail('large scope did not finish')
    assert saw_output_budget
    assert set(found) == expected and len(found) == len(expected)


@pytest.mark.asyncio
async def test_large_scope_without_run_bound_retention_reports_limitation(tmp_path):
    registry, repo, _ = registry_for(tmp_path)
    result = await registry.execute('file_search', {
        'query': 'Q' * 6000, 'mode': 'path', 'roots': [str(repo)]})
    assert result.is_error
    envelope = json.loads(result.output)
    assert envelope['error']['code'] == 'scope_output_budget'
    assert 'Run-bound output retention' in envelope['error']['message']


@pytest.mark.asyncio
async def test_literal_query_larger_than_read_chunk_preserves_all_hits(tmp_path):
    registry, repo, _ = registry_for(tmp_path)
    term = 'Q' * 70000
    content = 'start ' + term + '!' + term + ' end'
    source = repo / 'chunk-boundary.txt'
    source.write_bytes(content.encode('utf-8'))
    context = {'request_id': 'large-chunk-query'}
    args = {'query': term, 'mode': 'content', 'roots': [str(source)],
            'max_results': 1, 'context_chars': 4}
    offsets = []
    for _ in range(6):
        page = payload(await registry.execute_with_audit_context('file_search', args, audit_context=context))
        offsets.extend(item['character_offset'] for item in page['matches'])
        if not page['can_continue']:
            assert page['coverage_complete']
            break
        args['cursor'] = page['next_cursor']
    else:
        pytest.fail('large chunk-spanning query did not finish')
    assert offsets == [6, 70007]
