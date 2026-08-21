from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator import runtime_lifecycle


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)

    def warning(self, message):
        self.messages.append(message)


@pytest.mark.asyncio
async def test_shutdown_cancels_long_batch_timeout_and_finalize_tasks():
    async def _wait_forever():
        await asyncio.Event().wait()

    async def _shutdown_backend():
        return None

    timeout_task = asyncio.create_task(_wait_forever())
    finalize_task = asyncio.create_task(_wait_forever())
    await asyncio.sleep(0)
    shutdown_states = []
    runtime = SimpleNamespace(
        name="zelda",
        logger=_Logger(),
        error_logger=_Logger(),
        is_shutting_down=False,
        _scheduled_retry_tasks=set(),
        _persona_background_status_tasks=set(),
        _background_tasks=set(),
        _long_buffer_timeout_task=timeout_task,
        _long_finalize_task=finalize_task,
        process_task=None,
        backend_manager=SimpleNamespace(shutdown=_shutdown_backend),
        startup_success=False,
        _mark_runtime_shutdown=lambda clean: shutdown_states.append(clean),
    )

    await runtime_lifecycle.shutdown(runtime)

    assert timeout_task.cancelled()
    assert finalize_task.cancelled()
    assert runtime._long_buffer_timeout_task is None
    assert runtime._long_finalize_task is None
    assert shutdown_states == [True]


@pytest.mark.asyncio
async def test_shutdown_marks_unclean_and_finishes_when_queue_ignores_cancel(monkeypatch):
    release_queue = asyncio.Event()
    queue_cancelled = asyncio.Event()
    backend_shutdown = asyncio.Event()
    shutdown_states = []

    async def _stubborn_queue():
        while not release_queue.is_set():
            try:
                await release_queue.wait()
            except asyncio.CancelledError:
                queue_cancelled.set()

    async def _shutdown_backend():
        backend_shutdown.set()

    process_task = asyncio.create_task(_stubborn_queue())
    await asyncio.sleep(0)
    runtime = SimpleNamespace(
        name="samantha",
        logger=_Logger(),
        error_logger=_Logger(),
        is_shutting_down=False,
        _scheduled_retry_tasks=set(),
        _persona_background_status_tasks=set(),
        _background_tasks=set(),
        process_task=process_task,
        backend_manager=SimpleNamespace(shutdown=_shutdown_backend),
        startup_success=False,
        _mark_runtime_shutdown=lambda clean: shutdown_states.append(clean),
    )
    monkeypatch.setattr(
        runtime_lifecycle,
        "RUNTIME_TASK_SHUTDOWN_TIMEOUT_SECONDS",
        0.01,
    )

    with pytest.raises(RuntimeError, match="shutdown was incomplete"):
        await asyncio.wait_for(runtime_lifecycle.shutdown(runtime), timeout=0.5)

    assert queue_cancelled.is_set()
    assert backend_shutdown.is_set()
    assert shutdown_states == [False]
    assert runtime.process_task is process_task
    release_queue.set()
    await process_task


@pytest.mark.asyncio
async def test_shutdown_treats_already_stopped_telegram_actions_as_idempotent():
    shutdown_states = []

    async def _shutdown_backend():
        return None

    async def _not_running():
        raise RuntimeError("This Application is not running!")

    runtime = SimpleNamespace(
        name="samantha",
        logger=_Logger(),
        error_logger=_Logger(),
        is_shutting_down=False,
        _scheduled_retry_tasks=set(),
        _persona_background_status_tasks=set(),
        _background_tasks=set(),
        process_task=None,
        backend_manager=SimpleNamespace(shutdown=_shutdown_backend),
        startup_success=True,
        app=SimpleNamespace(
            updater=SimpleNamespace(stop=_not_running),
            stop=_not_running,
            shutdown=_not_running,
        ),
        _mark_runtime_shutdown=lambda clean: shutdown_states.append(clean),
    )

    await runtime_lifecycle.shutdown(runtime)

    assert shutdown_states == [True]
    assert len(runtime.error_logger.messages) == 3
