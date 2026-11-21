from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from adapters.antigravity_cli import AntigravityCLIAdapter
from adapters.claude_cli import ClaudeCLIAdapter
from adapters.ollama_api import HASHI_MODEL_CAPACITY_PROFILES as OLLAMA_PROFILES
from adapters.openrouter_api import _usage_cache_tokens
from adapters.xai_api import XaiApiAdapter
from orchestrator.config import AgentConfig, GlobalConfig
from orchestrator.hcc import is_hcc_enabled, set_hcc_enabled
from orchestrator.her_v2.v3_prompt import QUESTION_CONTRACT, compile_main_prompt
from orchestrator.pcm import render_pcm_document
from tools.model_capability_sources import normalize_engine as cap_normalize_engine
from tools.registry import ToolRegistry


def test_hcc_auto_enables_only_when_cached_content_exists_and_not_opted_out(tmp_path: Path):
    md = tmp_path / "agent.md"
    md.write_text(render_pcm_document(persona="P", system="S", memory="M", hcc=None), encoding="utf-8")
    assert is_hcc_enabled(tmp_path) is False

    md.write_text(render_pcm_document(persona="P", system="S", memory="M", hcc="[weather]\nsunny\n[weather_end]"), encoding="utf-8")
    assert is_hcc_enabled(tmp_path) is True

    set_hcc_enabled(tmp_path, False)
    assert is_hcc_enabled(tmp_path) is False


def test_question_contract_omitted_when_disabled_or_ask_user_unavailable():
    system_disabled, _ = compile_main_prompt(
        pcm_input={"current_request": "hi", "sections": [], "history": []},
        fallback_request="hi",
        context={"question_cards_enabled": False, "habit_catalogue": []},
    )
    assert QUESTION_CONTRACT not in system_disabled

    system_unavailable, _ = compile_main_prompt(
        pcm_input={"current_request": "hi"},
        fallback_request="hi",
        context={"ask_user_available": False},
        question_cards_enabled=True,
    )
    assert QUESTION_CONTRACT not in system_unavailable

    system_enabled, _ = compile_main_prompt(
        pcm_input={"current_request": "hi", "sections": [], "history": []},
        fallback_request="hi",
        context={"question_cards_enabled": True, "ask_user_available": True, "habit_catalogue": []},
    )
    assert QUESTION_CONTRACT in system_enabled


def test_tool_registry_config_warnings_wildcard_vision_and_background_hiding(tmp_path: Path):
    reg = ToolRegistry(["file_read", "nonexistent_tool_xyz"], tmp_path, tmp_path, {})
    assert reg.config_warnings and reg.config_warnings[0]["code"] == "unknown_configured_tools"
    assert reg.config_warnings[0]["unknown_tools"] == ["nonexistent_tool_xyz"]

    wildcard_no_media = ToolRegistry(["*"], tmp_path, tmp_path, {})
    assert wildcard_no_media.is_allowed("vision_inspect") is False
    bg_names = {d["function"]["name"] for d in wildcard_no_media.get_tool_definitions()}
    assert "background_job_start" not in bg_names
    assert "background_job_status" not in bg_names

    media_root = tmp_path / "media"
    media_root.mkdir()
    wildcard_with_media = ToolRegistry(["*"], tmp_path, tmp_path, {}, media_roots=[media_root])
    assert wildcard_with_media.is_allowed("vision_inspect") is True
    assert wildcard_with_media.is_allowed("media_read") is True


def test_ollama_capacity_defaults_and_xai_engine_normalization():
    assert "default" in OLLAMA_PROFILES
    assert OLLAMA_PROFILES["default"]["context_window_tokens"] >= 32_768
    assert cap_normalize_engine("xai") == "xai-api"


@pytest.mark.asyncio
async def test_prompt_cache_token_extraction_and_xai_propagation(tmp_path: Path):
    hit, miss = _usage_cache_tokens(
        {"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 250}}
    )
    assert (hit, miss) == (250, 750)

    sys_md = tmp_path / "agent.md"
    sys_md.write_text("# test\n", encoding="utf-8")
    cfg = AgentConfig("x", "xai-api", tmp_path, sys_md, "grok-4.2", True)
    adapter = XaiApiAdapter(cfg, GlobalConfig(authorized_id=0), api_key="k")

    async def handler(_req):
        return httpx.Response(
            200,
            json={
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "prompt_tokens_details": {"cached_tokens": 40},
                },
            },
        )

    adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    res = await adapter._call_api_once({"model": "grok-4.2", "messages": []}, {}, None)
    assert res.prompt_cache_hit_tokens == 40
    assert res.prompt_cache_miss_tokens == 60
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_xai_stream_preserves_cache_usage_and_finish_reason(tmp_path: Path):
    cfg = AgentConfig("x", "xai-api", tmp_path, None, "grok-4.2", True)
    adapter = XaiApiAdapter(cfg, GlobalConfig(authorized_id=0), api_key="k")
    events = [
        {"choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}]},
        {
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": 40},
            },
        },
    ]
    wire = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"

    async def handler(_request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=wire)

    adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await adapter._stream_api_once(
            {"model": "grok-4.2", "messages": [], "stream": True},
            {},
            None,
            api_url="https://api.x.ai/v1/chat/completions",
        )
        assert (result.text, result.finish_reason) == ("hello", "stop")
        assert (result.prompt_cache_hit_tokens, result.prompt_cache_miss_tokens) == (40, 60)
    finally:
        await adapter.shutdown()


def test_claude_cli_cache_usage_and_antigravity_mcp_hook(tmp_path: Path):
    sys_md = tmp_path / "agent.md"
    sys_md.write_text("# prompt\n", encoding="utf-8")
    cfg = AgentConfig("c", "claude-cli", tmp_path, sys_md, "claude-sonnet-4-6", True)
    claude = ClaudeCLIAdapter(cfg, GlobalConfig(authorized_id=0))
    claude._parse_stream_json_line(
        '{"type":"result","result":"done","usage":{"input_tokens":50,"cache_creation_input_tokens":20,"cache_read_input_tokens":300,"output_tokens":15}}',
        None,
    )
    assert claude._last_usage.prompt_cache_hit_tokens == 300
    assert claude._last_usage.prompt_cache_miss_tokens == 70
    assert not hasattr(claude, "_read_blocking")

    agy_cfg = AgentConfig("a", "antigravity-cli", tmp_path, sys_md, "gemini-3.8-flash-high", True)
    agy = AntigravityCLIAdapter(agy_cfg, GlobalConfig(authorized_id=0))
    assert agy._hashi_mcp_enabled is False
