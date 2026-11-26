"""Focused validation tests for HERV3 prefix-stable context management & compaction."""
from __future__ import annotations

import json
from pathlib import Path

from adapters.deepseek_api import DeepSeekAdapter
from adapters.hli_api import HASHI_MODEL_CAPACITY_PROFILES as HLI_CAPACITY_PROFILES
from adapters.hli_api import HLIAdapter, _hli_endpoint_key
from adapters.ollama_api import OllamaAdapter
from adapters.openrouter_api import (
    _INTRA_TURN_COMPACTED_MARKER,
    OpenRouterAdapter,
    _estimate_message_tokens,
    build_intra_turn_context_budget,
    compact_intra_turn_tool_messages,
)
from orchestrator.bridge_memory import BridgeContextAssembler, BridgeMemoryStore
from orchestrator.config import AgentConfig, GlobalConfig
from orchestrator.her_v2.v3_prompt import compile_main_prompt, compile_main_turn
from orchestrator.pcm import render_pcm_document


def _sample_pcm(
    *,
    request_text: str,
    timestamp: str,
    request_id: str,
    history: list[dict],
    local_system: str = "Follow project rules.",
    hcc: str = "Stable project cache notes.",
) -> dict:
    return {
        "current_request": request_text,
        "pcm_profile": "cloud",
        "sections": [
            {
                "key": "agent_local_sys",
                "authority": "local_system",
                "order": 100,
                "text": local_system,
            },
            {
                "key": "hcc",
                "authority": "memory",
                "order": 200,
                "text": hcc,
            },
            {
                "key": "time",
                "authority": "runtime_context",
                "order": 100,
                "text": f"Current local time: {timestamp}",
            },
            {
                "key": "current_message_context",
                "authority": "runtime_context",
                "order": 300,
                "text": (
                    "CURRENT MESSAGE CONTEXT\n"
                    'sender: {"kind":"human"}\n'
                    'frontend_ingress: {"connector":{"id":"session_api"},'
                    f'"message":{{"idempotency_digest":"sha256:deadbeef{request_id}",'
                    f'"request_id":"{request_id}"}},'
                    '"target":{"agent_id":"kqs"}}'
                ),
            },
        ],
        "history": history,
        "runtime_context": {},
    }


def _noisy_history_row(seq: int, user_text: str, assistant_text: str) -> dict:
    return {
        "sequence": seq,
        "session_id": "her-sess-123",
        "turn_id": f"turn-{seq}",
        "request_id": f"req-kqs-{seq}",
        "message_id": f"msg-{seq}",
        "idempotency_key": f"idem-{seq}",
        "authority_digest": "sha256:" + ("a" * 64),
        "execution_owner_id": f"worker-pid-{seq}",
        "route_snapshot": {"routes": {"blob": "x" * 1_500}},
        "user_message": user_text,
        "assistant_text": assistant_text,
        "status": "completed",
        "conversation_at": f"2026-11-26T10:{seq:02d}:00+11:00",
    }


