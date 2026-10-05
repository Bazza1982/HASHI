"""Request-local foreground observations, independent of model commentary."""
from __future__ import annotations

import asyncio
import inspect
import logging
import math
import os
import re
import shlex
import time
import uuid
from pathlib import Path
from contextvars import ContextVar
from typing import Any

from adapters.stream_events import StreamEvent, DELIVERY_TECHNICAL
from orchestrator.ui_language import tr
from orchestrator.path_presentation import display_user_path

CURRENT_ACTIVITY: ContextVar['ToolActivity | None'] = ContextVar('tool_activity', default=None)
TERMINAL_STATES = frozenset({'completed', 'failed', 'interrupted', 'cleanup_pending'})
FUNCTION_GENERATION = Path(os.environ['HASHI_FUNCTION_GENERATION_ROOT']).name if os.environ.get('HASHI_FUNCTION_GENERATION_ROOT') else uuid.uuid4().hex


def broad_scope_advisory(roots: list[str]) -> bool:
    """Conservative annotation only. No parsing of arbitrary Shell programs."""
    return any(value.strip() == '/' or re.fullmatch(r'[A-Za-z]:[\\/]?', value.strip())
               for value in roots)


def command_search_roots(command: str) -> list[str]:
    """Recognize a few literal native forms, never infer arbitrary program scope."""
    if any(char in command for char in '|;&$`\n()'):
        return []
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        return []
    if not tokens:
        return []
    name = tokens[0].casefold()
    if name == 'find':
        candidates = tokens[1:2]
    elif name in {'get-childitem', 'gci'} and '-recurse' in [v.casefold() for v in tokens]:
        candidates = [tokens[i+1] for i, value in enumerate(tokens[:-1])
                      if value.casefold() in {'-path', '-literalpath'}]
    elif name in {'rg', 'grep', 'ripgrep'}:
        # Only the simple query + literal paths form; option values are unknown.
        if any(v.startswith('-') and v not in {'-r', '-R', '-n', '-l', '--files', '--hidden'} for v in tokens[1:]):
            return []
        values = [v for v in tokens[1:] if not v.startswith('-')]
        candidates = values if '--files' in tokens else values[1:]
    else:
        return []
    candidates = [value.strip('\"\'') for value in candidates]
    return [value for value in candidates if value == '/' or re.fullmatch(r'[A-Za-z]:[\\/]?', value)]


def _seconds(options: dict, key: str, default: float) -> float:
    try:
        value = float(options.get(key, default))
        return value if math.isfinite(value) and value > 0 else default
    except (ValueError, TypeError):
        return default


