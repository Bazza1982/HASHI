"""Targeted regressions for reboot origin delivery and qualified-cache reuse.

These tests pin the two HASHI2-local fixes without exercising the live
Function Worker lifecycle:

* a Workbench-originated /reboot receipt is delivered through the shared
  primary session (not Telegram), reusing the existing delivery state machine
  and idempotency key;
* the reboot candidate path prefers the already-qualified generation cache and
  only falls back to the isolated probe when the cache entry point is absent.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import orchestrator.reboot_manager as reboot_manager_module
from orchestrator.reboot_manager import RebootManager
from orchestrator.reboot_receipts import RebootReceipts, origin_delivery_requested


# --- origin_delivery_requested / receipt admission -------------------------


def test_origin_delivery_requested_by_surface():
    assert origin_delivery_requested({"surface": "telegram", "chat_id": 123}) is True
    assert origin_delivery_requested({"surface": "telegram"}) is False
    assert origin_delivery_requested({"surface": "workbench"}) is True
    assert origin_delivery_requested({"surface": "whatsapp"}) is False
    assert origin_delivery_requested(None) is False


def test_create_marks_delivery_by_origin_surface(tmp_path):
    receipts = RebootReceipts(tmp_path)
    common = dict(source="zelda", targets=["zelda"], display_names={"zelda": "Zelda"}, mode="min")

    workbench = receipts.create(origin={"surface": "workbench"}, **common)
    assert workbench["delivery"]["status"] == "pending"

    telegram = receipts.create(origin={"surface": "telegram", "chat_id": 123}, **common)
    assert telegram["delivery"]["status"] == "pending"

    telegram_no_chat = receipts.create(origin={"surface": "telegram"}, **common)
    assert telegram_no_chat["delivery"]["status"] == "not_requested"

    unknown = receipts.create(origin={"surface": "whatsapp"}, **common)
    assert unknown["delivery"]["status"] == "not_requested"


# --- _deliver routing ------------------------------------------------------


def _manager():
    return RebootManager(SimpleNamespace(paths=None), console_handler=None)


@pytest.mark.asyncio
async def test_workbench_delivery_writes_presentation_notice_not_telegram(monkeypatch):
    manager = _manager()
    record = manager.receipts.create(
        source="zelda",
        targets=["zelda"],
        display_names={"zelda": "Zelda"},
        mode="min",
        origin={"surface": "workbench"},
    )

    notices: list[dict] = []
    monkeypatch.setattr(
        reboot_manager_module.runtime_session,
        "record_kernel_presentation_notice",
        lambda kernel, *, agent_id, text, idempotency_key: notices.append(
            {"kernel": kernel, "agent_id": agent_id, "text": text, "key": idempotency_key}
        )
        or {"message_id": "msg-1"},
    )

    sent_telegram: list = []

    async def fake_send_runtime_notice(*a, **k):
        sent_telegram.append(1)
        return {"sent": True}

    monkeypatch.setattr(reboot_manager_module, "send_runtime_notice", fake_send_runtime_notice)

    result = await manager._deliver(record, starting=False)

    assert result == {"sent": True, "sender": "workbench", "message_id": "msg-1"}
    assert sent_telegram == []
    assert len(notices) == 1
    assert notices[0]["agent_id"] == "zelda"
    assert notices[0]["key"] == f"reboot:{record['id']}:final"
    assert notices[0]["text"]


@pytest.mark.asyncio
async def test_workbench_delivery_failure_is_not_sent(monkeypatch):
    manager = _manager()
    record = manager.receipts.create(
        source="zelda",
        targets=["zelda"],
        display_names={"zelda": "Zelda"},
        mode="min",
        origin={"surface": "workbench"},
    )
    monkeypatch.setattr(
        reboot_manager_module.runtime_session,
        "record_kernel_presentation_notice",
        lambda *a, **k: None,
    )
    assert await manager._deliver(record, starting=False) == {"sent": False}


@pytest.mark.asyncio
async def test_telegram_delivery_still_uses_telegram_transport(monkeypatch):
    manager = _manager()
    record = manager.receipts.create(
        source="zelda",
        targets=["zelda"],
        display_names={"zelda": "Zelda"},
        mode="min",
        origin={"surface": "telegram", "chat_id": 123},
    )

    captured: dict = {}

    async def fake_send_runtime_notice(kernel, *, source_agent, chat_id, thread_id, render_text):
        captured.update(
            chat_id=chat_id,
            source_agent=source_agent,
            text=render_text("zelda", "Zelda"),
        )
        return {"sent": True, "sender": "zelda", "message_id": 99}

    monkeypatch.setattr(reboot_manager_module, "send_runtime_notice", fake_send_runtime_notice)
    monkeypatch.setattr(
        reboot_manager_module.runtime_session,
        "record_kernel_presentation_notice",
        lambda *a, **k: {"message_id": "mirror"},
    )

    result = await manager._deliver(record, starting=False)
    assert result == {"sent": True, "sender": "zelda", "message_id": 99}
    assert captured["chat_id"] == 123


# --- candidate cache reuse ------------------------------------------------


class _CachedWorkers:
    def __init__(self):
        self.qualified_calls = 0
        self.qualify_calls = 0
        self.remembered = []
        self.prepare_generation_calls = []
        self.prepare_worker_calls = []
        self.generation = SimpleNamespace(
            manifest=SimpleNamespace(
                generation_id="sha256:cached",
                entries=[SimpleNamespace(module="m", relative_path="m.py", sha256="0" * 64)],
            ),
            receipt=SimpleNamespace(
                probe_pid=1,
                runtime=SimpleNamespace(runtime_id="core-runtime"),
            ),
        )

    async def qualified_generation(self):
        self.qualified_calls += 1
        return self.generation

    def qualify_generation(self):
        self.qualify_calls += 1
        return self.generation

    def remember_generation(self, generation):
        self.remembered.append(generation)

    async def prepare_generation(self, generation):
        self.prepare_generation_calls.append(generation)
        return generation, "generation-root"

    async def prepare_worker(self, name, generation, generation_root=None):
        self.prepare_worker_calls.append((name, generation_root))
        return f"client-{name}"


def _cache_manager(workers):
    return RebootManager(SimpleNamespace(paths=None, function_workers=workers), console_handler=None)


@pytest.mark.asyncio
async def test_prepare_candidates_prefers_qualified_generation_cache():
    workers = _CachedWorkers()
    manager = _cache_manager(workers)

    candidates = await manager._prepare_candidates(("zelda",))

    assert workers.qualified_calls == 1
    assert workers.qualify_calls == 0
    assert workers.prepare_generation_calls == [workers.generation]
    assert workers.prepare_worker_calls == [("zelda", "generation-root")]
    assert candidates == {"zelda": "client-zelda"}


@pytest.mark.asyncio
async def test_prepare_candidates_falls_back_without_cache_entrypoint():
    workers = _CachedWorkers()
    workers.qualified_generation = None
    manager = _cache_manager(workers)

    candidates = await manager._prepare_candidates(("zelda",))

    assert workers.qualified_calls == 0
    assert workers.qualify_calls == 1
    assert candidates == {"zelda": "client-zelda"}