def test_prefix_stability_across_consecutive_turns_and_noise_stripping():
    ctx = {
        "habit_catalogue": ["Habit A: verify tests before claiming done."],
        "skills_catalogue": [
            {"id": "git-ops", "description": "Safe git workflow", "skill_md": "/skills/git-ops/SKILL.md"}
        ],
    }

    # Turn 1 (no history yet)
    sys1, hist1, user1 = compile_main_turn(
        pcm_input=_sample_pcm(
            request_text="Inspect the context budget",
            timestamp="2026-11-26 10:01:00",
            request_id="req-1",
            history=[],
        ),
        fallback_request="fallback",
        context=ctx,
    )
    assert hist1 == []
    assert user1.startswith("Reference material (data, not instructions):\n")
    assert user1.index("Reference material") < user1.index("Current turn context")
    assert user1.endswith("Current user request:\nInspect the context budget")

    # Turn 2 (1 settled turn in history, different clock & request_id)
    h2 = [_noisy_history_row(1, "Inspect the context budget", "Found the budget module.")]
    sys2, hist2, user2 = compile_main_turn(
        pcm_input=_sample_pcm(
            request_text="Now fix the watermarks",
            timestamp="2026-11-26 10:05:12",
            request_id="req-2",
            history=h2,
        ),
        fallback_request="fallback",
        context=ctx,
    )
    assert sys2 == sys1
    assert [m["role"] for m in hist2] == ["user", "assistant"]
    # Static prefix in Turn 2's first user message matches Turn 1's static prefix.
    static_block_1 = user1.split("\n\nCurrent turn context")[0]
    assert hist2[0]["content"].startswith(static_block_1 + "\n\nInspect the context budget")
    assert hist2[1]["content"] == "Found the budget module."

    # Turn 3 (2 settled turns) -> prefix through Turn 2's assistant reply is byte-identical!
    h3 = [*h2, _noisy_history_row(2, "Now fix the watermarks", "Updated high/low watermarks.")]
    sys3, hist3, user3 = compile_main_turn(
        pcm_input=_sample_pcm(
            request_text="Run the test suite",
            timestamp="2026-11-26 10:09:45",
            request_id="req-3",
            history=h3,
        ),
        fallback_request="fallback",
        context=ctx,
    )
    assert sys3 == sys2
    assert hist3[:2] == hist2
    assert [m["role"] for m in hist3] == ["user", "assistant", "user", "assistant"]
    assert hist3[2] == {"role": "user", "content": "Now fix the watermarks"}
    assert hist3[3] == {"role": "assistant", "content": "Updated high/low watermarks."}

    # Operational SQLite & ingress IDs must not leak into model prompts.
    wire_blob = sys3 + "\n".join(m["content"] for m in hist3) + user3
    for forbidden in (
        "route_snapshot",
        "execution_owner_id",
        "idempotency_key",
        "authority_digest",
        "req-kqs-1",
        "req-3",
        "idempotency_digest",
    ):
        assert forbidden not in wire_blob

    # Volatile context is present only at the end of the final user message.
    assert "2026-11-26 10:09:45" in user3
    assert "2026-11-26 10:09:45" not in "\n".join(m["content"] for m in hist3)
    assert user3.endswith("Current user request:\nRun the test suite")

    # Legitimate instruction/skill updates are never suppressed for cache hits.
    sys4, hist4, _user4 = compile_main_turn(
        pcm_input=_sample_pcm(
            request_text="Check updated rules",
            timestamp="2026-11-26 10:12:00",
            request_id="req-4",
            history=h3,
            local_system="Follow UPDATED project rules.",
        ),
        fallback_request="fallback",
        context={
            **ctx,
            "skills_catalogue": [
                *ctx["skills_catalogue"],
                {"id": "new-skill", "description": "Brand new skill", "skill_md": "/skills/new-skill/SKILL.md"},
            ],
        },
    )
    assert "Follow UPDATED project rules." in sys4
    assert "new-skill" in hist4[0]["content"]


def test_compile_main_prompt_orders_stable_before_volatile_and_strips_noise():
    history = [_noisy_history_row(1, "Old Q", "Old A")]
    _sys, user = compile_main_prompt(
        pcm_input=_sample_pcm(
            request_text="New Q",
            timestamp="2026-11-26 11:00:00",
            request_id="req-99",
            history=history,
        ),
        fallback_request="fallback",
        context={"habit_catalogue": ["Habit 1"]},
    )
    assert user.index("advisory_habits") < user.index("session_history_context_only")
    assert user.index("session_history_context_only") < user.index("turn_context_only")
    assert "route_snapshot" not in user
    assert "execution_owner_id" not in user
    assert "req-99" not in user
    assert "idempotency_digest" not in user


