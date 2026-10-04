"""Scoped discovery contract, bounded continuation and foreground control."""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sys
import time
from collections import OrderedDict
from pathlib import Path

from tools.tool_activity import CURRENT_ACTIVITY, broad_scope_advisory

MAX_ENVELOPE_CHARS = 16000


class Continuations:
    """Ephemeral Function-local derived state; never a filesystem index."""
    def __init__(self):
        self.items = OrderedDict()
        self.bytes = 0

    def put(self, binding, state, operation_id):
        encoded = json.dumps(state, ensure_ascii=False)
        size = len(encoded.encode('utf-8'))
        if size > 3 * 1024 * 1024:
            return None
        now = time.monotonic()
        for key in list(self.items):
            if now - self.items[key][0] > 900:
                self.drop(key)
        while self.items and (len(self.items) >= 64 or self.bytes + size > 8 * 1024 * 1024):
            self.drop(next(iter(self.items)))
        token = secrets.token_urlsafe(32)
        self.items[token] = (now, binding, state, operation_id, size)
        self.bytes += size
        return token

    def drop(self, key):
        row = self.items.pop(key, None)
        if row:
            self.bytes -= row[4]

    def get(self, key, binding):
        row = self.items.get(key)
        if row is None or row[1] != binding or time.monotonic() - row[0] > 900:
            raise ValueError('cursor stale, expired or bound to another query/Run/Agent; choose a new query explicitly')
        return row[2], row[3]


def outcome(status, data=None, error=None, warning=None):
    return json.dumps({'status': status, 'effect': 'observed' if data else 'no_change',
                       'data': data, 'error': error, 'warning': warning},
                      ensure_ascii=False, separators=(',', ':'))


def failed(message, *, status='failed', code='invalid_search'):
    from tools.builtins import BuiltinExecutionResult
    return BuiltinExecutionResult(outcome(status, error={'code': code, 'message': message,
                                                         'retryable': False}),
                                  {'search_outcome': status, 'unavailable': status == 'unavailable'})


def normalize_query(args: dict, *, defaults: list[dict], read_roots, cwd: Path,
                    allow_wsl_windows_drive_paths=False, operation='file_search'):
    from tools.builtins import _resolve_path
    query = args.get('query')
    if not isinstance(query, str) or not query.strip() or len(query) > 1024:
        raise ValueError('query must be a non-empty string of at most 1024 characters')
    mode = args.get('mode', 'path')
    match = args.get('match', 'literal')
    profile = args.get('profile', 'project')
    if mode not in {'path', 'content'} or match not in {'literal', 'glob', 'regex'}:
        raise ValueError('invalid mode or match')
    if match == 'regex':
        raise NotImplementedError('Regex search backend is unavailable; use literal/path or an appropriate scoped Shell command')
    if mode == 'content' and match != 'literal':
        raise ValueError('content search supports literal matching')
    if profile not in {'project', 'expanded'}:
        raise ValueError('profile must be project or expanded')
    raw_roots = args.get('roots')
    if raw_roots is not None and (not isinstance(raw_roots, list) or not raw_roots or len(raw_roots) > 32):
        raise ValueError('roots must contain 1 to 32 exact paths')
    selected = []
    entries = defaults if raw_roots is None else [
        {'path': value, 'provenance': 'tool-explicit'} for value in raw_roots]
    for item in entries:
        raw = item.get('path')
        if not isinstance(raw, str) or not raw or len(raw) > 4096:
            raise ValueError('root must be a non-empty path of at most 4096 characters')
        # Missing frozen Workzones are reported, never replaced by a parent root.
        if item.get('available') is False and raw_roots is None:
            path = Path(raw)
        else:
            path = _resolve_path(raw, read_roots, cwd,
                                 allow_wsl_windows_drive_paths=allow_wsl_windows_drive_paths)
        if not any(entry['path'] == str(path) for entry in selected):
            selected.append({**item, 'path': str(path)})
    if not selected:
        raise ValueError('no preferred search roots are available')
    options = args.get('options') or {}
    if not isinstance(options, dict):
        raise ValueError('options must be an object')
    bools = {'case_sensitive': args.get('case_sensitive', False),
             'include_hidden': args.get('include_hidden', False),
             'follow_links': options.get('follow_links', False),
             'cross_filesystems': options.get('cross_filesystems', False),
             'recursive': args.get('_recursive', True)}
    if any(not isinstance(value, bool) for value in bools.values()):
        raise ValueError('search boolean options must be true or false')
    filters = {}
    for key in ('include', 'exclude'):
        value = args.get(key, [])
        if not isinstance(value, list) or len(value) > 32 or any(
                not isinstance(p, str) or not p or len(p) > 512 for p in value):
            raise ValueError(f'{key} must contain at most 32 non-empty globs')
        filters[key] = value
    size = args.get('max_results', 50)
    context = args.get('context_chars', 160)
    if type(size) is not int or not 1 <= size <= 200 or type(context) is not int or not 0 <= context <= 2000:
        raise ValueError('max_results must be 1..200; context_chars must be 0..2000')
    return {'query': query, 'mode': mode, 'match': match, 'profile': profile,
            'selected_roots': selected, **bools, **filters,
            'max_results': size, 'context_chars': context, 'operation': operation}


