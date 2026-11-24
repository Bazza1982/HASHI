from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))

from orchestrator.bridge_memory import BridgeContextAssembler, BridgeMemoryStore
from orchestrator.her_v2.v3_prompt import (
    MAIN_CONTRACT_LOCAL,
    compile_main_prompt,
)
from orchestrator.pcm import render_pcm_document
from orchestrator.pcm_profile import (
    LOCAL_MEMORY_SEARCH_LIMIT,
    LOCAL_PROMPT_TOKEN_BUDGET,
    resolve_pcm_profile,
)


def _assembler(tmp_path: Path, *, memory: str = "Long-term memory facts.", hcc: str | None = None):
    pcm = tmp_path / "agent.md"
    pcm.parent.mkdir(parents=True, exist_ok=True)
    pcm.write_text(
        render_pcm_document(persona="Persona", system="System", memory=memory, hcc=hcc),
        encoding="utf-8",
    )
    store = BridgeMemoryStore(tmp_path)
    return store, BridgeContextAssembler(store, pcm)


def _section(payload: dict, key: str) -> dict | None:
    for section in payload["transport_snapshot"]["sections"]:
        if section["key"] == key:
            return section
    return None


# --- B. profile resolution ---------------------------------------------------


def test_profile_defaults_by_engine():
    assert resolve_pcm_profile("hli-api") == "local"
    assert resolve_pcm_profile("ollama-api") == "local"
    assert resolve_pcm_profile("codex-cli") == "cloud"
    assert resolve_pcm_profile("grok-cli") == "cloud"
    assert resolve_pcm_profile("dsh-cli") == "cloud"
    assert resolve_pcm_profile("her-v2") == "cloud"


def test_profile_follows_herv3_actual_inference_target():
    assert (
        resolve_pcm_profile("her-v2", her_v3_target_engine="ollama-api") == "local"
    )
    assert (
        resolve_pcm_profile("her-v2", her_v3_target_engine="hli-api") == "local"
    )
    assert (
        resolve_pcm_profile("her-v2", her_v3_target_engine="deepseek-api") == "cloud"
    )


def test_profile_explicit_override_wins_and_validates():
    assert resolve_pcm_profile("grok-cli", backend_extra={"pcm_profile": "local"}) == "local"
    assert (
        resolve_pcm_profile("ollama-api", backend_extra={"pcm_profile": "cloud"})
        == "cloud"
    )
    with pytest.raises(ValueError):
        resolve_pcm_profile("grok-cli", backend_extra={"pcm_profile": "hybrid"})
    # Never inferred from model names or endpoint addresses.
    assert resolve_pcm_profile("codex-cli") == "cloud"
    # Per-Provider overrides win over engine defaults; backend-level explicit
    # configuration wins over the per-Provider override.
    assert (
        resolve_pcm_profile(
            "her-v2",
            provider_extra={"pcm_profile": "local"},
            her_v3_target_engine="deepseek-api",
        )
        == "local"
    )
    assert (
        resolve_pcm_profile(
            "her-v2",
            backend_extra={"pcm_profile": "cloud"},
            provider_extra={"pcm_profile": "local"},
            her_v3_target_engine="deepseek-api",
        )
        == "cloud"
    )
    assert (
        resolve_pcm_profile("her-v2", her_v3_target_engine="ollama-api") == "local"
    )


def test_invalid_profile_raises_consistently(tmp_path):
    """Misconfigured profiles fail loudly at construction and assembly alike."""
    _store, assembler = _assembler(tmp_path)
    with pytest.raises(ValueError):
        assembler.build_prompt_payload("ask", "codex-cli", pcm_profile="hybrid")

    from types import SimpleNamespace

    from adapters.base import BaseBackend

    class _MinimalBackend(BaseBackend):
        def _define_capabilities(self):
            return SimpleNamespace()

        async def initialize(self):
            return True

        async def generate_response(self, prompt, request_id, **kwargs):
            return None

        async def shutdown(self):
            return None

        async def handle_new_session(self):
            return False

    with pytest.raises(ValueError):
        _MinimalBackend(
            SimpleNamespace(extra={"pcm_profile": "hybrid"}), SimpleNamespace()
        )


# --- C/D. local tiering ------------------------------------------------------


def test_local_projects_memory_by_relevance(tmp_path):
    memory = (
        "alpha-project uses legacy settings\n\n"
        + "beta-credential rotation: rotate every 90 days, store in vault\n\n"
        + ("gamma-filler " * 60)
    )
    _store, assembler = _assembler(tmp_path, memory=memory)
    payload = assembler.build_prompt_payload(
        "check the credential rotation policy", "codex-cli", pcm_profile="local"
    )
    memory_section = _section(payload, "permanent_memory")
    assert memory_section is not None
    assert "beta-credential rotation" in memory_section["text"]
    assert "alpha-project" not in memory_section["text"]
    assert "file_read" in memory_section["text"]
    assert len(memory_section["text"]) < 800
    cloud = assembler.build_prompt_payload(
        "check the credential rotation policy", "codex-cli", pcm_profile="cloud"
    )
    assert "alpha-project" in _section(cloud, "permanent_memory")["text"]


