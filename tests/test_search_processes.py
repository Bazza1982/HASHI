"""Small real foreground process probes; no production runtime adoption."""
import asyncio
import json
import os
import shlex
import shutil
import sys
import tracemalloc
from pathlib import Path

import pytest

from tests.test_scoped_search import registry_for
from tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_real_mcp_progress_precedes_result_and_cancel_remains_responsive(tmp_path):
    from tools.gateway.context import write_gateway_context
    from tools.registry import ToolRegistry
    context = tmp_path / 'gateway.json'
    registry = ToolRegistry(['shell', 'file_search'], tmp_path, tmp_path, {},
        tool_options={'tool_activity': {'long_threshold_seconds': .02,
                                      'snapshot_interval_seconds': .01}},
        audit_context={'agent_name': 'fixture', 'request_id': 'fixture-run'})
    write_gateway_context(registry, context, backend='codex-cli')
    started_marker = tmp_path / 'child-started.txt'
    script = tmp_path / 'wait-for-cancel.py'
    script.write_text('from pathlib import Path\nimport time\n'
                      f'Path({str(started_marker)!r}).write_text("started")\n'
                      'time.sleep(30)\n', encoding='utf-8')
    proc = await asyncio.create_subprocess_exec(sys.executable, '-B', '-m',
        'tools.gateway.mcp_stdio', '--context', str(context),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    errors = asyncio.create_task(proc.stderr.read())
    async def send(value):
        proc.stdin.write(json.dumps(value).encode() + b'\n')
        await proc.stdin.drain()
    try:
        await send({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'shell', 'arguments': {'command': script_command(script)},
            '_meta': {'progressToken': 'progress'}}})
        first = json.loads(await asyncio.wait_for(proc.stdout.readline(), 5))
        assert first['method'] == 'notifications/progress' and 'id' not in first
        assert first['params']['_meta']['tool_activity']['state'] in {'started', 'running'}
        # Progress state can describe admission. A real child-created marker
        # proves cancellation exercises an in-flight process, not queued work.
        async with asyncio.timeout(5):
            while not started_marker.exists():
                await asyncio.sleep(.01)
        await send({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                    'params': {'requestId': 1}})
        received = []
        while True:
            event = json.loads(await asyncio.wait_for(proc.stdout.readline(), 5))
            received.append(event)
            if event.get('id') == 1:
                assert event['error']['code'] == -32800
                break
        final = next(event['params']['_meta']['tool_activity'] for event in received
                     if event.get('method') == 'notifications/progress' and
                     event['params']['_meta']['tool_activity']['state'] == 'interrupted')
        assert final['cleanup']['process_reaped']
        await send({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        listed = json.loads(await asyncio.wait_for(proc.stdout.readline(), 5))
        assert listed['id'] == 2 and any(t['name'] == 'file_search' for t in listed['result']['tools'])
        proc.stdin.close()
        await asyncio.wait_for(proc.wait(), 5)
        assert proc.returncode == 0
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        await errors



def script_command(path):
    if os.name == 'nt':
        return "& '" + sys.executable.replace("'", "''") + "' '" + str(path).replace("'", "''") + "'"
    return shlex.quote(sys.executable) + ' ' + shlex.quote(str(path))


@pytest.mark.asyncio
async def test_stdout_and_stderr_are_drained_with_bounded_parent_memory(tmp_path):
    script = tmp_path / 'output.py'
    script.write_text("import sys\nsys.stdout.buffer.write(b'a' * (8 * 1024 * 1024))\n"
                      "sys.stderr.buffer.write(b'b' * (8 * 1024 * 1024))\n", encoding='utf-8')
    registry = ToolRegistry(['shell'], tmp_path, tmp_path, {})
    await registry.execute('shell', {'command': 'exit 0'})
    tracemalloc.start()
    try:
        result = await registry.execute('shell', {'command': script_command(script)})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert not result.is_error, result.output[:300]
    assert len(result.output) < 21000
    assert peak < 5 * 1024 * 1024
    assert result.details['output_counters']['stdout_bytes'] == 8 * 1024 * 1024
    assert result.details['output_counters']['stderr_bytes'] == 8 * 1024 * 1024
    assert result.details['foreground_cleanup']['process_reaped']


@pytest.mark.asyncio
async def test_cancel_stops_the_actual_search_worker_and_ends_observer(tmp_path, monkeypatch):
    registry, repo, _ = registry_for(tmp_path, options={
        'tool_activity': {'snapshot_interval_seconds': .01, 'long_threshold_seconds': .01}})
    source = repo / 'long.txt'
    with source.open('wb') as stream:
        stream.write(b'x' * 8192)
        stream.truncate(64 * 1024 * 1024)
    started = asyncio.Event()
    processes = []
    real_start = asyncio.create_subprocess_exec
    async def capture(*args, **kwargs):
        process = await real_start(*args, **kwargs)
        if any(str(arg).endswith('search_worker.py') for arg in args):
            processes.append(process)
            started.set()
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', capture)
    events = []
    task = asyncio.create_task(registry.execute_with_audit_context(
        'file_search', {'query': 'not-present', 'mode': 'content', 'roots': [str(source)]},
        'cancel', audit_context={'request_id': 'run', 'tool_activity_observer': events.append}))
    await asyncio.wait_for(started.wait(), 3)
    assert processes[0].returncode is None
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as error:
        await asyncio.wait_for(task, 3)
    cleanup = error.value.hashi_tool_details['foreground_cleanup']
    assert cleanup['process_reaped'] and cleanup['status'] != 'cleanup_failed'
    assert processes[0].returncode is not None
    audit_path = Path(registry.search_scope['agent_home']) / 'tool_action_audit.jsonl'
    audit = [json.loads(line) for line in audit_path.read_text().splitlines()]
    assert audit[0]['event_type'] == 'tool_execution_started'
    assert audit[0]['status'] == 'started'
    assert audit[0]['details']['selected_roots'] == [str(source)]
    assert audit[-1]['details']['coverage_complete'] is False
    assert audit[-1]['details']['foreground_cleanup']['process_reaped']
    assert [event.metadata['state'] for event in events][-2:] == ['cancelling', 'interrupted']
    before = len(events)
    await asyncio.sleep(.04)
    assert len(events) == before


@pytest.mark.asyncio
async def test_deadline_zero_matches_are_not_complete_zero_matches(tmp_path):
    registry, repo, _ = registry_for(tmp_path, options={'file_search': {'timeout_seconds': .01}})
    with (repo / 'long.txt').open('wb') as stream:
        stream.write(b'x' * 8192)
        stream.truncate(64 * 1024 * 1024)
    result = await registry.execute('file_search', {'query': 'absent', 'mode': 'content'})
    envelope = json.loads(result.output)
    assert envelope['status'] == 'partial'
    assert not envelope['data']['coverage_complete']
    assert envelope['data']['stop_reason'] == 'deadline'
    assert result.details['foreground_cleanup']['process_reaped']


@pytest.mark.asyncio
async def test_wide_scope_is_advisory_and_shell_needs_no_prior_search(tmp_path):
    registry = ToolRegistry(['shell'], tmp_path, tmp_path, {})
    result = await registry.execute('shell', {'command': 'exit 0', 'purpose': 'search',
                                            'search_roots': ['D:\\']})
    assert not result.is_error
    assert result.details['scope_advisory']
    assert 'scope advisory' in result.output
    from tools.tool_activity import command_search_roots
    assert command_search_roots('find / -name target') == ['/']
    assert not command_search_roots("rg '/' repo")
    assert not command_search_roots('find $ROOT -name target | head')


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
@pytest.mark.asyncio
async def test_cancel_regex_reaps_foreground_worker_and_ripgrep(tmp_path, monkeypatch):
    import psutil

    registry, repo, _ = registry_for(tmp_path)
    source = repo / "long.txt"
    with source.open("wb") as stream:
        chunk = b"a" * (4 * 1024 * 1024)
        for _ in range(24):
            stream.write(chunk)
    started = asyncio.Event()
    processes = []
    real_start = asyncio.create_subprocess_exec
    async def capture(*args, **kwargs):
        process = await real_start(*args, **kwargs)
        if any(str(arg).endswith("search_worker.py") for arg in args):
            processes.append(process)
            started.set()
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    task = asyncio.create_task(registry.execute_with_audit_context(
        "file_search", {"query": "(a+)+b", "match": "regex", "mode": "content",
                        "roots": [str(source)]},
        "cancel-regex", audit_context={"request_id": "regex-cancel-run"},
    ))
    await asyncio.wait_for(started.wait(), 5)
    parent = psutil.Process(processes[0].pid)
    child = None
    for _ in range(1000):
        if task.done():
            break
        try:
            children = parent.children(recursive=True)
        except psutil.NoSuchProcess:
            break
        for candidate in children:
            try:
                if candidate.name().lower() in {"rg", "rg.exe"} and candidate.is_running():
                    opened = candidate.open_files()
                    if any(Path(row.path).resolve() == source.resolve() for row in opened):
                        child = (candidate.pid, candidate.create_time())
                        break
            except psutil.Error:
                continue
        if child:
            break
        await asyncio.sleep(.005)
    assert child is not None, "actual regex subprocess was not observed"
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as raised:
        await asyncio.wait_for(task, 10)
    assert raised.value.hashi_tool_details["foreground_cleanup"]["process_reaped"]
    assert processes[0].returncode is not None
    try:
        still_running = psutil.Process(child[0])
        assert still_running.create_time() != child[1] or not still_running.is_running()
    except psutil.NoSuchProcess:
        pass


@pytest.mark.asyncio
async def test_cancel_path_regex_with_catastrophic_backtracking_reaps_worker(tmp_path, monkeypatch):
    registry, repo, _ = registry_for(tmp_path)
    (repo / (("a" * 60) + "x.txt")).touch()
    started = asyncio.Event()
    processes = []
    real_start = asyncio.create_subprocess_exec
    async def capture(*args, **kwargs):
        process = await real_start(*args, **kwargs)
        if any(str(arg).endswith("search_worker.py") for arg in args):
            processes.append(process)
            started.set()
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    task = asyncio.create_task(registry.execute_with_audit_context(
        "file_search", {"query": "(a+)+$", "match": "regex", "mode": "path", "roots": [str(repo)]},
        "cancel-path-regex", audit_context={"request_id": "path-regex-cancel"},
    ))
    await asyncio.wait_for(started.wait(), 5)
    await asyncio.sleep(.2)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as raised:
        await asyncio.wait_for(task, 10)
    assert raised.value.hashi_tool_details["foreground_cleanup"]["process_reaped"]
    assert processes[0].returncode is not None


@pytest.mark.asyncio
async def test_cancel_python_content_regex_with_catastrophic_backtracking_reaps_worker(tmp_path, monkeypatch):
    registry, repo, _ = registry_for(tmp_path)
    source = repo / "pathological.txt"
    source.write_bytes((b"a" * 60) + b"!\n")
    started = asyncio.Event()
    processes = []
    real_start = asyncio.create_subprocess_exec
    async def capture(*args, **kwargs):
        process = await real_start(*args, **kwargs)
        if any(str(arg).endswith("search_worker.py") for arg in args):
            processes.append(process)
            started.set()
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    task = asyncio.create_task(registry.execute_with_audit_context(
        "file_search", {"query": "(a+)+$", "match": "regex", "mode": "content",
                        "roots": [str(source)], "options": {"regex_engine": "python"}},
        "cancel-python-regex", audit_context={"request_id": "python-regex-cancel"},
    ))
    await asyncio.wait_for(started.wait(), 5)
    await asyncio.sleep(.2)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as raised:
        await asyncio.wait_for(task, 10)
    assert raised.value.hashi_tool_details["foreground_cleanup"]["process_reaped"]
    assert processes[0].returncode is not None