class ToolActivity:
    def __init__(self, tool: str, call_id: str, context: dict, options: dict):
        self.tool = tool
        self.call_id = call_id or uuid.uuid4().hex
        self.context = context
        self.observer = context.get('tool_activity_observer')
        self.operation_id = uuid.uuid4().hex
        self.started = time.time()
        self.monotonic = time.monotonic()
        self.threshold = _seconds(options, 'long_threshold_seconds', 30)
        self.interval = _seconds(options, 'visible_progress_interval_seconds', 150)
        self.tick = _seconds(options, 'snapshot_interval_seconds', 5)
        self.sequence = 0
        self.last_visible = 0.0
        self.ever_visible = False
        self.snapshot: dict[str, Any] = {
            'operation_id': self.operation_id, 'tool_call_id': self.call_id, 'tool': tool,
            'operation_type': tool, 'state': 'started', 'started_at': self.started,
            'function_generation': str(context.get('function_generation') or FUNCTION_GENERATION),
            'request_id': str(context.get('request_id') or context.get('task_id') or ''),
            'agent_id': str(context.get('agent_name') or ''),
            'selected_roots': [], 'scope_provenance': 'unknown',
            'last_worker_response': None, 'last_output': None, 'last_work_progress': None,
            'counters': {}, 'liveness': 'unknown', 'progress': 'unknown',
        }
        self._task = None
        self._token = None
        self._process = None
        self.failures = 0

    async def __aenter__(self):
        self._token = CURRENT_ACTIVITY.set(self)
        if callable(self.observer):
            self._task = asyncio.create_task(self._ticker())
        return self

    async def __aexit__(self, kind, error, traceback):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if kind is not None and self.snapshot['state'] not in TERMINAL_STATES:
            details = getattr(error, 'hashi_tool_details', {}) or {}
            cleanup = details.get('foreground_cleanup') or {}
            state = ('interrupted' if cleanup.get('process_reaped') and
                     cleanup.get('status') != 'cleanup_failed' else 'cleanup_pending')
            await self.finish(state if kind is asyncio.CancelledError else 'failed',
                              stop_reason='interrupted' if kind is asyncio.CancelledError else 'io_error',
                              cleanup=cleanup)
        if self._token is not None:
            CURRENT_ACTIVITY.reset(self._token)
        self._process = None

    def watch_process(self, process):
        self._process = process
        self.update(liveness='alive' if process.returncode is None else 'exited')

    async def record_start(self):
        writer = self.context.get('_tool_audit_start')
        if callable(writer):
            facts = {key: self.snapshot[key] for key in (
                'operation_id', 'tool_call_id', 'tool', 'operation_type',
                'function_generation', 'request_id', 'agent_id', 'selected_roots',
                'scope_provenance', 'started_at')}
            await asyncio.to_thread(writer, facts)

    def update(self, *, counters=None, worker_response=False, output=False, **facts):
        now = time.time()
        if counters is not None:
            old = self.snapshot['counters']
            # Clock ticks and output bytes are not file scanning progress.
            work_keys = ('directories_enumerated', 'files_enumerated', 'characters_read', 'bytes_read')
            if any(isinstance(counters.get(k), (int, float)) and
                   counters[k] > (old.get(k) or 0) for k in work_keys):
                self.snapshot['last_work_progress'] = now
                self.snapshot['progress'] = 'observed'
            self.snapshot['counters'] = dict(counters)
        if worker_response:
            self.snapshot['last_worker_response'] = now
            self.snapshot['liveness'] = 'alive'
        if output:
            self.snapshot['last_output'] = now
        self.snapshot.update(facts)

    def summary(self):
        roots = self.snapshot.get('selected_roots') or []
        scope = ', '.join(display_user_path(root) for root in roots[:8]) or tr('tool.activity.unknown_scope')
        if len(roots) > 8:
            scope += ', …'
        elapsed = max(0, int(time.monotonic() - self.monotonic))
        counters = self.snapshot['counters']
        count = counters.get('files_enumerated')
        key = 'tool.activity.scanning' if count is not None else 'tool.activity.running'
        return tr(key, tool=self.tool, scope=scope, elapsed=elapsed,
                  files=count, matches=counters.get('matches', 0),
                  state=tr('tool.activity.state.' + self.snapshot['state']))

    async def emit(self, *, visible: bool):
        if not callable(self.observer):
            return
        self.ever_visible |= visible
        self.sequence += 1
        metadata = {**self.snapshot, 'operation_id': self.operation_id,
                    'update_sequence': self.sequence,
                    'elapsed_ms': int((time.monotonic() - self.monotonic) * 1000),
                    'visible_update': visible, 'display_summary': self.summary()}
        event = StreamEvent(kind='tool_activity', summary=metadata['display_summary'] if visible else '',
                            tool_name=self.tool, delivery_class=DELIVERY_TECHNICAL,
                            origin='pao.tools', event_id=f'{self.operation_id}:{self.call_id}:{self.sequence}',
                            metadata=metadata)
        try:
            value = self.observer(event)
            if inspect.isawaitable(value):
                await asyncio.wait_for(value, timeout=1)
        except Exception as exc:
            self.failures += 1
            logging.getLogger(__name__).warning('Tool activity observer unavailable (%s)', type(exc).__name__)

    async def _ticker(self):
        await self.emit(visible=False)
        while True:
            await asyncio.sleep(self.tick)
            if self._process is not None:
                self.update(liveness='alive' if self._process.returncode is None else 'exited')
            elapsed = time.monotonic() - self.monotonic
            visible = elapsed >= self.threshold and (
                self.last_visible == 0 or elapsed - self.last_visible >= self.interval)
            if visible:
                self.last_visible = elapsed
            await self.emit(visible=visible)

    async def finish(self, state: str, **facts):
        self.update(state=state, **facts)
        if state in TERMINAL_STATES and state != 'cleanup_pending':
            self.update(liveness='exited')
        await self.emit(visible=self.ever_visible)


async def bounded_communicate(proc, *, activity: ToolActivity | None = None,
                              limit: int = 65536) -> tuple[bytes, bytes]:
    """Drain both pipes concurrently; retain bounded head/tail without spooling."""
    async def drain(stream, key):
        head, tail = bytearray(), bytearray()
        total = 0
        while chunk := await stream.read(32768):
            total += len(chunk)
            need = max(0, limit // 2 - len(head))
            head.extend(chunk[:need])
            tail.extend(chunk[need:])
            if len(tail) > limit // 2:
                del tail[:-limit // 2]
            if activity:
                counters = {**activity.snapshot['counters'], key: total}
                activity.update(counters=counters, output=True, worker_response=True)
        if total > limit:
            return bytes(head) + b'\n...[output omitted; bounded head/tail]\n' + bytes(tail)
        return bytes(head) + bytes(tail)
    stdout, stderr = await asyncio.gather(drain(proc.stdout, 'stdout_bytes'),
                                        drain(proc.stderr, 'stderr_bytes'))
    await proc.wait()
    return stdout, stderr