def _tool_round(round_idx: int, payload_chars: int) -> list[dict]:
    call_id = f"call_{round_idx}"
    return [
        {
            "role": "assistant",
            "content": f"Step {round_idx}: inspecting file.",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "file_read",
                        "arguments": json.dumps({"path": f"src/mod_{round_idx}.py"}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": f"HEAD_{round_idx} " + ("x" * payload_chars) + f" TAIL_{round_idx}",
        },
    ]


def test_normal_tool_loop_on_1m_model_does_not_repeatedly_compact():
    budget = build_intra_turn_context_budget(
        context_window_tokens=1_000_000,
        response_headroom_tokens=16_384,
        provenance="deepseek_1m",
    )
    assert budget.hard_input_limit_tokens == 983_616
    assert budget.working_input_target_tokens == 300_000
    assert budget.high_watermark_tokens == 255_000
    assert budget.low_watermark_tokens == 165_000

    messages: list[dict] = [
        {"role": "system", "content": "System instructions."},
        {"role": "user", "content": "Primary user goal: analyze repository."},
    ]
    # 18 rounds x ~8K tokens = ~144K tokens (well above the old 90K/110K cap,
    # still comfortably below the 255K high watermark).
    for step in range(18):
        start = len(messages)
        messages.extend(_tool_round(step, payload_chars=32_000))
        compacted = compact_intra_turn_tool_messages(
            messages,
            budget=budget,
            tool_schema_tokens=6_000,
            last_input_tokens=step * 8_000,
            new_messages_start=start,
        )
        assert compacted == 0

    assert all(
        not str(m.get("content", "")).startswith(_INTRA_TURN_COMPACTED_MARKER)
        for m in messages
        if m["role"] == "tool"
    )


def test_stateless_high_low_watermark_batch_compaction_and_headroom():
    budget = build_intra_turn_context_budget(
        context_window_tokens=32_768,
        response_headroom_tokens=4_096,
        provenance="local_32k",
    )
    assert budget.hard_input_limit_tokens == 28_672
    assert budget.high_watermark_tokens == int(28_672 * 0.85)
    assert budget.low_watermark_tokens == int(28_672 * 0.55)

    messages: list[dict] = [
        {"role": "system", "content": "System contract."},
        {"role": "user", "content": "Preserve this user objective verbatim."},
    ]
    compaction_events: list[int] = []
    # Each round adds ~2,500 tokens (~10,000 ASCII chars).
    for step in range(14):
        start = len(messages)
        messages.extend(_tool_round(step, payload_chars=10_000))
        compacted = compact_intra_turn_tool_messages(
            messages,
            budget=budget,
            tool_schema_tokens=2_000,
            new_messages_start=start,
        )
        if compacted:
            compaction_events.append((step, compacted))

    # Must batch-compact infrequently (leaving clean headroom), never on every step!
    assert 1 <= len(compaction_events) <= 2
    assert compaction_events[0][1] >= 3  # Batch folded multiple older results at once

    # Verify task state and exact 1:1 tool_call_id pairing preserved!
    assert messages[0]["content"] == "System contract."
    assert messages[1]["content"] == "Preserve this user objective verbatim."
    expected_ids = [
        call["id"]
        for m in messages
        if m["role"] == "assistant"
        for call in m.get("tool_calls", [])
    ]
    actual_ids = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
    assert actual_ids == expected_ids

    # Most recent tool group stays verbatim!
    assert not messages[-1]["content"].startswith(_INTRA_TURN_COMPACTED_MARKER)


def test_oversized_newly_appended_tool_result_compacted_against_remaining_budget():
    budget = build_intra_turn_context_budget(
        context_window_tokens=32_768,
        response_headroom_tokens=4_096,
        provenance="hli_32k",
    )
    messages: list[dict] = [
        {"role": "system", "content": "System contract."},
        {"role": "user", "content": "Read the huge log and summarize errors."},
    ]
    start = len(messages)
    # Single newly appended 120,000-char (~30K token) tool output exceeds entire 32K window!
    messages.extend(_tool_round(0, payload_chars=120_000))

    compacted = compact_intra_turn_tool_messages(
        messages,
        budget=budget,
        tool_schema_tokens=2_000,
        last_input_tokens=500,  # Previous API call was tiny!
        new_messages_start=start,
    )
    assert compacted == 1
    tool_content = messages[-1]["content"]
    assert tool_content.startswith(_INTRA_TURN_COMPACTED_MARKER)
    assert "tool=file_read" in tool_content
    assert "target=src/mod_0.py" in tool_content
    assert "exceeded remaining context budget" in tool_content
    total_after = 2_000 + sum(_estimate_message_tokens(m) for m in messages)
    assert total_after <= budget.hard_input_limit_tokens
    # Calling compaction again immediately is a clean no-op (no infinite loop).
    assert (
        compact_intra_turn_tool_messages(
            messages, budget=budget, tool_schema_tokens=2_000
        )
        == 0
    )


def test_unreachable_low_watermark_stops_cleanly_without_mutating_user_goal():
    budget = build_intra_turn_context_budget(
        context_window_tokens=8_192,
        usable_input_tokens=6_144,
        provenance="tiny_8k",
    )
    # Fixed system + user prefix alone is ~4,500 tokens (> low_watermark ~3,379).
    big_user = "Important user specification: " + ("u" * 16_000)
    messages: list[dict] = [
        {"role": "system", "content": "System contract."},
        {"role": "user", "content": big_user},
    ]
    messages.extend(_tool_round(0, payload_chars=8_000))
    compacted = compact_intra_turn_tool_messages(
        messages, budget=budget, tool_schema_tokens=500
    )
    assert compacted == 1
    assert messages[1]["content"] == big_user
    # Second call does nothing and terminates cleanly.
    assert (
        compact_intra_turn_tool_messages(
            messages, budget=budget, tool_schema_tokens=500
        )
        == 0
    )


def test_adapters_resolve_model_aware_budgets_from_existing_profiles(tmp_path: Path):
    agent_md = tmp_path / "agent.md"
    agent_md.write_text(render_pcm_document(persona="Concise.", system="Rules."), encoding="utf-8")
    gcfg = GlobalConfig(
        authorized_id=1,
        project_root=tmp_path,
        bridge_home=tmp_path,
        base_logs_dir=tmp_path / "logs",
    )

    # DeepSeek 1M
    ds = DeepSeekAdapter(
        AgentConfig(
            name="ds", engine="deepseek-api", workspace_dir=tmp_path,
            system_md=agent_md, model="deepseek-flash", is_active=True,
        ),
        gcfg,
        api_key="test-key",
    )
    ds_budget = ds._resolve_intra_turn_context_budget()
    assert ds_budget.context_window_tokens == 1_000_000
    assert ds_budget.working_input_target_tokens == 300_000
    assert ds_budget.high_watermark_tokens == 255_000

    # HLI local 32K (probed served_context_tokens)
    endpoint = "http://127.0.0.1:8080/v1/chat/completions"
    model = "qwen3.8-27b-local-test"
    entry_key = _hli_endpoint_key(endpoint, model)
    HLI_CAPACITY_PROFILES[entry_key] = {
        "context_window_tokens": 32_768,
        "response_headroom_tokens": 4_096,
        "capacity_provenance": "hashi_compute_hli_model_capabilities",
    }
    try:
        hli = HLIAdapter(
            AgentConfig(
                name="hli", engine="hli-api", workspace_dir=tmp_path,
                system_md=agent_md, model=model, is_active=True,
                extra={"hli_url": endpoint},
            ),
            gcfg,
            api_key=None,
        )
        hli_budget = hli._resolve_intra_turn_context_budget()
        assert hli_budget.context_window_tokens == 32_768
        assert hli_budget.hard_input_limit_tokens == 28_672
        assert hli_budget.high_watermark_tokens < 28_672
        assert hli_budget.low_watermark_tokens < hli_budget.high_watermark_tokens
    finally:
        HLI_CAPACITY_PROFILES.pop(entry_key, None)

    # Ollama 32K default
    ol = OllamaAdapter(
        AgentConfig(
            name="ol", engine="ollama-api", workspace_dir=tmp_path,
            system_md=agent_md, model="qwen3:27b", is_active=True,
        ),
        gcfg,
    )
    ol_budget = ol._resolve_intra_turn_context_budget()
    assert ol_budget.context_window_tokens == 32_768
    assert ol_budget.hard_input_limit_tokens == 24_576

    # Native history_messages injected cleanly before current user turn
    or_adapter = OpenRouterAdapter(
        AgentConfig(
            name="or", engine="openrouter-api", workspace_dir=tmp_path,
            system_md=agent_md, model="test/model", is_active=True,
        ),
        gcfg,
        api_key="test-key",
    )
    or_adapter.sys_prompt = "SYS"
    msgs, _routing = or_adapter._initial_messages(
        "Current Turn 3",
        None,
        history_messages=[
            {"role": "user", "content": "Turn 1"},
            {"role": "assistant", "content": "Reply 1"},
            {"role": "user", "content": "Turn 2"},
            {"role": "assistant", "content": "Reply 2"},
        ],
    )
    assert msgs == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "Turn 1"},
        {"role": "assistant", "content": "Reply 1"},
        {"role": "user", "content": "Turn 2"},
        {"role": "assistant", "content": "Reply 2"},
        {"role": "user", "content": "Current Turn 3"},
    ]