async def run_worker(config):
    from tools.builtins import (_bash_process_kwargs, _bash_process_group_id,
                                _cleanup_bash_process, _shield_bash_cleanup)
    activity = CURRENT_ACTIVITY.get()
    proc = None
    drain_task = None
    group = None
    try:
        if activity:
            activity.update(selected_roots=[entry['path'] for entry in config['selected_roots']],
                            scope_provenance='verified', operation_type=config['operation'])
            await activity.record_start()
        proc = await asyncio.create_subprocess_exec(
            sys.executable, '-B', str(Path(__file__).with_name('search_worker.py')),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=4 * 1024 * 1024,
            **_bash_process_kwargs())
        group = _bash_process_group_id(proc)
        if activity:
            activity.watch_process(proc)
            activity.update(state='running', liveness='alive',
                            selected_roots=[entry['path'] for entry in config['selected_roots']],
                            scope_provenance='verified', operation_type=config['operation'])
        result = None
        partial = None
        stderr_tail = bytearray()
        async def read_out():
            nonlocal result, partial
            while line := await proc.stdout.readline():
                if len(line) > 4 * 1024 * 1024:
                    raise ValueError('search worker record exceeds transport budget')
                event = json.loads(line)
                if event.get('kind') == 'progress':
                    partial = event
                    if activity:
                        activity.update(counters=event['counters'], worker_response=True)
                elif event.get('kind') == 'result':
                    result = event['data']
        async def read_err():
            while chunk := await proc.stderr.read(32768):
                stderr_tail.extend(chunk)
                if len(stderr_tail) > 4096:
                    del stderr_tail[:-4096]
        async def drain():
            await asyncio.gather(read_out(), read_err())
            await proc.wait()
            return b'', bytes(stderr_tail)
        drain_task = asyncio.create_task(drain())
        proc.stdin.write(json.dumps(config, ensure_ascii=False).encode('utf-8') + b'\n')
        await proc.stdin.drain()
        proc.stdin.close()
        deadline = config.get('deadline')
        if deadline:
            done, _ = await asyncio.wait({drain_task}, timeout=max(0.01, deadline-time.time()))
            if drain_task not in done:
                cleanup = await _shield_bash_cleanup(_cleanup_bash_process(proc, drain_task, pgid=group))
                if result is None:
                    counters = dict(activity.snapshot['counters']) if activity else {}
                    result = {'matches': (partial or {}).get('matches', []),
                              'counters': (partial or {}).get('counters', counters),
                              'coverage': (partial or {}).get('coverage', {'roots': []}),
                              'scope_exhausted': False, 'coverage_complete': False,
                              'stop_reason': 'deadline', 'warnings': ['Scan stopped before completing coverage'],
                              'state': None}
                result['cleanup'] = cleanup
                return result
        await asyncio.shield(drain_task)
        if proc.returncode != 0 or result is None:
            raise ValueError('search worker failed: ' + stderr_tail.decode('utf-8', 'replace')[-2048:])
        result['cleanup'] = {'status': 'normal_completion', 'process_reaped': True,
                             'group_alive': False, 'errors': []}
        return result
    except asyncio.CancelledError as exc:
        if activity:
            await activity.finish('cancelling', stop_reason='interrupted')
        cleanup = (await _shield_bash_cleanup(_cleanup_bash_process(proc, drain_task, pgid=group))
                   if proc is not None and drain_task is not None else
                   {'status': 'not_started', 'process_reaped': True, 'errors': []})
        setattr(exc, 'hashi_tool_details', {'stop_reason': 'interrupted', 'foreground_cleanup': cleanup,
            'selected_roots': config['selected_roots'], 'coverage_complete': False,
            'observed_counters': dict(activity.snapshot['counters']) if activity else {}})
        raise
    except BaseException as exc:
        if proc is not None and drain_task is not None:
            cleanup = await _shield_bash_cleanup(_cleanup_bash_process(proc, drain_task, pgid=group))
            setattr(exc, 'hashi_tool_details', {'stop_reason': 'io_error', 'foreground_cleanup': cleanup})
        raise


