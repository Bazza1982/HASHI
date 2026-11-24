"""Unit tests for HAL dispatch adapters (instance identity + command results).

Focused on H2 (never drop the @instance suffix to hit a same-named agent on a
different instance) and H3 (None / unsupported / ok:false / exception are never
reported as success; accepted != completed).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from hal.dispatch import GatewayDispatchAdapter, resolve_agent_target, split_agent_spec


def test_split_agent_spec_parses_instance():
    assert split_agent_spec("@lily@hashi4") == ("lily", "hashi4")
    assert split_agent_spec("@lily") == ("lily", None)
    assert split_agent_spec("lily") == ("lily", None)
    assert split_agent_spec(None) == (None, None)


def test_resolve_agent_target_never_drops_instance_suffix():
    assert resolve_agent_target("@lily@hashi4", "hashi4") == ("lily", None)
    name, error = resolve_agent_target("@lily@hashi4", "hashi3")
    assert name is None
    assert "not this instance" in error
    assert resolve_agent_target("@lily", "hashi4") == ("lily", None)
    assert resolve_agent_target(None, "hashi4") == (None, "empty agent target")


class _FakeServer:
    def __init__(self):
        self._map = {"lily": SimpleNamespace(name="lily")}

    def _runtime_map(self):
        return self._map


def _run(coro):
    return asyncio.run(coro)


def _patch_command_exec(monkeypatch, result):
    async def fake_execute(runtime, text, **kwargs):
        return result

    import orchestrator.admin_local_testing as admin

    monkeypatch.setattr(admin, "try_execute_slash_command_text", fake_execute)


def test_command_ok_false_is_not_accepted(monkeypatch):
    _patch_command_exec(monkeypatch, {"ok": False, "error": "command disabled"})
    adapter = GatewayDispatchAdapter(_FakeServer())
    result = _run(adapter.dispatch_command(command_text="/reboot max", trigger_agent="lily"))
    assert result.accepted is False
    assert "command rejected" in result.detail


def test_command_none_is_not_supported(monkeypatch):
    _patch_command_exec(monkeypatch, None)
    adapter = GatewayDispatchAdapter(_FakeServer())
    result = _run(adapter.dispatch_command(command_text="/unknown", trigger_agent=None))
    assert result.accepted is False
    assert "not supported" in result.detail


def test_command_ok_true_marks_accepted_not_completed(monkeypatch):
    _patch_command_exec(monkeypatch, {"ok": True, "result": "scheduled"})
    adapter = GatewayDispatchAdapter(_FakeServer())
    result = _run(adapter.dispatch_command(command_text="/reboot max", trigger_agent="lily"))
    assert result.accepted is True
    assert "accepted != completed" in result.detail
# HASHI-Fingerprint: hfp_41e29b3efc0945b29b6e0130f9ce12f3
