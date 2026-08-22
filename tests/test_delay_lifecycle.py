from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import runtime_control, runtime_remote, runtime_workspace
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.scheduler import TaskScheduler


def _scheduler(tmp_path) -> TaskScheduler:
    return TaskScheduler(
        tasks_path=tmp_path / "tasks.json",
        state_path=tmp_path / "scheduler_state.json",
        runtimes=[],
        authorized_id=1,
    )


async def _schedule(scheduler: TaskScheduler, *, agent_name: str = "zelda") -> None:
    await scheduler.schedule_delayed_message(
        agent_name=agent_name,
        chat_id=42,
        prompt="future work",
        delay_minutes=5,
    )


def _update() -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=42),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "label"),
    [(runtime_workspace.cmd_wipe, "Wipe"), (runtime_workspace.cmd_reset, "Reset")],
)
async def test_workspace_destructive_commands_block_while_delays_exist(
    tmp_path,
    handler,
    label,
):
    scheduler = _scheduler(tmp_path)
    await _schedule(scheduler)
    replies: list[str] = []

    async def _reply(_update, text, **_kwargs):
        replies.append(text)

    runtime = SimpleNamespace(
        name="zelda",
        orchestrator=SimpleNamespace(scheduler=scheduler),
        _is_authorized_user=lambda _user_id: True,
        _backend_busy=lambda: False,
        _reply_text=_reply,
    )

    await handler(runtime, _update(), SimpleNamespace(args=["CONFIRM"]))

    assert replies == [
        f"{label} is blocked while 1 delayed message(s) are pending. Use /recall first."
    ]
    assert scheduler.count_delayed_messages("zelda") == 1


@pytest.mark.asyncio
async def test_agent_move_blocks_before_running_migration_script(tmp_path):
    scheduler = _scheduler(tmp_path)
    await _schedule(scheduler, agent_name="sunny")
    send = AsyncMock()
    runtime = SimpleNamespace(
        orchestrator=SimpleNamespace(scheduler=scheduler),
        _send_text=send,
    )

    await runtime_remote.do_move(runtime, _update(), "sunny", "HASHI2", {})

    send.assert_awaited_once()
    assert "Move is blocked" in send.await_args.args[1]
    assert "1</code> delayed message" in send.await_args.args[1]


@pytest.mark.asyncio
async def test_session_transfer_blocks_while_delays_exist(tmp_path):
    scheduler = _scheduler(tmp_path)
    await _schedule(scheduler)
    replies: list[str] = []

    async def _reply(_update, text, **_kwargs):
        replies.append(text)

    runtime = SimpleNamespace(
        name="zelda",
        orchestrator=SimpleNamespace(scheduler=scheduler),
        _is_authorized_user=lambda _user_id: True,
        has_active_transfer=lambda: False,
        _reply_text=_reply,
    )

    await FlexibleAgentRuntime._cmd_bridge_handoff(
        runtime,
        _update(),
        SimpleNamespace(args=["sunny"]),
        mode="transfer",
    )

    assert replies == [
        "Transfer is blocked while 1 delayed message(s) are pending. Use /recall first."
    ]


@pytest.mark.asyncio
async def test_agent_delete_blocks_while_target_owns_delays(tmp_path):
    scheduler = _scheduler(tmp_path)
    await _schedule(scheduler, agent_name="sunny")
    answers: list[tuple[str, bool]] = []

    async def _answer(text="", *, show_alert=False):
        answers.append((text, show_alert))

    orchestrator = SimpleNamespace(
        scheduler=scheduler,
        _runtime_map=lambda: {},
        delete_agent_from_config=AsyncMock(),
    )
    runtime = SimpleNamespace(
        name="zelda",
        orchestrator=orchestrator,
        _is_authorized_user=lambda _user_id: True,
    )
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=1),
        data="agents:confirmdelete:sunny",
        answer=_answer,
    )

    await FlexibleAgentRuntime.callback_agents(
        runtime,
        SimpleNamespace(callback_query=query),
        SimpleNamespace(),
    )

    assert answers == [("Recall 1 delayed message(s) from sunny first.", True)]
    orchestrator.delete_agent_from_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_preserves_and_reports_delayed_messages(tmp_path):
    scheduler = _scheduler(tmp_path)
    await _schedule(scheduler)
    replies: list[str] = []

    async def _reply(_update, text, **_kwargs):
        replies.append(text)

    runtime = SimpleNamespace(
        name="zelda",
        orchestrator=SimpleNamespace(scheduler=scheduler),
        config=SimpleNamespace(active_backend="her-v2", engine="her-v2"),
        logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
        queue=asyncio.Queue(),
        backend_manager=SimpleNamespace(current_backend=None),
        current_request_meta=None,
        last_prompt=None,
        is_generating=False,
        _is_authorized_user=lambda _user_id: True,
        _reply_text=_reply,
    )

    await runtime_control.cmd_stop(runtime, _update(), SimpleNamespace(args=[]))

    assert scheduler.count_delayed_messages("zelda") == 1
    assert "Preserved 1 delayed message(s)" in replies[0]