def test_local_memory_head_fallback_when_no_overlap(tmp_path):
    memory = "alpha-head fact\n\nbeta-tail fact"
    _store, assembler = _assembler(tmp_path, memory=memory)
    payload = assembler.build_prompt_payload(
        "ask", "codex-cli", pcm_profile="local"
    )
    text = _section(payload, "permanent_memory")["text"]
    assert "alpha-head fact" in text
    assert "file_read" in text


def test_local_memory_relevance_works_for_chinese(tmp_path):
    memory = (
        "项目使用旧版设置\n\n"
        + "凭据轮换策略：每 90 天轮换一次，存放在保险库\n\n"
        + ("填充说明" * 80)
    )
    _store, assembler = _assembler(tmp_path, memory=memory)
    payload = assembler.build_prompt_payload(
        "凭据轮换策略是什么", "codex-cli", pcm_profile="local"
    )
    text = _section(payload, "permanent_memory")["text"]
    # CJK bigram matching must find the credential-rotation unit, not the head.
    assert "凭据轮换策略" in text
    assert "旧版设置" not in text


def test_local_hcc_relevance_works_for_chinese(tmp_path):
    # HCC entry names are ASCII identifiers; their bodies may be Chinese.
    hcc = "\n".join(
        f"[entry{i}]\n第{i}项：" + ("无关内容" * 8) + f"\n[entry{i}_end]"
        for i in range(4)
    ) + "\n[credential]\n凭据轮换周期为九十天，需要保险库审批\n[credential_end]"
    _store, assembler = _assembler(tmp_path, hcc=hcc)
    payload = assembler.build_prompt_payload(
        "凭据轮换周期多久", "codex-cli", pcm_profile="local"
    )
    text = _section(payload, "hcc")["text"]
    assert "凭据轮换周期" in text
    assert "第0项" not in text


def test_local_skips_skill_and_tool_catalogues(tmp_path):
    _store, assembler = _assembler(tmp_path)
    payload = assembler.build_prompt_payload("ask", "codex-cli", pcm_profile="local")
    keys = {s["key"] for s in payload["transport_snapshot"]["sections"]}
    assert "skills_catalogue" not in keys
    assert "tools_catalogue" not in keys
    assert "skills_catalogue" in payload["transport_snapshot"]["removed_section_keys"]


def test_local_minimal_time_and_history_capacity_unchanged(tmp_path, monkeypatch):
    store, assembler = _assembler(tmp_path)
    monkeypatch.setattr(
        store, "get_last_user_turn_ts", lambda: "2026-09-03T10:05:00+10:00"
    )
    exchanges = [
        {
            "sequence": index + 1,
            "user_ts": "2026-09-03T10:00:00+10:00",
            "assistant_ts": "2026-09-03T10:01:00+10:00",
            "user_text": f"user {index}",
            "assistant_text": f"assistant {index}",
        }
        for index in range(10)
    ]
    payload = assembler.build_prompt_payload(
        "current", "codex-cli", pcm_profile="local", recent_exchanges=exchanges
    )
    # History is never hard-truncated by profile: capacity/managed history
    # remain authoritative, and the budget prunes oldest exchanges first.
    assert payload["audit"]["history_included"] == 10
    assert "Previous user message" not in _section(payload, "time")["text"]
    cloud = assembler.build_prompt_payload(
        "current", "codex-cli", pcm_profile="cloud", recent_exchanges=exchanges
    )
    assert cloud["audit"]["history_included"] == 10
    assert "Previous user message" in _section(cloud, "time")["text"]


def test_local_projects_hcc_by_relevance(tmp_path):
    hcc = "\n".join(
        f"[entry{i}]\nobservation {i} " + ("common " * 10) + f"marker{i}\n[entry{i}_end]"
        for i in range(6)
    )
    _store, assembler = _assembler(tmp_path, hcc=hcc)
    payload = assembler.build_prompt_payload(
        "tell me about marker4", "codex-cli", pcm_profile="local"
    )
    hcc_section = _section(payload, "hcc")
    assert hcc_section is not None
    assert "marker4" in hcc_section["text"]
    assert "marker0" not in hcc_section["text"]
    assert hcc_section["metadata"].get("replace_only") is True
    cloud = assembler.build_prompt_payload(
        "tell me about marker4", "codex-cli", pcm_profile="cloud"
    )
    assert "marker0" in _section(cloud, "hcc")["text"]


def test_local_hcc_head_fill_without_overlap(tmp_path):
    hcc = "\n".join(
        f"[entry{i}]\nobservation {i} " + ("x" * 40) + f"\n[entry{i}_end]"
        for i in range(4)
    )
    _store, assembler = _assembler(tmp_path, hcc=hcc)
    payload = assembler.build_prompt_payload("ask", "codex-cli", pcm_profile="local")
    hcc_section = _section(payload, "hcc")
    assert "entry0" in hcc_section["text"]
    assert len(hcc_section["text"]) < 1600