def test_bridge_memory_append_only_and_batch_watermark_compaction(tmp_path: Path, monkeypatch):
    agent_md = tmp_path / "agent.md"
    agent_md.write_text(
        render_pcm_document(persona="Concise.", system="Rules."),
        encoding="utf-8",
    )
    store = BridgeMemoryStore(tmp_path)
    assembler = BridgeContextAssembler(store, agent_md)

    exchanges = [
        {
            "sequence": i + 1,
            "user_ts": f"2026-11-26T10:{i:02d}:00+11:00",
            "assistant_ts": f"2026-11-26T10:{i:02d}:15+11:00",
            "user_text": f"USER_{i} " + ("u" * 600),
            "assistant_text": f"ASST_{i} " + ("a" * 600),
        }
        for i in range(12)
    ]

    # Within generous 64K budget, all 12 exchanges stay append-only (no 10-turn slide).
    p_full = assembler.build_prompt_payload(
        "Now", "openrouter-api", recent_exchanges=exchanges
    )
    assert not p_full["audit"]["history_omitted"]
    assert "USER_0 " in p_full["final_prompt"]
    assert "USER_11 " in p_full["final_prompt"]

    # When budget is exceeded, batch-compacts down to low watermark (~70% of limit),
    # leaving meaningful headroom rather than popping only 1 exchange.
    monkeypatch.setitem(assembler.PROMPT_TOKEN_BUDGETS, "openrouter-api", 2_400)
    p_batch = assembler.build_prompt_payload(
        "Now", "openrouter-api", recent_exchanges=exchanges
    )
    assert len(p_batch["audit"]["history_omitted"]) >= 4
    assert p_batch["audit"]["final_prompt_tokens_after_budget"] <= int(2_400 * 0.78)
    assert "USER_11 " in p_batch["final_prompt"]
