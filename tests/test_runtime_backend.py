from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_backend


def _update():
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        effective_chat=SimpleNamespace(id=456),
    )


def _context(*args):
    return SimpleNamespace(args=list(args))


def _runtime(agent_mode="flex", allowed_backends=None, available_models=None):
    replies = []
    switches = []
    runtime = SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        backend_manager=SimpleNamespace(agent_mode=agent_mode),
        config=SimpleNamespace(allowed_backends=allowed_backends or [{"engine": "claude-cli"}, {"engine": "openai-api"}]),
        _build_backend_menu_text=lambda: "BACKEND MENU",
        _backend_keyboard=lambda: "BACKEND KB",
        _build_backend_model_prompt=lambda engine, with_context: f"PROMPT:{engine}:{with_context}",
        _backend_model_keyboard=lambda engine, with_context: f"KB:{engine}:{with_context}",
        _get_available_models_for=lambda engine: available_models.get(engine, []) if available_models else [],
    )

    async def _reply_text(update, text, **kwargs):
        replies.append((text, kwargs))

    async def _switch_backend_mode(chat_id, target_engine, target_model=None, with_context=False):
        switches.append((chat_id, target_engine, target_model, with_context))
        return True, f"switched:{target_engine}:{target_model}:{with_context}"

    runtime._reply_text = _reply_text
    runtime._switch_backend_mode = _switch_backend_mode
    return runtime, replies, switches


@pytest.mark.asyncio
async def test_cmd_backend_blocks_in_fixed_mode():
    runtime, replies, switches = _runtime(agent_mode="fixed")

    await runtime_backend.cmd_backend(runtime, _update(), _context())

    assert "Backend switching is disabled" in replies[-1][0]
    assert replies[-1][1]["parse_mode"] == "Markdown"
    assert switches == []


@pytest.mark.asyncio
async def test_cmd_backend_shows_menu_without_args():
    runtime, replies, switches = _runtime()

    await runtime_backend.cmd_backend(runtime, _update(), _context())

    assert replies[-1][0] == "BACKEND MENU"
    assert replies[-1][1]["reply_markup"] == "BACKEND KB"
    assert switches == []


@pytest.mark.asyncio
async def test_cmd_backend_rejects_disallowed_engine():
    runtime, replies, switches = _runtime()

    await runtime_backend.cmd_backend(runtime, _update(), _context("gemini-cli"))

    assert replies[-1][0] == "Backend not allowed: gemini-cli"
    assert switches == []


@pytest.mark.asyncio
async def test_cmd_backend_prompts_for_model_when_none_selected():
    runtime, replies, switches = _runtime()

    await runtime_backend.cmd_backend(runtime, _update(), _context("openai-api", "+"))

    assert replies[-1][0] == "PROMPT:openai-api:True"
    assert replies[-1][1]["reply_markup"] == "KB:openai-api:True"
    assert switches == []


@pytest.mark.asyncio
async def test_cmd_backend_rejects_unknown_model():
    runtime, replies, switches = _runtime(available_models={"openai-api": ["gpt-5.4"]})

    await runtime_backend.cmd_backend(runtime, _update(), _context("openai-api", "bad-model"))

    assert replies[-1][0] == "Unknown model for openai-api: bad-model\nUse /backend openai-api to see available options."
    assert switches == []


@pytest.mark.asyncio
async def test_cmd_backend_switches_with_model_and_context():
    runtime, replies, switches = _runtime(available_models={"claude-cli": ["claude-sonnet-4-6"]})

    await runtime_backend.cmd_backend(runtime, _update(), _context("claude-cli", "+", "sonnet"))

    assert switches == [(456, "claude-cli", "claude-sonnet-4-6", True)]
    assert replies[-1][0] == "switched:claude-cli:claude-sonnet-4-6:True"
