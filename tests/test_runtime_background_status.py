from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator import runtime_background_status
from orchestrator.privacy_levels import PrivacyLevel


class _Logger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, message: str) -> None:
        self.messages.append(message)

    def warning(self, message: str) -> None:
        self.messages.append(message)


class _Bot:
    def __init__(self) -> None:
        self.edits: list[dict] = []
        self.deleted: list[dict] = []

    async def edit_message_text(self, **kwargs) -> None:
        self.edits.append(kwargs)

    async def delete_message(self, **kwargs) -> None:
        self.deleted.append(kwargs)


def _item(**overrides):
    values = {
        "request_id": "req-1",
        "chat_id": 123,
        "silent": False,
        "deliver_to_telegram": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _runtime(persona_path: Path, renderer: AsyncMock):
    logger = _Logger()
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            active_backend="her-v2",
            system_md=str(persona_path),
            extra={
                "background_mode": True,
                "background_status_text": "legacy hard-coded status",
                "typing_message": "legacy typing placeholder",
            },
            allowed_backends=[],
        ),
        backend_manager=SimpleNamespace(
            current_backend=SimpleNamespace(run_habit_dream_model=renderer),
        ),
        logger=logger,
        error_logger=logger,
        app=SimpleNamespace(bot=_Bot()),
        maintenance_events=[],
    )
    runtime._log_maintenance = (
        lambda item, event, **fields: runtime.maintenance_events.append(
            (event, fields)
        )
    )
    runtime.sent = []

    async def send_long_message(**kwargs):
        runtime.sent.append(kwargs)
        return 0.01, 1

    runtime.send_long_message = send_long_message
    return runtime


@pytest.mark.asyncio
async def test_status_is_model_authored_from_exact_persona_and_invalidates_on_change(
    tmp_path,
):
    persona_path = tmp_path / "agent.md"
    persona_path.write_text("Address the user as Captain. Sign as Nova.", encoding="utf-8")
    renderer = AsyncMock(
        side_effect=[
            SimpleNamespace(is_success=True, text="Captain, Nova is still on it. ✨"),
            SimpleNamespace(is_success=True, text="Doctor, Iris is working backstage. 🌙"),
        ]
    )
    runtime = _runtime(persona_path, renderer)
    item = _item()

    first = runtime_background_status.prepare(runtime, item)
    assert first is not None
    await first
    assert runtime._persona_background_status_cache.text == (
        "Captain, Nova is still on it. ✨"
    )
    assert runtime_background_status.prepare(runtime, item) is None
    assert renderer.await_count == 1
    assert "Address the user as Captain" in renderer.await_args_list[0].args[0]
    assert "legacy hard-coded status" not in renderer.await_args_list[0].args[0]

    persona_path.write_text("Address the user as Doctor. Sign as Iris.", encoding="utf-8")
    second = runtime_background_status.prepare(runtime, item)
    assert second is not None
    await second

    assert renderer.await_count == 2
    assert "Address the user as Doctor" in renderer.await_args_list[1].args[0]
    assert runtime._persona_background_status_cache.text == (
        "Doctor, Iris is working backstage. 🌙"
    )


@pytest.mark.asyncio
async def test_status_edits_placeholder_once_and_final_wait_can_join_it(tmp_path):
    persona_path = tmp_path / "agent.md"
    persona_path.write_text("Warm Persona", encoding="utf-8")
    renderer = AsyncMock(
        return_value=SimpleNamespace(
            is_success=True,
            text="I'm continuing in the background and will return here. 🌸",
        )
    )
    runtime = _runtime(persona_path, renderer)
    item = _item()
    await runtime_background_status.prepare(runtime, item)
    blocker = asyncio.Event()

    async def generation():
        await blocker.wait()
        return SimpleNamespace(is_success=True, text="done")

    generation_task = asyncio.create_task(generation())
    placeholder = SimpleNamespace(message_id=99)
    delivery = runtime_background_status.schedule_delivery(
        runtime,
        item,
        generation_task,
        placeholder,
    )
    await runtime_background_status.wait_for_delivery(item)

    assert delivery.done()
    assert runtime.app.bot.edits == [
        {
            "chat_id": 123,
            "message_id": 99,
            "text": "I'm continuing in the background and will return here. 🌸",
        }
    ]
    assert runtime.sent == []
    assert runtime.maintenance_events == [
        (
            "bg_persona_status",
            {
                "delivered": True,
                "persona_sha256": runtime._persona_background_status_cache.persona_sha256[
                    :12
                ],
            },
        )
    ]
    blocker.set()
    await generation_task