async def execute_file_search(args, *, read_roots, cwd, search_scope, audit_context,
                              continuations: Continuations, options=None,
                              protected_read_paths=(), operation='file_search'):
    from tools.builtins import BuiltinExecutionResult
    options = options or {}
    if operation == 'file_search' and options.get('enabled') is False:
        return failed('Scoped search is disabled', status='unavailable')
    defaults = search_scope.get('preferred_roots') or [
        {'path': str(cwd.resolve()), 'provenance': 'execution-cwd'}]
    try:
        config = normalize_query(args, defaults=defaults, read_roots=read_roots, cwd=cwd,
                                 allow_wsl_windows_drive_paths=audit_context.get('allow_wsl_windows_drive_paths', False),
                                 operation=operation)
    except NotImplementedError as exc:
        return failed(str(exc), status='unavailable', code='regex_unavailable')
    except (ValueError, OSError) as exc:
        return failed(str(exc))
    config.update(authorized_roots=[str(root) for root in read_roots],
                  protected_read_paths=list(map(str, protected_read_paths)),
                  match_budget=8000)
    scope_size = len(json.dumps({'query': config['query'], 'roots': config['selected_roots'],
        'filters': {key: config[key] for key in ('include', 'exclude')}}, ensure_ascii=False))
    config['match_budget'] = min(8000, MAX_ENVELOPE_CHARS - scope_size * 2 - 4000)
    if config['match_budget'] < 1024:
        return failed('Scope/filter metadata exceeds the output budget; narrow the roots or filters',
                      code='scope_output_budget')
    if args.get('_terms'):
        config['terms'] = args['_terms']
    # Bound to trusted identity, frozen scope, query and this Function generation.
    binding = hashlib.sha256(json.dumps({
        'query': config, 'owner': search_scope.get('owner_id'),
        'agent': audit_context.get('agent_name'),
        'request': audit_context.get('request_id') or audit_context.get('task_id'),
        'scope_revision': search_scope.get('workzone_revision'),
        'generation': audit_context.get('function_generation'),
    }, sort_keys=True).encode()).hexdigest()
    activity = CURRENT_ACTIVITY.get()
    operation_id = activity.operation_id if activity else secrets.token_hex(16)
    cursor = args.get('cursor')
    if cursor:
        try:
            config['state'], operation_id = continuations.get(cursor, binding)
            continuations.drop(cursor)
            if activity:
                activity.operation_id = operation_id
        except ValueError as exc:
            return failed(str(exc), code='cursor_stale')
    timeout = options.get('timeout_seconds')
    if timeout is not None:
        try:
            timeout = float(timeout)
            if not 0 < timeout < float('inf'):
                raise ValueError()
            config['deadline'] = time.time() + timeout
        except (TypeError, ValueError):
            return failed('Configured search deadline must be a positive finite number')
    started = time.monotonic()
    try:
        result = await run_worker(config)
    except (ValueError, OSError) as exc:
        result = failed(str(exc), code='worker_failure')
        return BuiltinExecutionResult(result.output, {**result.details,
                                      **getattr(exc, 'hashi_tool_details', {})})
    state = result.pop('state')
    complete = result['coverage_complete']
    result.update(operation_id=operation_id, mode=config['mode'], query=config['query'],
                  selected_roots=config['selected_roots'],
                  workzone_revision=search_scope.get('workzone_revision'),
                  can_continue=state is not None, next_cursor='x' * 43 if state else None,
                  elapsed_ms=int((time.monotonic()-started)*1000),
                  effective_policy={key: config[key] for key in (
                      'profile', 'include', 'exclude', 'include_hidden', 'follow_links', 'cross_filesystems')})
    result['coverage']['excluded_content'] = 'Containers/media/binary; ordinary UTF-8 text only' if config['mode'] == 'content' else 'No file bodies read'
    result['coverage']['default_directory_exclusions'] = (
        sorted(__import__('tools.search_worker', fromlist=['PROJECT_EXCLUDED_DIRS']).PROJECT_EXCLUDED_DIRS)
        if config['profile'] == 'project' else [])
    if broad_scope_advisory([entry['path'] for entry in config['selected_roots']]):
        result['warnings'].append('Broad selected roots may take longer; scope is advisory, not denied')
    status = 'success' if complete else 'partial'
    if result['stop_reason'] == 'cursor_stale' or all(
            root.get('failed') for root in result['coverage']['roots']) and result['coverage']['roots']:
        status = 'failed'
    error = None
    if status == 'failed':
        error = {'code': result['stop_reason'], 'message': 'No complete valid search coverage; inspect coverage errors', 'retryable': False}
    # Preserve scope/coverage first, then budget individual matches. Never cut JSON.
    output = outcome(status, result, error=error)
    removed = []
    # Reserve room for the final cursor, stop reason and capacity warning.
    payload_budget = MAX_ENVELOPE_CHARS - 1024
    while len(output) > payload_budget and result['matches']:
        removed.insert(0, result['matches'].pop())
        output = outcome(status, result, error=error)
    while len(output) > payload_budget and result['coverage']['errors']:
        result['coverage']['errors'].pop()
        output = outcome(status, result, error=error)
    if len(output) > payload_budget:
        return failed('Selected scope metadata exceeds the structured output budget; narrow the roots',
                      code='scope_output_budget')
    if removed:
        state = state or {'roots': [{'path': root['path'], 'done': True, 'initialized': True,
                                     'failed': root['failed'], 'stack': [], 'pending_file': None}
                                    for root in result['coverage']['roots']],
                         'counters': dict(result['counters']), 'errors': result['coverage']['errors'],
                         'warnings': result['warnings'], 'turn': 0}
        pending = ([state.pop('pending_match')] if 'pending_match' in state else [])
        state['pending_matches'] = removed + pending + state.get('pending_matches', [])
        result['counters']['matches'] -= len(removed)
        state['counters']['matches'] = result['counters']['matches']
        result.update(coverage_complete=False, scope_exhausted=False, stop_reason='output_budget')
        complete, status = False, 'partial'
    next_cursor = continuations.put(binding, state, operation_id) if state else None
    result.update(can_continue=next_cursor is not None, next_cursor=next_cursor)
    if state and next_cursor is None:
        result['warnings'].append('Continuation capacity exceeded; no automatic restart')
    output = outcome(status, result, error=error)
    return BuiltinExecutionResult(output, {'search_outcome': status,
                                          'foreground_cleanup': result['cleanup'],
                                          'stop_reason': result['stop_reason'],
                                          'coverage_complete': complete,
                                          'selected_roots': result['selected_roots'],
                                          'coverage': result['coverage']})
