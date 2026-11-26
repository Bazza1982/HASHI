from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import hashlib

import pytest

from orchestrator.api_gateway import _gateway_body_evidence
from orchestrator.runtime_command_binding import get_flexible_bot_commands
from remote.restart_receipts import project_restart
from tools import desktop


@pytest.mark.parametrize('wol', [False, True])
def test_real_command_menu_fits_telegram_limit(monkeypatch, wol):
    monkeypatch.setattr('orchestrator.runtime_command_binding.private_wol_available', lambda *_: wol)
    runtime = SimpleNamespace(global_config=SimpleNamespace(project_root=Path.cwd(), instance_id='HASHI4'))
    commands = get_flexible_bot_commands(runtime)
    names = [command.command for command in commands]
    assert len(names) <= 100
    assert len(set(names)) == len(names)
    assert {'language', 'restart', 'help'} <= set(names)
    assert ('wol' in names) == wol


def test_gateway_caps_large_body_and_preserves_full_digest():
    body = b'x' * 20000
    evidence = _gateway_body_evidence(body)
    assert len(evidence['body']) == 16384
    assert evidence['body_bytes'] == len(body)
    assert evidence['body_truncated'] is True
    assert evidence['body_sha256'] == hashlib.sha256(body).hexdigest()


def test_old_restart_projects_a_terminal_unconfirmed_result():
    record = {
        'restart_id': 'rst_20261115T135747Z_93c38ad2a401',
        'requester_agent': 'arale', 'target_instance': 'HASHI4',
        'controlled_instance': 'HASHI4',
        'notify_origin': {'owner_id': 'owner', 'agent_id': 'arale', 'instance_id': 'HASHI4'},
        'state': 'verifying', 'phase': 'terminal_verification',
        'updated_at': (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
        'lifecycle_timing': {'stages': [{}, {}, {}, {}]},
    }
    projected = project_restart(record, instance_id='HASHI4', owner_id='owner', agent_id='arale')
    assert projected['terminal'] is True
    assert projected['status'] == 'failed'
    assert projected['reason'] == 'restart_unconfirmed'
    assert projected['latest_sequence'] == 6
    assert record['state'] == 'verifying'


@pytest.mark.asyncio
async def test_windows_without_native_binary_reports_unavailable(monkeypatch):
    monkeypatch.setattr(desktop.os, 'name', 'nt')
    monkeypatch.delenv('HASHI_USECOMPUTER_BIN', raising=False)
    monkeypatch.setattr(desktop.shutil, 'which', lambda _: None)
    desktop._find_usecomputer.cache_clear()
    try:
        result = await desktop.execute_desktop_screenshot({})
        assert 'unavailable on Windows' in result
    finally:
        desktop._find_usecomputer.cache_clear()
