import asyncio
import json
from types import SimpleNamespace

import pytest

from adapters.claude_cli import ClaudeCLIAdapter
from adapters.claw_cli import ClawCLIAdapter
from adapters.codex_cli import CodexCLIAdapter
from adapters.deepseek_api import DeepSeekAdapter
from adapters.gemini_cli import GeminiCLIAdapter
from adapters.grok_cli import GrokCLIAdapter
from adapters.ollama_api import OllamaAdapter
from adapters.openrouter_api import OpenRouterAdapter
from adapters.stream_events import KIND_TEXT_DELTA


def _agent_config(tmp_path, *, extra=None):
    return SimpleNamespace(
        name="test",
        model="test-model",
        workspace_dir=tmp_path,
        system_md=None,
        extra=extra or {},
        resolve_access_root=lambda: tmp_path,
    )


def test_openai_compatible_backends_advertise_answer_stream(tmp_path):
    cfg = _agent_config(tmp_path)

    openrouter = OpenRouterAdapter(cfg, SimpleNamespace(), api_key="test-key")
    deepseek = DeepSeekAdapter(cfg, SimpleNamespace(), api_key="test-key")
    ollama = OllamaAdapter(cfg, SimpleNamespace(), api_key=None)

    assert getattr(openrouter.capabilities, "supports_answer_stream", False) is True
    assert getattr(deepseek.capabilities, "supports_answer_stream", False) is True
    assert getattr(ollama.capabilities, "supports_answer_stream", False) is True


def test_stream_json_cli_backends_advertise_answer_stream(tmp_path):
    cfg = _agent_config(tmp_path)

    claude = ClaudeCLIAdapter(cfg, SimpleNamespace(claude_cmd="claude"), api_key="test-key")
    gemini = GeminiCLIAdapter(cfg, SimpleNamespace(gemini_cmd="gemini"), api_key="test-key")
    grok = GrokCLIAdapter(cfg, SimpleNamespace(grok_cmd="grok"), api_key="test-key")

    assert getattr(claude.capabilities, "supports_answer_stream", False) is True
    assert getattr(gemini.capabilities, "supports_answer_stream", False) is True
    assert getattr(grok.capabilities, "supports_answer_stream", False) is True


def test_cli_backends_do_not_advertise_answer_stream_by_default(tmp_path):
    cfg = _agent_config(tmp_path)

    codex_capabilities = CodexCLIAdapter._define_capabilities(
        CodexCLIAdapter.__new__(CodexCLIAdapter)
    )
    claw = ClawCLIAdapter(cfg, SimpleNamespace(), api_key="test-key")

    assert getattr(codex_capabilities, "supports_answer_stream", False) is False
    assert getattr(claw.capabilities, "supports_answer_stream", False) is False


@pytest.mark.asyncio
async def test_claude_text_delta_preserves_full_answer_chunk(tmp_path):
    cfg = _agent_config(tmp_path)
    adapter = ClaudeCLIAdapter(cfg, SimpleNamespace(claude_cmd="claude"), api_key="test-key")
    events = []

    async def collect(event):
        events.append(event)

    long_delta = "x" * 250
    adapter._handle_stream_event(
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": long_delta}},
        collect,
    )
    await asyncio.sleep(0)

    assert len(events) == 1
    assert events[0].kind == KIND_TEXT_DELTA
    assert events[0].summary == long_delta


@pytest.mark.asyncio
async def test_gemini_text_delta_preserves_full_answer_chunk(tmp_path):
    cfg = _agent_config(tmp_path)
    adapter = GeminiCLIAdapter(cfg, SimpleNamespace(gemini_cmd="gemini"), api_key="test-key")
    events = []
    fragments = []

    async def collect(event):
        events.append(event)

    long_delta = "y" * 250
    completed = adapter._parse_stream_json_line(
        json.dumps({"type": "message", "role": "assistant", "content": long_delta, "delta": True}),
        collect,
        fragments,
    )
    await asyncio.sleep(0)

    assert completed is False
    assert fragments == [long_delta]
    assert len(events) == 1
    assert events[0].kind == KIND_TEXT_DELTA
    assert events[0].summary == long_delta
