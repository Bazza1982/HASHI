from __future__ import annotations

import asyncio
import io
import json
import logging
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.bootstrap_logging import (
    AnimMute,
    ConsoleOutputFilter,
    receive_worker_log,
)
from orchestrator.function_worker_features import (
    WORKER_LOG_RELAY_FEATURE,
    worker_log_relay_enabled,
)
from orchestrator.function_worker_protocol import (
    FUNCTION_WORKER_PROTOCOL_VERSION,
    FunctionWorkerProtocolError,
    JsonConnectionPeer,
)

ROOT = Path(__file__).resolve().parents[1]


def _run_worker_logging_probe(tmp_path, bootstrap):
    script = '''
import asyncio, json, logging, sys, warnings
from pathlib import Path
from orchestrator import function_worker_host as module
home=Path(sys.argv[1])
bootstrap=json.loads(sys.argv[2])
credential='123456789:abcdefghijklmnopqrstuvwxyz_ABCD'
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
        logging.getLogger('PostTurnRegistry').warning(
            f'observer registration diagnostic https://api.telegram.org/bot{credential}/getMe',
            extra={'terminal_detail': f'token={credential}'},
        )
        warnings.warn('library warning diagnostic')
        self.stop_event.set()
    async def shutdown(self): self.phase='STOPPED'
module.FunctionWorkerHost=Host
asyncio.run(module.run_function_worker(None, bootstrap))
'''
    return subprocess.run(
        [sys.executable, "-B", "-c", script, str(tmp_path), json.dumps(bootstrap)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _run_worker_logging_ipc_process(connection, bridge_home, bootstrap):
    """Run the new Worker logging path over a real child-process pipe."""
    from orchestrator import function_worker_host as module

    home = Path(bridge_home)

    class Host:
        def __init__(self, worker_connection, _bootstrap):
            self.bridge_home = home
            self.peer = JsonConnectionPeer(
                worker_connection,
                label=f"new-worker:{os.getpid()}",
            )
            self.stop_event = asyncio.Event()
            self.phase = "BOOTING"

        async def prepare(self):
            logging.getLogger("PostTurnRegistry").warning(
                "observer registration diagnostic"
            )
            self.phase = "READY"
            await self.peer.emit(
                "worker.ready",
                {
                    "worker_protocol": FUNCTION_WORKER_PROTOCOL_VERSION,
                    "worker_pid": os.getpid(),
                },
            )
            self.stop_event.set()

        async def shutdown(self):
            self.phase = "STOPPED"

    module.FunctionWorkerHost = Host
    asyncio.run(module.run_function_worker(connection, bootstrap))


def test_worker_warnings_use_shared_animation_filter_and_keep_file_diagnostics(
    tmp_path, monkeypatch
):
    result = _run_worker_logging_probe(
        tmp_path,
        {"protocol_features": [WORKER_LOG_RELAY_FEATURE]},
    )
    assert result.returncode == 0, result.stderr
    assert not result.stdout and not result.stderr
    files = list((tmp_path / "logs" / "function-workers").glob("*.log"))
    assert len(files) == 1
    diagnostic = files[0].read_text()
    assert "observer registration diagnostic" in diagnostic
    assert "library warning diagnostic" in diagnostic
    assert "123456789:abcdefghijklmnopqrstuvwxyz_ABCD" not in diagnostic
    assert "[REDACTED_BOT_TOKEN]" in diagnostic
    if os.name != "nt":
        assert files[0].stat().st_mode & 0o077 == 0
    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    assert len(events) == 2 and all(event["event"] == "worker.log" for event in events)
    assert "123456789:abcdefghijklmnopqrstuvwxyz_ABCD" not in json.dumps(events)
    assert events[0]["payload"]["terminal_detail"] == "token=[REDACTED]"

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


@pytest.mark.parametrize(
    ("bootstrap", "expected_log_events"),
    [
        pytest.param({}, 0, id="legacy-supervisor-default"),
        pytest.param(
            {"protocol_features": [WORKER_LOG_RELAY_FEATURE]},
            1,
            id="capable-supervisor-opt-in",
        ),
    ],
)
@pytest.mark.asyncio
async def test_worker_log_relay_negotiates_over_real_process_ipc(
    tmp_path,
    bootstrap,
    expected_log_events,
):
    context = multiprocessing.get_context("spawn")
    supervisor_connection, worker_connection = context.Pipe(duplex=True)
    ready = asyncio.Event()
    log_received = asyncio.Event()
    log_events = []
    rejected_legacy_events = []

    async def handle_event(event, payload):
        if event == "worker.ready":
            ready.set()
            return
        if event == "worker.log" and expected_log_events:
            log_events.append(payload)
            log_received.set()
            return
        rejected_legacy_events.append(event)
        raise FunctionWorkerProtocolError(f"legacy Supervisor rejected {event!r}")

    peer = JsonConnectionPeer(
        supervisor_connection,
        label="legacy-or-capable-supervisor",
        event_handler=handle_event,
    )
    process = context.Process(
        target=_run_worker_logging_ipc_process,
        args=(worker_connection, str(tmp_path), bootstrap),
        name="new-worker-log-ipc-probe",
        daemon=False,
    )
    process.start()
    worker_connection.close()
    peer.start()
    try:
        await asyncio.wait_for(ready.wait(), timeout=10)
        if expected_log_events:
            await asyncio.wait_for(log_received.wait(), timeout=10)
        await asyncio.to_thread(process.join, 10)
        assert not process.is_alive()
        assert process.exitcode == 0
        await asyncio.wait_for(peer.wait_closed(), timeout=10)
        await asyncio.sleep(0)
    finally:
        await peer.close()
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 5)

    files = list((tmp_path / "logs" / "function-workers").glob("*.log"))
    assert len(files) == 1
    assert "observer registration diagnostic" in files[0].read_text()
    assert rejected_legacy_events == []
    assert len(log_events) == expected_log_events


@pytest.mark.parametrize(
    ("bootstrap", "enabled"),
    [
        ({}, False),
        ({"protocol_features": None}, False),
        ({"protocol_features": WORKER_LOG_RELAY_FEATURE}, False),
        ({"protocol_features": ["unknown-v1"]}, False),
        ({"protocol_features": [WORKER_LOG_RELAY_FEATURE, 1]}, False),
        ({"protocol_features": (WORKER_LOG_RELAY_FEATURE,)}, True),
    ],
)
def test_worker_log_relay_capability_is_explicit_and_fail_closed(
    bootstrap,
    enabled,
):
    assert worker_log_relay_enabled(bootstrap) is enabled
