from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from orchestrator.bridge_memory import (
    BgeM3Encoder,
    BridgeContextAssembler,
    BridgeMemoryStore,
    LocalEmbeddingEncoder,
)
from orchestrator.memory_search_mode import (
    apply_memory_injection_preferences,
    get_memory_sync_preference,
    is_memory_search_enabled,
    set_memory_search_enabled,
)
from orchestrator.runtime_workspace import cmd_memory


def test_t01_fresh_workspace_injects_relevant_saved_memory_by_default_and_respects_explicit_off(tmp_path):
    workspace = tmp_path / "fresh_agent"
    workspace.mkdir()
    store = BridgeMemoryStore(workspace)
    store.record_memory("preference", "chat", "User's favorite color is chartreuse.", importance=1.0)

    # New workspace defaults memory search to ON without any manual /memory search on.
    assert is_memory_search_enabled(workspace) is True
    assembler = BridgeContextAssembler(store, system_md=None)
    apply_memory_injection_preferences(assembler, workspace)
    assert assembler.saved_memory_injection_enabled is True

    prompt = assembler.build_prompt("What is my favorite color?", "deepseek-api")
    assert "OPTIONAL SEARCHED LONG-TERM MEMORY" in prompt
    assert "chartreuse" in prompt

    # Explicit opt-out persists and suppresses retrieval.
    set_memory_search_enabled(workspace, False)
    assembler_off = BridgeContextAssembler(store, system_md=None)
    apply_memory_injection_preferences(assembler_off, workspace)
    assert assembler_off.saved_memory_injection_enabled is False
    prompt_off = assembler_off.build_prompt("What is my favorite color?", "deepseek-api")
    assert "OPTIONAL SEARCHED LONG-TERM MEMORY" not in prompt_off


def test_t02_multilingual_chinese_japanese_mixed_vectors_are_nonzero_and_discriminative(tmp_path):
    enc = LocalEmbeddingEncoder()
    zh_vec = enc.encode("我最喜欢的颜色是黄绿色，平时喜欢喝乌龙茶。")
    zh_query = enc.encode("我喜欢的颜色是什么")
    zh_unrelated = enc.encode("明天下午两点在会议室讨论财务报表")

    assert any(v != 0.0 for v in zh_vec)
    assert any(v != 0.0 for v in zh_query)
    assert enc.cosine(zh_vec, zh_query) > enc.cosine(zh_vec, zh_unrelated)

    ja_vec = enc.encode("好きな色は緑色で、週末はハイキングに行きます。")
    ja_query = enc.encode("好きな色")
    assert any(v != 0.0 for v in ja_vec)
    assert enc.cosine(ja_vec, ja_query) > enc.cosine(ja_vec, zh_unrelated)

    mixed_vec = enc.encode("Python 部署流程和中文配置说明")
    mixed_query = enc.encode("中文配置流程")
    assert enc.cosine(mixed_vec, mixed_query) > 0.0

    # End-to-end store recall for pure Chinese, mixed CJK+English, and Japanese.
    store = BridgeMemoryStore(tmp_path)
    store.record_memory("fact", "chat", "用户的电脑密码提示和北京出差酒店预订记录。")
    store.record_memory("fact", "chat", "我最喜欢的颜色是黄绿色，饮料只喝冰美式。")
    store.record_memory("fact", "chat", "好きな色は緑です。HASHI4 の設定ファイル。")

    zh_hits = store.retrieve_memories("喜欢什么颜色", limit=3)
    assert zh_hits
    assert "黄绿色" in zh_hits[0]["content"]
    assert zh_hits[0]["vector_score"] > 0.0

    ja_hits = store.retrieve_memories("好きな色", limit=3)
    assert ja_hits
    assert "緑です" in ja_hits[0]["content"]


def test_t03_unrelated_memories_return_empty_instead_of_recency_backfill(tmp_path):
    store = BridgeMemoryStore(tmp_path)
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    for idx in range(10):
        store.record_memory(
            "fact",
            "chat",
            f"无关记录编号{idx}：今天去超市买了苹果香蕉牛奶面包。",
            importance=1.0,
        )

    assert store.retrieve_memories("量子力学薛定谔方程推导", limit=6, now=now) == []
    assert store.retrieve_memories("quantum mechanics hamiltonian", limit=6, now=now) == []


@pytest.mark.asyncio
async def test_t26_memory_sync_command_updates_store_and_surfaces_degraded_or_active_status(tmp_path, monkeypatch):
    workspace = tmp_path / "sync_agent"
    workspace.mkdir()
    store = BridgeMemoryStore(workspace)
    store.record_memory("fact", "chat", "关键信息：项目代号凤凰计划将于周五发布。")

    replies: list[str] = []
    skill_state: dict[str, bool] = {}

    async def reply(_update, text, **_kwargs):
        replies.append(text)

    runtime = SimpleNamespace(
        workspace_dir=workspace,
        memory_store=store,
        context_assembler=BridgeContextAssembler(store, system_md=None),
        _is_authorized_user=lambda _uid: True,
        _reply_text=reply,
        _get_skill_state=lambda: dict(skill_state),
        _set_skill_state=lambda k, v: skill_state.__setitem__(k, v),
    )
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))

    # Point BGE to a missing directory so sync on must fall back gracefully AND report degraded status.
    monkeypatch.setattr(BgeM3Encoder, "DEFAULT_MODEL_DIR", tmp_path / "missing-bge-model")
    await cmd_memory(runtime, update, SimpleNamespace(args=["sync", "on"]))
    assert get_memory_sync_preference(workspace) is True
    assert skill_state["memory_sync"] is True
    status = store.get_vector_status()
    assert status["overall_status"] == "degraded"
    assert status["degraded"] is True
    assert "degraded" in replies[-1].lower()
    # Even when BGE is degraded, pure Chinese FTS/multilingual recall still works.
    assert any("凤凰计划" in row["content"] for row in store.retrieve_memories("凤凰计划发布"))

    # Now simulate an upgraded 1024-d BGE encoder via /memory sync on and verify vec dim migration & backfill.
    class FakeReadyBge:
        DIM = 1024
        ready = True
        vector_dim = 1024
        encoder_id = "fake-bge-v1:1024"
        error = None

        @staticmethod
        def encode(text: str) -> list[float]:
            vec = [0.0] * 1024
            vec[0] = 1.0 if "凤凰" in text or "发布" in text else 0.0
            vec[1] = 1.0 if vec[0] == 0.0 else 0.0
            return vec

        @staticmethod
        def cosine(a: list[float], b: list[float]) -> float:
            return sum(x * y for x, y in zip(a, b))

    monkeypatch.setattr(BgeM3Encoder, "cached", classmethod(lambda cls, **_kw: FakeReadyBge()))
    await cmd_memory(runtime, update, SimpleNamespace(args=["sync", "on"]))
    upgraded = store.get_vector_status()
    assert upgraded["encoder_ready"] is True
    assert upgraded["encoder_dim"] == 1024
    assert upgraded["counts"]["memories"] == 1
    if upgraded["sqlite_vec_supported"]:
        assert upgraded["vec_dim"] == 1024
        assert upgraded["overall_status"] == "fully_upgraded"

    # Turning sync off restores local 256-dim multilingual encoder cleanly.
    await cmd_memory(runtime, update, SimpleNamespace(args=["sync", "off"]))
    assert get_memory_sync_preference(workspace) is False
    off_status = store.get_vector_status()
    assert off_status["encoder_dim"] == 256