@pytest.mark.asyncio
async def test_status_uses_one_off_send_without_placeholder(tmp_path):
    persona_path = tmp_path / "agent.md"
    persona_path.write_text("Concise Persona", encoding="utf-8")
    renderer = AsyncMock(
        return_value=SimpleNamespace(is_success=True, text="Still working backstage. 🎭")
    )
    runtime = _runtime(persona_path, renderer)
    item = _item()
    await runtime_background_status.prepare(runtime, item)
    blocker = asyncio.Event()
    generation_task = asyncio.create_task(blocker.wait())

    delivery = runtime_background_status.schedule_delivery(
        runtime,
        item,
        generation_task,
        None,
    )
    await delivery

    assert runtime.sent == [
        {
            "chat_id": 123,
            "text": "Still working backstage. 🎭",
            "request_id": "req-1",
            "purpose": "background-persona-status",
        }
    ]
    blocker.set()
    await generation_task


@pytest.mark.asyncio
async def test_completed_generation_wins_race_and_no_late_status_is_sent(tmp_path):
    persona_path = tmp_path / "agent.md"
    persona_path.write_text("Slow Persona", encoding="utf-8")
    release_renderer = asyncio.Event()

    async def render(*args, **kwargs):
        await release_renderer.wait()
        return SimpleNamespace(is_success=True, text="Too late")

    runtime = _runtime(persona_path, AsyncMock(side_effect=render))
    item = _item()
    generation_task = asyncio.create_task(asyncio.sleep(0))
    await generation_task
    placeholder = SimpleNamespace(message_id=88)

    delivery = runtime_background_status.schedule_delivery(
        runtime,
        item,
        generation_task,
        placeholder,
    )
    await delivery

    assert runtime.app.bot.edits == []
    assert runtime.sent == []
    assert runtime.app.bot.deleted == [{"chat_id": 123, "message_id": 88}]
    release_renderer.set()
    await asyncio.gather(*runtime._persona_background_status_tasks)


@pytest.mark.asyncio
async def test_persona_edit_during_render_never_delivers_stale_voice(tmp_path):
    persona_path = tmp_path / "agent.md"
    persona_path.write_text("Old Persona Voice", encoding="utf-8")
    release_old = asyncio.Event()

    async def render(prompt, **kwargs):
        if "Old Persona Voice" in prompt:
            await release_old.wait()
            return SimpleNamespace(is_success=True, text="old voice")
        return SimpleNamespace(is_success=True, text="new voice")

    runtime = _runtime(persona_path, AsyncMock(side_effect=render))
    item = _item()
    old_render = runtime_background_status.prepare(runtime, item)
    assert old_render is not None
    await asyncio.sleep(0)
    persona_path.write_text("New Persona Voice", encoding="utf-8")
    generation_blocker = asyncio.Event()
    generation_task = asyncio.create_task(generation_blocker.wait())

    delivery = runtime_background_status.schedule_delivery(
        runtime,
        item,
        generation_task,
        SimpleNamespace(message_id=66),
    )
    await delivery

    assert runtime.app.bot.edits[0]["text"] == "new voice"
    release_old.set()
    await old_render
    assert runtime._persona_background_status_cache.text == "new voice"
    generation_blocker.set()
    await generation_task


@pytest.mark.asyncio
async def test_non_her_agent_uses_allowed_tool_free_api_renderer(tmp_path):
    persona_path = tmp_path / "agent.md"
    persona_path.write_text("Call the user Commander.", encoding="utf-8")
    tool_free_renderer = AsyncMock(
        return_value=SimpleNamespace(is_success=True, text="Commander, I'll return soon. 🛰️")
    )
    logger = _Logger()
    manager = SimpleNamespace(
        current_backend=SimpleNamespace(),
        privacy_level=PrivacyLevel.PROVIDER_TRUST,
        generate_tool_free_ephemeral_response=tool_free_renderer,
    )
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            active_backend="codex-cli",
            system_md=str(persona_path),
            extra={"background_mode": True},
            allowed_backends=[
                {"engine": "codex-cli", "model": "gpt-test"},
                {"engine": "openrouter-api", "model": "vendor/model"},
            ],
        ),
        backend_manager=manager,
        logger=logger,
        error_logger=logger,
    )

    task = runtime_background_status.prepare(runtime, _item())
    assert task is not None
    await task

    assert runtime._persona_background_status_cache.text == (
        "Commander, I'll return soon. 🛰️"
    )
    tool_free_renderer.assert_awaited_once()
    call = tool_free_renderer.await_args.kwargs
    assert call["engine"] == "openrouter-api"
    assert call["model"] == "vendor/model"
    assert "Call the user Commander" in call["prompt"]


def test_legacy_status_templates_are_not_a_persona_fallback(tmp_path):
    missing = tmp_path / "missing-agent.md"
    renderer = AsyncMock()
    runtime = _runtime(missing, renderer)

    assert runtime_background_status.prepare(runtime, _item()) is None
    renderer.assert_not_called()