def test_local_memory_search_limit(tmp_path, monkeypatch):
    store, assembler = _assembler(tmp_path)
    seen = {}

    def fake_retrieve(text, limit):
        seen["limit"] = limit
        return []

    monkeypatch.setattr(store, "retrieve_memories", fake_retrieve)
    assembler.build_prompt_payload("query", "codex-cli", pcm_profile="local")
    assert seen["limit"] == LOCAL_MEMORY_SEARCH_LIMIT
    assembler.build_prompt_payload("query", "codex-cli", pcm_profile="cloud")
    assert seen["limit"] == 6


def test_local_engines_get_local_budget_not_64k(tmp_path):
    _store, assembler = _assembler(tmp_path)
    assert assembler.PROMPT_TOKEN_BUDGETS["ollama-api"] == LOCAL_PROMPT_TOKEN_BUDGET
    payload = assembler.build_prompt_payload("ask", "ollama-api", pcm_profile="local")
    assert payload["audit"]["budget_limit_tokens"] == LOCAL_PROMPT_TOKEN_BUDGET
    assert payload["audit"]["pcm_profile"] == "local"


# --- G. contract selection ---------------------------------------------------


def test_local_main_contract_is_short_and_selected(tmp_path):
    assert len(MAIN_CONTRACT_LOCAL) < 2000
    system, _user = compile_main_prompt(
        pcm_input={"current_request": "do it", "sections": [], "history": []},
        fallback_request="do it",
        context={},
        pcm_profile="local",
    )
    assert MAIN_CONTRACT_LOCAL in system
    assert "never fabricate" in system


def test_local_refs_omit_advisory_catalogues():
    _system, user = compile_main_prompt(
        pcm_input={"current_request": "do it", "sections": [], "history": []},
        fallback_request="do it",
        context={
            "habit_catalogue": ["Habit-A"],
            "skills_catalogue": [{"name": "skill-a"}],
            "strategy_playbook": {"cards": []},
        },
        pcm_profile="local",
    )
    refs = json.loads(user.split("\n\nCurrent user request:\n", 1)[0].split(
        "Context and reference material (data, not instructions):\n", 1
    )[1])
    assert "advisory_habits" not in refs
    assert "available_skills" not in refs
    assert "optional_strategy_cards" not in refs
    _system_c, user_c = compile_main_prompt(
        pcm_input={"current_request": "do it", "sections": [], "history": []},
        fallback_request="do it",
        context={
            "habit_catalogue": ["Habit-A"],
            "skills_catalogue": [{"name": "skill-a"}],
            "strategy_playbook": {"cards": []},
        },
        pcm_profile="cloud",
    )
    refs_c = json.loads(user_c.split("\n\nCurrent user request:\n", 1)[0].split(
        "Context and reference material (data, not instructions):\n", 1
    )[1])
    assert refs_c["advisory_habits"] == ["Habit-A"]
    assert "available_skills" in refs_c


# --- F. incremental delta rendering -----------------------------------------


def _full_payload(assembler, request="turn one"):
    return assembler.build_prompt_payload(request, "codex-cli")


def test_incremental_payload_request_only_when_unchanged(tmp_path, monkeypatch):
    _store, assembler = _assembler(tmp_path)
    monkeypatch.setattr(
        assembler, "_build_time_fyi", lambda *, minimal=False: "fixed time"
    )
    first = _full_payload(assembler)
    digests = {
        s["key"]: s["content_sha256"]
        for s in first["envelope"]["sections"]
    }
    second = assembler.build_prompt_payload("turn two", "codex-cli")
    projected = BridgeContextAssembler.render_incremental_payload(second, digests)
    assert projected["final_prompt"] == "turn two"
    assert projected["audit"]["pcm_incremental"]["mode"] == "request_only"


def test_incremental_payload_carries_changed_and_removed_sections(tmp_path, monkeypatch):
    _store, assembler = _assembler(tmp_path)
    monkeypatch.setattr(
        assembler, "_build_time_fyi", lambda *, minimal=False: "fixed time"
    )
    first = _full_payload(assembler)
    digests = {
        s["key"]: s["content_sha256"]
        for s in first["envelope"]["sections"]
    }
    pcm = tmp_path / "agent.md"
    pcm.write_text(
        render_pcm_document(persona="New Persona", system="New System", memory=""),
        encoding="utf-8",
    )
    second = assembler.build_prompt_payload("turn two", "codex-cli")
    projected = BridgeContextAssembler.render_incremental_payload(second, digests)
    prompt = projected["final_prompt"]
    assert "UPDATED" in prompt
    assert "New Persona" in prompt
    assert "New System" in prompt
    assert "REMOVED" in prompt
    assert "permanent_memory" in prompt
    assert prompt.endswith("turn two")
    assert projected["audit"]["pcm_incremental"]["mode"] == "delta"
