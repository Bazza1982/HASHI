"""Delayed reboot: queue until the instance is quiet, then hand off.

The wait must never fence a route, close admission or touch a Worker, so every
assertion here is about what has *not* happened yet.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator.reboot_manager import RebootManager
from orchestrator.reboot_ui import render_notice

from tests.test_reboot_manager import _Kernel


def _setup(tmp_path, monkeypatch=None, *, poll=0.01, idle_window=0.05):
    if monkeypatch is not None:
        monkeypatch.setattr(
            "orchestrator.reboot_manager.REBOOT_DELAY_POLL_SECONDS", poll
        )
        monkeypatch.setattr(
            "orchestrator.reboot_manager.REBOOT_DELAY_IDLE_WINDOW_SECONDS", idle_window
        )
    kernel = _Kernel(names=("zelda",))
    kernel.paths = SimpleNamespace(bridge_home=tmp_path)
    kernel.shutdown_event = asyncio.Event()
    kernel._restart_request = None
    manager = RebootManager(kernel, None)
    request = {
        "mode": "min",
        "agent_name": "zelda",
        "request_key": "delay-1",
        "origin": {"chat_id": 42, "actor_id": "42", "thread_id": 7},
        "locale": "en",
    }
    return kernel, manager, request


def parse_request(value):
    from orchestrator.runtime_reboot import parse_request as parse

    return parse(value)


@pytest.mark.asyncio
async def test_delayed_reboot_waits_for_a_quiet_window_before_cutover(
    tmp_path, monkeypatch
):
    kernel, manager, request = _setup(tmp_path, monkeypatch)
    client = kernel.runtimes[0].client
    client.metadata["is_generating"] = True

    accepted = manager.submit({**request, "delay_seconds": 1800})
    assert accepted["accepted"] and accepted["waiting"]
    record = accepted["record"]
    assert record["phase"] == "waiting_idle"
    assert record["status"] == "accepted"
    assert "Nothing has been restarted yet" in render_notice(record, locale="en")
    assert "/reboot cancel" in render_notice(record, locale="en")
    waiting_zh = render_notice(record, locale="zh-CN")
    assert "重启已排队" in waiting_zh and "30 分钟" in waiting_zh

    await asyncio.sleep(0.12)
    assert not kernel.shutdown_event.is_set()
    assert kernel._restart_request is None
    assert manager.active_operation == record["id"]

    client.metadata["is_generating"] = False
    await asyncio.sleep(0.02)
    assert not kernel.shutdown_event.is_set()

    await asyncio.sleep(0.2)
    assert kernel.shutdown_event.is_set()
    assert kernel._restart_request["operation_id"] == record["id"]
    assert kernel._restart_request["targets"] == ["zelda"]


@pytest.mark.asyncio
async def test_delayed_reboot_expires_without_fencing_anything(tmp_path, monkeypatch):
    kernel, manager, request = _setup(tmp_path, monkeypatch)
    client = kernel.runtimes[0].client
    client.metadata["queue_depth"] = 2

    accepted = manager.submit({**request, "delay_seconds": 0.2})
    assert accepted["waiting"]

    await asyncio.sleep(0.45)
    record = manager.receipts.get(accepted["record"]["id"])
    assert record["status"] == "rejected"
    assert record["reason"] == "idle_timeout"
    assert record["busy_targets"] == ["zelda"]
    assert not kernel.shutdown_event.is_set()
    assert kernel._restart_request is None
    assert manager.active_operation is None
    assert manager._operation_projection(record)["blocking_targets"] == [
        {"agent_id": "zelda", "display_name": "zelda"}
    ]
    rendered = render_notice(record, locale="en")
    assert "The wait limit expired" in rendered
    assert "zelda" in rendered


@pytest.mark.asyncio
async def test_delayed_reboot_can_be_cancelled_before_cutover(tmp_path, monkeypatch):
    kernel, manager, request = _setup(tmp_path, monkeypatch)
    client = kernel.runtimes[0].client
    client.metadata["is_generating"] = True

    accepted = manager.submit({**request, "delay_seconds": 120})
    assert accepted["waiting"]

    cancelled = manager.submit({"cancel": True})
    assert cancelled["accepted"]
    record = manager.receipts.get(accepted["record"]["id"])
    assert record["status"] == "rejected"
    assert record["reason"] == "delay_cancelled"
    assert manager.active_operation is None

    await asyncio.sleep(0.12)
    assert not kernel.shutdown_event.is_set()
    assert kernel._restart_request is None
    assert manager.submit({"cancel": True}) == {
        "accepted": False,
        "reason": "not_waiting",
    }


@pytest.mark.parametrize("value", [0, -5, True, "15", 10**9])
@pytest.mark.asyncio
async def test_delayed_reboot_rejects_an_unusable_wait_limit(tmp_path, value):
    kernel, manager, request = _setup(tmp_path, monkeypatch=None)

    assert manager.submit({**request, "delay_seconds": value}) == {
        "accepted": False,
        "reason": "invalid_scope",
    }
    assert not kernel.shutdown_event.is_set()
    assert manager.active_operation is None


def test_reboot_request_parser_accepts_delay_only_as_a_known_modifier():
    assert parse_request("max delay") == ("max", {"delay_seconds": 1800.0})
    assert parse_request("max delay=15") == ("max", {"delay_seconds": 900.0})
    assert parse_request("min") == ("min", {})
    assert parse_request("3 delay") == ("3", {"delay_seconds": 1800.0})
    assert parse_request("max delay=0") == (None, {})
    assert parse_request("max delay=abc") == (None, {})
    assert parse_request("max force") == (None, {})
    assert parse_request("cancel") == ("cancel", {})
# HASHI-Fingerprint: hfp_2e68cec687f24f76b6f7e207aea77a72
