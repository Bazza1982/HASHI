from __future__ import annotations

import json
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from tools.tool_output_store import ToolOutputStore


def binding(name):
    return ("HASHI3", "agent", "owner", "session", name, "request-" + name)


def _abandoned_writer(home, entered):
    store = ToolOutputStore(agent_home=home)

    def chunks():
        yield "unfinished" * 10_000
        entered.set()
        Event().wait(30)

    store.retain_text_chunks(binding("abandoned"), "file_read", chunks())


@pytest.mark.parametrize("replacement", [False, True])
def test_slow_writer_does_not_block_other_run(tmp_path, replacement):
    first, second = binding("slow"), binding("other")
    entered, release = Event(), Event()
    store = ToolOutputStore(agent_home=tmp_path)
    other = ToolOutputStore(agent_home=tmp_path) if replacement else store
    original = other.retain(second, "file_read", "previous result")

    def slow_chunks():
        entered.set()
        assert release.wait(10)
        yield "slow result"

    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(store.retain_text_chunks, first, "file_read", slow_chunks())
        assert entered.wait(5)
        try:
            read = pool.submit(other.page, second, original, 0)
            assert json.loads(read.result(timeout=3))["text"] == "previous result"
        finally:
            release.set()
        ref = slow.result(timeout=10)
    assert json.loads(other.page(first, ref, 0))["text"] == "slow result"


def test_stopped_run_cannot_publish_after_streaming_finishes(tmp_path):
    current = binding("cancelled")
    state = {current: "running"}
    entered, release = Event(), Event()
    store = ToolOutputStore(agent_home=tmp_path, state_resolver=state.get)

    def slow_chunks():
        entered.set()
        assert release.wait(10)
        yield "late result"

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(store.retain_text_chunks, current, "file_read", slow_chunks())
        assert entered.wait(5)
        state[current] = "stopped"
        release.set()
        assert pending.result(timeout=10) is None
    assert not store.has_bound(current)


def test_reconciliation_preserves_an_active_stage(tmp_path):
    current = binding("active")
    entered, release = Event(), Event()
    store = ToolOutputStore(agent_home=tmp_path, state_resolver=lambda _: "running")
    other = ToolOutputStore(agent_home=tmp_path, state_resolver=lambda _: "running")

    def chunks():
        entered.set()
        assert release.wait(10)
        yield "live output"

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(store.retain_text_chunks, current, "file_read", chunks())
        assert entered.wait(5)
        try:
            assert len(list(store.private_root.glob(".staging-*"))) == 1
            other.reconcile_terminal()
            assert len(list(store.private_root.glob(".staging-*"))) == 1
        finally:
            release.set()
        ref = pending.result(timeout=10)
    assert json.loads(other.page(current, ref, 0))["text"] == "live output"


def test_replacement_reclaims_stage_after_writer_process_dies(tmp_path):
    survivor = binding("survivor")
    store = ToolOutputStore(agent_home=tmp_path)
    ref = store.retain(survivor, "file_read", "keep this")
    ctx = multiprocessing.get_context("spawn")
    entered = ctx.Event()
    process = ctx.Process(target=_abandoned_writer, args=(tmp_path, entered))
    process.start()
    try:
        assert entered.wait(15)
        assert list(store.private_root.glob(".staging-*"))
        with ThreadPoolExecutor(max_workers=1) as pool:
            probe = pool.submit(lambda: (store.reconcile_terminal(), store.page(survivor, ref, 0)))
            try:
                _, page = probe.result(timeout=5)
                assert json.loads(page)["text"] == "keep this"
                assert list(store.private_root.glob(".staging-*"))
            finally:
                if process.is_alive():
                    process.terminate()
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=10)
    assert not process.is_alive()

    replacement = ToolOutputStore(
        agent_home=tmp_path,
        state_resolver=lambda b: "stopped" if b == binding("abandoned") else "running",
    )
    replacement.reconcile_terminal()
    assert not list(store.private_root.glob(".staging-*"))
    assert not replacement.has_bound(binding("abandoned"))
    assert json.loads(replacement.page(survivor, ref, 0))["text"] == "keep this"


def test_concurrent_search_stages_obey_the_shared_entry_budget(tmp_path):
    current = binding("search")
    first = ToolOutputStore(agent_home=tmp_path)
    second = ToolOutputStore(agent_home=tmp_path)
    barrier = Barrier(2, timeout=10)

    def chunks(value):
        barrier.wait()
        yield value

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(store.retain_search_continuation, current, chunks(value),
                               max_entries=1, max_bytes=0, max_state_bytes=0,
                               expires_at=0)
                   for store, value in [(first, "one"), (second, "two")]]
        references = [task.result(timeout=15) for task in pending]
    assert sum(reference is not None for reference in references) == 1
    reference = next(reference for reference in references if reference is not None)
    assert first.read_search_continuation(current, reference, max_state_bytes=0) in {"one", "two"}


def test_reconciliation_preserves_unrecognized_stage_contents(tmp_path):
    store = ToolOutputStore(agent_home=tmp_path)
    root = store.prepare_private_root()
    stage = root / ".staging-unrecognized"
    stage.mkdir()
    note = stage / "unrecognized.txt"
    note.write_text("preserve", encoding="utf-8")
    store.reconcile_terminal()
    assert note.read_text(encoding="utf-8") == "preserve"
