from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
from pathlib import Path

from orchestrator.bootstrap_logging import AnimMute, ConsoleOutputFilter, receive_worker_log

ROOT = Path(__file__).resolve().parents[1]


def test_worker_warnings_use_shared_animation_filter_and_keep_file_diagnostics(tmp_path, monkeypatch):
    script = '''
import asyncio, json, logging, sys, warnings
from pathlib import Path
from orchestrator import function_worker_host as module
home=Path(sys.argv[1])
class Peer:
    def start(self): pass
    async def emit(self, event, payload):
        with (home/'events.jsonl').open('a') as out:
            out.write(json.dumps({'event':event,'payload':payload})+'\\n')
    async def close(self): pass
    async def wait_closed(self): await asyncio.Event().wait()
class Host:
    def __init__(self, connection, bootstrap):
        self.bridge_home=home
        self.peer=Peer()
        self.stop_event=asyncio.Event()
        self.phase='READY'
    async def prepare(self):
        logging.getLogger('PostTurnRegistry').warning('observer registration diagnostic')
        warnings.warn('library warning diagnostic')
        self.stop_event.set()
    async def shutdown(self): self.phase='STOPPED'
module.FunctionWorkerHost=Host
asyncio.run(module.run_function_worker(None, {}))
'''
    result = subprocess.run([sys.executable, "-B", "-c", script, str(tmp_path)],
                            cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert not result.stdout and not result.stderr
    files = list((tmp_path / "logs" / "function-workers").glob("*.log"))
    assert len(files) == 1
    diagnostic = files[0].read_text()
    assert "observer registration diagnostic" in diagnostic
    assert "library warning diagnostic" in diagnostic
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert len(events) == 2 and all(event["event"] == "worker.log" for event in events)

    output = io.StringIO()
    console = logging.StreamHandler(output)
    console.addFilter(ConsoleOutputFilter())
    mute = AnimMute()
    console.addFilter(mute)
    monkeypatch.setattr(logging.getLogger(), "handlers", [console])
    for event in events:
        receive_worker_log(event["payload"])
    assert output.getvalue() == ""
    console.removeFilter(mute)
    receive_worker_log(events[0]["payload"])
    assert "observer registration diagnostic" in output.getvalue()
