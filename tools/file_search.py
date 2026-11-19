"""Scoped discovery contract, bounded continuation and foreground control."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import shutil
import secrets
import sys
import time
from collections import OrderedDict
from pathlib import Path

from tools.tool_activity import CURRENT_ACTIVITY, broad_scope_advisory

MAX_ENVELOPE_CHARS = 16000

# Engineering defaults for one local search operation. These are configuration
# defaults, not policy or Agent limits: an installation can override each
# budget through the existing per-tool configuration, and an individual call
# may request a longer window when the task justifies it (optionally capped by
# a configured timeout_max_seconds). No default constrains the Agent Run,
# conversation, or task.
DEFAULT_SEARCH_BUDGET_SECONDS = {
    "file_search": 45.0,
    "file_list": 15.0,
    "file_list_recursive": 45.0,
    "log_query": 45.0,
    "repo_discover": 30.0,
}

# The parent waits this long past the cooperative deadline for the worker to
# serialize its deadline-limited result (matches, coverage and continuation
# state) before falling back to emergency process-tree cleanup. Emergency
# cleanup cannot preserve a continuation cursor that was never captured.
DEFAULT_CLEANUP_GRACE_SECONDS = 2.0


def resolve_search_budget(args, *, options, operation, recursive=None):
    """Resolve the cooperative execution budget for one search operation.

    Precedence: an explicit per-call ``timeout_seconds`` request wins; then a
    configured ``timeout_seconds`` (existing behaviour, including the Smart
    Registry foreground fuse); then the per-operation engineering default.
    A configured ``timeout_max_seconds`` caps explicit per-call requests only;
    configured deadlines and defaults continue to apply unchanged.

    Returns ``(deadline_seconds, policy)`` where policy describes the decision
    for the structured result. Raises ValueError on invalid values.
    """
    options = options or {}
    if recursive is None:
        recursive = bool(args.get("_recursive")) if isinstance(args, dict) else False
    cap = None
    if options.get("timeout_max_seconds") is not None:
        try:
            candidate = float(options["timeout_max_seconds"])
            if 0 < candidate < float("inf"):
                cap = candidate
        except (TypeError, ValueError):
            pass
    requested = args.get("timeout_seconds") if isinstance(args, dict) else None
    if requested is not None:
        try:
            requested = float(requested)
            if not 0 < requested < float("inf"):
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("timeout_seconds must be a positive finite number of seconds")
        if cap is not None and requested > cap:
            policy = {"source": "per_call_request", "requested_seconds": requested,
                      "capped": True, "timeout_max_seconds": cap}
            return cap, policy
        policy = {"source": "per_call_request", "requested_seconds": requested,
                  "capped": False, "timeout_max_seconds": cap}
        return requested, policy
    if options.get("timeout_seconds") is not None:
        try:
            configured = float(options["timeout_seconds"])
            if not 0 < configured < float("inf"):
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("Configured search deadline must be a positive finite number")
        policy = {"source": "configured_timeout", "requested_seconds": configured,
                  "capped": False, "timeout_max_seconds": cap}
        return configured, policy
    default_key = operation
    if operation == "file_list" and recursive:
        default_key = "file_list_recursive"
    default = DEFAULT_SEARCH_BUDGET_SECONDS.get(default_key)
    if default is None:
        return None, {"source": "none", "default_budget_seconds": None}
    policy = {"source": "engineering_default", "default_budget_seconds": default,
              "capped": False, "timeout_max_seconds": cap}
    return default, policy



class InvalidRegex(ValueError):
    pass


class Continuations:
    """Ephemeral Function-local derived state; never a filesystem index."""
    def __init__(self):
        self.items = OrderedDict()
        self.bytes = 0
        self.policy = {'max_entries': 64, 'max_bytes': 8 * 1024 * 1024,
                       'max_state_bytes': 3 * 1024 * 1024, 'ttl_seconds': 0}

    def configure(self, options):
        selected = options.get('continuations', {})
        if not isinstance(selected, dict):
            raise ValueError('file_search continuations configuration must be an object')
        defaults = {'max_entries': 64, 'max_bytes': 8 * 1024 * 1024,
                    'max_state_bytes': 3 * 1024 * 1024, 'ttl_seconds': 0}
        if set(selected) - set(defaults):
            raise ValueError('unknown file_search continuations configuration')
        policy = {**defaults, **selected}
        for key in ('max_entries', 'max_bytes', 'max_state_bytes'):
            if type(policy[key]) is not int or policy[key] < 0:
                raise ValueError(f'continuations {key} must be a nonnegative integer')
        ttl = policy['ttl_seconds']
        if (type(ttl) not in (int, float) or ttl < 0 or
                type(ttl) is float and not math.isfinite(ttl)):
            raise ValueError('continuations ttl_seconds must be a nonnegative finite number')
        self.policy = policy
        now = time.monotonic()
        for key, row in list(self.items.items()):
            if ((ttl and now - row[0] > ttl) or
                    (policy['max_state_bytes'] and row[4] > policy['max_state_bytes'])):
                self.drop(key)
        while self.items and ((policy['max_entries'] and len(self.items) > policy['max_entries']) or
                              (policy['max_bytes'] and self.bytes > policy['max_bytes'])):
            self.drop(next(iter(self.items)))

    def put(self, binding, state, operation_id):
        encoded = json.dumps(state, ensure_ascii=False)
        size = len(encoded.encode('utf-8'))
        maximum_state = self.policy['max_state_bytes']
        maximum_bytes = self.policy['max_bytes']
        maximum_entries = self.policy['max_entries']
        if (maximum_state and size > maximum_state) or (maximum_bytes and size > maximum_bytes):
            return None
        now = time.monotonic()
        ttl = self.policy['ttl_seconds']
        if ttl:
            for key in list(self.items):
                if now - self.items[key][0] > ttl:
                    self.drop(key)
        while self.items and ((maximum_entries and len(self.items) >= maximum_entries) or
                              (maximum_bytes and self.bytes + size > maximum_bytes)):
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
        if row is None or row[1] != binding:
            raise ValueError('Cursor is unavailable for this query/Run/Agent or no longer retained')
        ttl = self.policy['ttl_seconds']
        if ttl and time.monotonic() - row[0] > ttl:
            self.drop(key)
            raise ValueError('Cursor has expired')
        return row[2], row[3]


class RunContinuations:
    """Private Run-scoped scanner checkpoints, retained until the Run ends.

    Checkpoints are immutable so a lost response or cancelled scan can retry the
    same cursor. The existing Run output store owns authorization and cleanup.
    """
    def __init__(self, store, run_binding):
        self.store = store
        self.run_binding = run_binding
        self.retention = store.retention
        self.policy = {}

    def configure(self, options):
        probe = Continuations()
        probe.configure(options)
        selected = options.get('continuations', {})
        self.policy = dict(probe.policy)
        # Disk replaces the process-wide memory LRU. Keep all active cursors by
        # default; deployments may select explicit per-Run disk budgets.
        for key in ('max_entries', 'max_bytes'):
            if key not in selected:
                self.policy[key] = 0

    def put(self, binding, state, operation_id):
        expires = time.time() + self.policy['ttl_seconds'] if self.policy['ttl_seconds'] else 0
        payload = {'binding': binding, 'state': state, 'operation_id': operation_id, 'expires_at': expires}
        chunks = json.JSONEncoder(ensure_ascii=False, separators=(',', ':')).iterencode(payload)
        try:
            return self.store.retain_search_continuation(
                self.run_binding, chunks, max_entries=self.policy['max_entries'],
                max_bytes=self.policy['max_bytes'], max_state_bytes=self.policy['max_state_bytes'],
                expires_at=expires,
            )
        except (OSError, TimeoutError, ValueError):
            return None

    def get(self, key, binding):
        try:
            value = json.loads(self.store.read_search_continuation(
                self.run_binding, key, max_state_bytes=self.policy['max_state_bytes']))
            if (not isinstance(value, dict) or value.get('binding') != binding
                    or not isinstance(value.get('state'), dict)
                    or not isinstance(value.get('operation_id'), str)
                    or type(value.get('expires_at')) not in (int, float)):
                raise ValueError('invalid checkpoint')
            if value['expires_at'] and value['expires_at'] <= time.time():
                raise ValueError('expired checkpoint')
            return value['state'], value['operation_id']
        except (OSError, TimeoutError, TypeError, ValueError):
            raise ValueError('Cursor is unavailable for this query/Run/Agent or no longer retained') from None

    def drop(self, key):
        # Preserve the exact checkpoint through response loss and cancellation;
        # the existing store removes all Run checkpoints after terminal state.
        pass


def outcome(status, data=None, error=None, warning=None):
    return json.dumps({'status': status, 'effect': 'observed' if data else 'no_change',
                       'data': data, 'error': error, 'warning': warning},
                      ensure_ascii=False, separators=(',', ':'))


def failed(message, *, status='failed', code='invalid_search', data=None):
    from tools.builtins import BuiltinExecutionResult
    return BuiltinExecutionResult(outcome(status, data=data, error={'code': code, 'message': message,
                                                                    'retryable': False}),
                                  {'search_outcome': status, 'unavailable': status == 'unavailable'})


def restart_guidance():
    return {
        'action': 'restart_without_cursor',
        'instruction': 'Repeat the same search without cursor and compare results with prior pages',
        'deduplicate_by': ['path', 'type', 'file_signature', 'character_offset', 'term'],
        'revision': 'A restart rescans the current filesystem; changed files can produce different results',
        'preserves_unreturned_results': False,
    }


def normalize_query(args: dict, *, defaults: list[dict], read_roots, cwd: Path,
                    allow_wsl_windows_drive_paths=False, operation='file_search'):
    from tools.builtins import _resolve_path
    query = args.get('query')
    if not isinstance(query, str) or not query.strip():
        raise ValueError('query must be a non-empty string')
    mode = args.get('mode', 'path')
    match = args.get('match', 'literal')
    profile = args.get('profile', 'project')
    options = args.get('options') or {}
    if not isinstance(options, dict):
        raise ValueError('options must be an object')
    regex_engine = options.get('regex_engine', 'auto')
    if not isinstance(regex_engine, str) or regex_engine not in {'auto', 'ripgrep', 'python'}:
        raise ValueError('regex_engine must be auto, ripgrep or python')
    if mode not in {'path', 'content'} or match not in {'literal', 'glob', 'regex'}:
        raise ValueError('invalid mode or match')
    if mode == 'content' and match not in {'literal', 'regex'}:
        raise ValueError('content search supports literal or regex matching')
    regex_backend = None
    if match == 'regex' and mode == 'content':
        if '\n' in query or '\r' in query:
            raise InvalidRegex('Content regex is line-oriented and cannot contain a newline')
        regex_backend = shutil.which('rg') if regex_engine != 'python' else None
        if regex_engine == 'ripgrep' and regex_backend is None:
            raise NotImplementedError('The selected ripgrep regex engine is not installed')
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
    regex_line_budget = options.get('regex_max_line_bytes')
    if regex_line_budget is not None and (
        type(regex_line_budget) is not int or regex_line_budget < 0
    ):
        raise ValueError('regex_max_line_bytes must be a nonnegative integer')
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
            'max_results': size, 'context_chars': context, 'operation': operation,
            **({'regex_content': True, 'regex_backend': regex_backend,
                'regex_engine_requested': regex_engine, 'regex_line_budget_requested': regex_line_budget}
               if match == 'regex' and mode == 'content' else {})}


async def run_worker(config):
    request = json.dumps(config, ensure_ascii=False).encode('utf-8') + b'\n'
    state_capacity = config.get('state_capacity_bytes', 3 * 1024 * 1024)
    if type(state_capacity) is not int or state_capacity < 0:
        raise ValueError('Search continuation capacity must be a nonnegative integer')
    # Result records contain the selected state plus a small coverage envelope.
    # A configured zero deliberately removes that capacity bound.
    record_limit = state_capacity + 2 * 1024 * 1024 if state_capacity else None
    stream_limit = max(4 * 1024 * 1024, record_limit) if record_limit else sys.maxsize
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
            stderr=asyncio.subprocess.PIPE, limit=stream_limit,
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
                if record_limit and len(line) > record_limit:
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
        proc.stdin.write(request)
        await proc.stdin.drain()
        proc.stdin.close()
        deadline = config.get('deadline')
        if deadline:
            grace = config.get('cleanup_grace_seconds', DEFAULT_CLEANUP_GRACE_SECONDS)
            try:
                grace = float(grace)
                if not 0 <= grace < float('inf'):
                    grace = DEFAULT_CLEANUP_GRACE_SECONDS
            except (TypeError, ValueError):
                grace = DEFAULT_CLEANUP_GRACE_SECONDS
            # Cooperative window: the worker stops scheduling new work at the
            # same deadline, then serializes its partial result (continuation
            # state included). Wait until the worker deadline itself...
            done, _ = await asyncio.wait({drain_task}, timeout=max(0.01, deadline-time.time()))
            if drain_task not in done:
                # ...plus a short cleanup grace so a deadline-limited result
                # that is already being written is collected, not destroyed.
                done, _ = await asyncio.wait({drain_task}, timeout=max(0.0, grace))
                if drain_task not in done:
                    # Emergency path: the worker could not finish its result.
                    # Terminate the owned tree; never invent a continuation
                    # cursor that was never captured.
                    cleanup = await _shield_bash_cleanup(_cleanup_bash_process(proc, drain_task, pgid=group))
                    if result is None:
                        counters = dict(activity.snapshot['counters']) if activity else {}
                        result = {'matches': (partial or {}).get('matches', []),
                                  'counters': (partial or {}).get('counters', counters),
                                  'coverage': (partial or {}).get('coverage', {'roots': [], 'errors': []}),
                                  'scope_exhausted': False, 'coverage_complete': False,
                                  'stop_reason': 'deadline', 'state': None,
                                  'continuation_lost': True,
                                  'warnings': [
                                      'Scan stopped before completing coverage',
                                      ('The scanner could not serialize its state within the cleanup '
                                       'grace window, so no continuation cursor exists. Matches and '
                                       'coverage reflect the last progress event; consider narrowing '
                                       'the roots, refining the query, or running a fresh scan.')]}
                    result['cleanup'] = cleanup
                    return result
        await asyncio.shield(drain_task)
        if proc.returncode != 0 or result is None:
            raise ValueError('search worker failed: ' + stderr_tail.decode('utf-8', 'replace')[-2048:])
        result['cleanup'] = {'status': 'normal_completion', 'process_reaped': True,
                             'group_alive': False, 'errors': []}
        if result.get('stop_reason') == 'deadline':
            # The worker returned its cooperative deadline result intact.
            result['cleanup']['cooperative_deadline'] = True
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
                              protected_read_paths=(), operation='file_search',
                              retain_scope=None):
    from tools.builtins import BuiltinExecutionResult
    options = options or {}
    if operation == 'file_search' and options.get('enabled') is False:
        return failed('Scoped search is disabled', status='unavailable')
    try:
        continuations.configure(options)
    except ValueError as exc:
        return failed(str(exc))
    defaults = search_scope.get('preferred_roots') or [
        {'path': str(cwd.resolve()), 'provenance': 'execution-cwd'}]
    try:
        config = normalize_query(args, defaults=defaults, read_roots=read_roots, cwd=cwd,
                                 allow_wsl_windows_drive_paths=audit_context.get('allow_wsl_windows_drive_paths', False),
                                 operation=operation)
    except InvalidRegex as exc:
        return failed(str(exc), code='invalid_regex')
    except NotImplementedError as exc:
        return failed(str(exc), status='unavailable', code='regex_unavailable')
    except (ValueError, OSError) as exc:
        return failed(str(exc))
    state_caps = [continuations.policy[key] for key in ('max_state_bytes', 'max_bytes')
                  if continuations.policy[key]]
    config.update(authorized_roots=[str(root) for root in read_roots],
                  protected_read_paths=list(map(str, protected_read_paths)),
                  match_budget=8000,
                  state_capacity_bytes=min(state_caps) if state_caps else 0)
    scope_size = len(json.dumps({'query': config['query'], 'roots': config['selected_roots'],
        'filters': {key: config[key] for key in ('include', 'exclude')}}, ensure_ascii=False))
    compact_scope = operation == 'file_search' and scope_size > 4000
    config['match_budget'] = (8000 if compact_scope else
                              min(8000, MAX_ENVELOPE_CHARS - scope_size * 2 - 4000))
    if config['match_budget'] < 1024:
        return failed('Scope/filter metadata exceeds the output budget; narrow the roots or filters',
                      code='scope_output_budget')
    if args.get('_terms'):
        config['terms'] = args['_terms']
    # Bind query and scope to the Run; only process-local cursors also depend on Worker generation.
    binding = hashlib.sha256(json.dumps({
        'query': config, 'owner': search_scope.get('owner_id'), 'operation': operation,
        'agent': audit_context.get('agent_name'),
        'request': audit_context.get('request_id') or audit_context.get('task_id'),
        'scope_revision': search_scope.get('workzone_revision'),
        'generation': (None if isinstance(continuations, RunContinuations)
                       else audit_context.get('function_generation')),
    }, sort_keys=True).encode()).hexdigest()
    activity = CURRENT_ACTIVITY.get()
    operation_id = activity.operation_id if activity else secrets.token_hex(16)
    cursor = args.get('cursor')
    if cursor:
        try:
            if isinstance(continuations, RunContinuations):
                config['state'], operation_id = await asyncio.to_thread(
                    continuations.get, cursor, binding)
            else:
                config['state'], operation_id = continuations.get(cursor, binding)
            continuations.drop(cursor)
            if activity:
                activity.operation_id = operation_id
        except ValueError as exc:
            return failed(str(exc), code='cursor_stale', data={
                'stop_reason': 'cursor_stale', 'can_continue': False,
                'scope_exhausted': False, 'coverage_complete': False,
                'recovery': restart_guidance(),
            })
    if config.get('regex_content'):
        saved_state = config.get('state') or {}
        if 'regex_line_budget_effective' in saved_state:
            config['regex_line_budget_effective'] = saved_state['regex_line_budget_effective']
            config['regex_line_budget_source'] = saved_state['regex_line_budget_source']
        else:
            requested = config['regex_line_budget_requested']
            if requested is None:
                from tools.office.limits import _available_memory

                available = _available_memory()
                config['regex_line_budget_effective'] = max(1, available // 8) if available else 0
                config['regex_line_budget_source'] = 'available_memory' if available else 'memory_unavailable'
            else:
                config['regex_line_budget_effective'] = requested
                config['regex_line_budget_source'] = 'selected' if requested else 'unlimited'
    try:
        timeout, budget_policy = resolve_search_budget(
            args, options=options, operation=operation,
            recursive=bool(config.get('recursive')))
    except ValueError as exc:
        return failed(str(exc), code='invalid_timeout')
    if timeout is not None:
        config['deadline'] = time.time() + timeout
    grace = options.get('cleanup_grace_seconds', DEFAULT_CLEANUP_GRACE_SECONDS)
    try:
        grace = float(grace)
        if not 0 <= grace < float('inf'):
            grace = DEFAULT_CLEANUP_GRACE_SECONDS
    except (TypeError, ValueError):
        grace = DEFAULT_CLEANUP_GRACE_SECONDS
    config['cleanup_grace_seconds'] = grace
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
                  execution_budget={**budget_policy, 'effective_seconds': timeout,
                                    'cleanup_grace_seconds': config.get('cleanup_grace_seconds')},
                  effective_policy={key: config[key] for key in (
                      'profile', 'include', 'exclude', 'include_hidden', 'follow_links', 'cross_filesystems')})
    if result['stop_reason'] == 'deadline':
        result['warnings'].append(
            'Execution budget reached; coverage is incomplete. You may continue '
            'this scan with next_cursor, narrow the roots, refine the query, switch '
            'tools, or stop when you have enough evidence. A deadline is not a '
            'command to abandon the task.')
    result['effective_policy']['continuations'] = {
        **continuations.policy, 'retention': getattr(continuations, 'retention', 'registry_lifetime')}
    if config.get('regex_content'):
        result['effective_policy']['regex_engine'] = result.pop('regex_engine', None)
        result['coverage']['regex_matching_scope'] = 'single_lf_delimited_lines'
        result['effective_policy']['regex_max_line_bytes'] = config['regex_line_budget_effective']
        result['effective_policy']['regex_line_budget_source'] = config['regex_line_budget_source']
        if config['regex_line_budget_source'] == 'memory_unavailable':
            result['warnings'].append('Available memory is unknown; regex has no automatic line budget')
    result['coverage']['excluded_content'] = 'Containers/media/binary; ordinary UTF-8 text only' if config['mode'] == 'content' else 'No file bodies read'
    from tools.search_worker import PROJECT_EXCLUDED_DIRS, PROJECT_EXCLUDED_FILES, CONTAINER_SUFFIXES
    result['coverage']['default_directory_exclusions'] = (
        sorted(PROJECT_EXCLUDED_DIRS) if config['profile'] == 'project' else [])
    result['coverage']['default_file_exclusions'] = (
        sorted(PROJECT_EXCLUDED_FILES) if config['profile'] == 'project' else [])
    result['coverage']['excluded_content_suffixes'] = (
        sorted(CONTAINER_SUFFIXES) if config['mode'] == 'content' and operation != 'log_query' else [])
    if broad_scope_advisory([entry['path'] for entry in config['selected_roots']]):
        result['warnings'].append('Broad selected roots may take longer; scope is advisory, not denied')
    if compact_scope:
        root_ids = {entry['path']: f'root-{index}' for index, entry in
                    enumerate(config['selected_roots'])}
        original_coverage_roots = result['coverage']['roots']
        metadata = {
            'version': 1,
            'query': config['query'],
            'selected_roots': [{**entry, 'root_id': root_ids[entry['path']]}
                               for entry in config['selected_roots']],
            'filters': {'include': config['include'], 'exclude': config['exclude']},
        }
        encoded = json.dumps(metadata, ensure_ascii=True, separators=(',', ':'))
        digest = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
        saved_reference = (config.get('state') or {}).get('scope_metadata_reference') or {}
        reference = (saved_reference.get('reference')
                     if saved_reference.get('sha256') == digest else None)
        if not reference:
            try:
                reference = (await asyncio.to_thread(retain_scope, encoded)
                             if retain_scope is not None else None)
            except Exception:
                reference = None
        if not reference:
            return failed('Large search scope needs Run-bound output retention to page metadata',
                          code='scope_output_budget')
        result['scope_reference'] = {
            'reference': reference, 'read_tool': 'tool_output_read',
            'sha256': digest,
            'format': 'json',
        }
        result['query'] = None
        result['selected_roots'] = [
            {'root_id': root_ids[entry['path']], 'provenance': entry.get('provenance'),
             **({'available': entry['available']} if 'available' in entry else {})}
            for entry in config['selected_roots']]
        result['coverage']['roots'] = [
            {'root_id': root_ids[entry['path']], 'exhausted': entry['exhausted'],
             'failed': entry['failed']} for entry in result['coverage']['roots']]
        for match in result['matches']:
            match['root'] = root_ids[match['root']]
        result['effective_policy']['include'] = None
        result['effective_policy']['exclude'] = None
        result['effective_policy']['filters_in_scope_reference'] = True
    status = 'success' if complete else 'partial'
    if result['stop_reason'] in {'cursor_stale', 'invalid_regex', 'regex_backend_error'} or all(
            root.get('failed') for root in result['coverage']['roots']) and result['coverage']['roots']:
        status = 'failed'
    error = None
    if status == 'failed':
        message = 'No complete valid search coverage; inspect coverage errors'
        if result['stop_reason'] == 'invalid_regex' and result['coverage']['errors']:
            message = result['coverage']['errors'][0]['code']
        error = {'code': result['stop_reason'], 'message': message, 'retryable': False}
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
        coverage_roots = (original_coverage_roots if compact_scope
                          else result['coverage']['roots'])
        if compact_scope:
            paths = {root_id: path for path, root_id in root_ids.items()}
            for match in removed:
                match['root'] = paths[match['root']]
        state = state or {'roots': [{'path': root['path'], 'done': True, 'initialized': True,
                                     'failed': root['failed'], 'stack': [], 'pending_file': None}
                                    for root in coverage_roots],
                         'counters': dict(result['counters']), 'errors': result['coverage']['errors'],
                         'warnings': result['warnings'], 'turn': 0}
        pending = ([state.pop('pending_match')] if 'pending_match' in state else [])
        state['pending_matches'] = removed + pending + state.get('pending_matches', [])
        result['counters']['matches'] -= len(removed)
        state['counters']['matches'] = result['counters']['matches']
        result.update(coverage_complete=False, scope_exhausted=False, stop_reason='output_budget')
        complete, status = False, 'partial'
    if state and compact_scope:
        state['scope_metadata_reference'] = result['scope_reference']
    if state and isinstance(continuations, RunContinuations):
        next_cursor = await asyncio.to_thread(continuations.put, binding, state, operation_id)
    else:
        next_cursor = continuations.put(binding, state, operation_id) if state else None
    result.update(can_continue=next_cursor is not None, next_cursor=next_cursor)
    if state and next_cursor is None:
        result['warnings'].append('Continuation storage unavailable or configured capacity reached; no automatic restart')
    if (result['stop_reason'] == 'cursor_stale' or
            status == 'partial' and not next_cursor and not result['scope_exhausted']):
        result['recovery'] = restart_guidance()
    output = outcome(status, result, error=error)
    return BuiltinExecutionResult(output, {'search_outcome': status,
                                          'foreground_cleanup': result['cleanup'],
                                          'stop_reason': result['stop_reason'],
                                          'coverage_complete': complete,
                                          'selected_roots': result['selected_roots'],
                                          'coverage': result['coverage']})
# HASHI-Fingerprint: hfp_e14485052c6c4e49bbc76050da7e245f
