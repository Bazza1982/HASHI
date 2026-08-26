from __future__ import annotations

import base64
import json
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.modules.setdefault("edge_tts", types.ModuleType("edge_tts"))

from orchestrator.bridge_memory import BridgeMemoryStore
from orchestrator import runtime_workspace
from orchestrator.canonical_audit import (
    CanonicalAuditAccessError,
    CanonicalAuditConfigurationError,
    CanonicalAuditStore,
)
from orchestrator.central_memory import (
    MemorySearchAuthorizationError,
    MemorySearchService,
)
from orchestrator.commands.wiki import build_wiki_prompt, wiki_command
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.skill_manager import SkillManager
from tools.registry import ToolRegistry


RAW_AUTH = {
    "allow_raw_audit": True,
    "actor": "auditor",
    "purpose": "contract verification",
}


def test_canonical_audit_keeps_complete_chain_and_content_addressed_artifact(tmp_path):
    store = CanonicalAuditStore(
        tmp_path,
        instance_id="HASHI2",
        agent_id="rika",
        config={"artifact_threshold_bytes": 1024},
    )
    first = store.record(
        "provider_request",
        {"secret": "raw-secret", "large": "z" * 2048},
        request_id="req-1",
    )
    store.record(
        "tool_call",
        {"arguments": {"token": "unredacted"}, "result": b"binary-result"},
        request_id="req-1",
    )

    events = store.read_events(RAW_AUTH)
    assert events[0]["event_id"] == first
    assert events[0]["payload"]["secret"] == "raw-secret"
    artifact = events[0]["payload"]["large"]["$artifact"]
    artifact_path = store.root / artifact["relative_path"]
    assert artifact_path.read_bytes() == b"z" * 2048
    assert events[1]["previous_record_digest"]
    assert events[1]["payload"]["arguments"]["token"] == "unredacted"


def test_canonical_audit_encryption_hides_plaintext_and_preserves_reasoning_semantics(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "TEST_HASHI_AUDIT_KEY", base64.urlsafe_b64encode(b"k" * 32).decode("ascii")
    )
    store = CanonicalAuditStore(
        tmp_path,
        instance_id="HASHI2",
        agent_id="rika",
        config={
            "encryption_required": True,
            "encryption_key_env": "TEST_HASHI_AUDIT_KEY",
            "artifact_threshold_bytes": 1024,
        },
    )
    store.record(
        "provider_reasoning",
        {"availability": "available", "raw_delta": "private chain of thought"},
    )
    store.record(
        "provider_reasoning",
        {"availability": "unavailable", "fabricated": False},
    )
    store.record(
        "provider_response",
        {"large": "top-secret-artifact" * 200},
    )

    disk = store.events_path.read_bytes()
    assert b"private chain of thought" not in disk
    assert b"top-secret-artifact" not in disk
    events = store.read_events(RAW_AUTH)
    assert events[0]["payload"]["raw_delta"] == "private chain of thought"
    assert events[1]["payload"] == {
        "availability": "unavailable",
        "fabricated": False,
    }
    reference = events[2]["payload"]["large"]
    artifact_path = store.root / reference["$artifact"]["relative_path"]
    assert b"top-secret-artifact" not in artifact_path.read_bytes()
    assert store.read_artifact(reference, RAW_AUTH) == (
        "top-secret-artifact" * 200
    ).encode("utf-8")


def test_canonical_audit_survives_workspace_lifecycle_and_has_no_expiry(tmp_path):
    bridge_home = tmp_path / "bridge"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = CanonicalAuditStore(
        bridge_home, instance_id="HASHI2", agent_id="rika"
    )
    event_id = store.record("chat_exchange", {"user": "hello", "assistant": "hi"})
    for child in workspace.iterdir():
        child.unlink()
    reloaded = CanonicalAuditStore(
        bridge_home, instance_id="HASHI2", agent_id="rika"
    )
    assert reloaded.read_events(RAW_AUTH)[0]["event_id"] == event_id
    assert not hasattr(reloaded, "ttl") and not hasattr(reloaded, "prune")


def test_raw_audit_read_and_wipe_require_separate_explicit_authority(tmp_path):
    store = CanonicalAuditStore(
        tmp_path,
        instance_id="HASHI2",
        agent_id="rika",
        config={"artifact_threshold_bytes": 1024},
    )
    first_id = store.record("tool_call", {"secret": "keep-first"})
    event_id = store.record("tool_call", {"large": "delete-me" * 300})
    store.record("tool_call", {"secret": "keep-last"})
    reference = store.read_events(RAW_AUTH)[1]["payload"]["large"]
    artifact_path = store.root / reference["$artifact"]["relative_path"]
    assert artifact_path.exists()
    with pytest.raises(CanonicalAuditAccessError):
        store.read_events({"allow_raw_audit": True})
    with pytest.raises(CanonicalAuditAccessError):
        store.audit_wipe(
            authorization=RAW_AUTH,
            confirmation="CONFIRM",
            event_ids=[event_id],
        )

    deleted = store.audit_wipe(
        authorization=RAW_AUTH,
        confirmation="DELETE CANONICAL AUDIT HASHI2/rika",
        event_ids=[event_id],
    )
    events = store.read_events(RAW_AUTH)
    assert deleted == 1
    assert [event["event_type"] for event in events] == [
        "tool_call",
        "tool_call",
        "audit_wipe",
    ]
    assert events[0]["event_id"] == first_id
    wrappers = [
        json.loads(line)
        for line in store.events_path.read_text(encoding="utf-8").splitlines()
    ]
    assert events[1]["previous_record_digest"] == wrappers[0]["record_digest"]
    assert artifact_path.exists() is False


def test_canonical_audit_rejects_path_traversal_components_and_tampered_chain(tmp_path):
    store = CanonicalAuditStore(tmp_path, instance_id="..", agent_id="..")
    assert store.root.resolve().is_relative_to(tmp_path.resolve())
    store.record("tool_call", {"secret": "original"})
    [wrapper] = [
        json.loads(line)
        for line in store.events_path.read_text(encoding="utf-8").splitlines()
    ]
    wrapper["event"]["payload"]["secret"] = "tampered"
    store.events_path.write_text(
        json.dumps(wrapper, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(CanonicalAuditConfigurationError, match="digest mismatch"):
        store.read_events(RAW_AUTH)


def test_local_recency_decay_is_monotonic_and_components_are_observable(tmp_path):
    store = BridgeMemoryStore(tmp_path)
    recent_id = store.record_memory("fact", "test", "same searchable memory", 1.0)
    old_id = store.record_memory("fact", "test", "same searchable memory", 1.0)
    now = datetime(2026, 8, 26, tzinfo=timezone.utc)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE memories SET ts = ? WHERE id = ?",
            ((now - timedelta(days=1)).isoformat(), recent_id),
        )
        connection.execute(
            "UPDATE memories SET ts = ? WHERE id = ?",
            ((now - timedelta(days=90)).isoformat(), old_id),
        )
        connection.commit()

    results = {
        item["id"]: item
        for item in store.retrieve_memories("same searchable memory", limit=10, now=now)
    }
    assert results[recent_id]["recency_score"] > results[old_id]["recency_score"]
    assert results[recent_id]["score"] > results[old_id]["score"]
    for key in ("vector_score", "text_score", "importance_score", "recency_score"):
        assert key in results[recent_id]


def _central_db(path: Path):
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE consolidated(
                id INTEGER PRIMARY KEY, instance TEXT, agent_id TEXT,
                content TEXT, source_ts TEXT, source TEXT,
                memory_type TEXT, importance REAL, embedding BLOB
            )
            """
        )
        connection.executemany(
            "INSERT INTO consolidated VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "HASHI2", "rika", "Rika private decision", "2026-08-26", "sync", "decision", 1.0, None),
                (2, "HASHI2", "arale", "Arale private decision", "2026-08-26", "sync", "decision", 1.0, None),
            ],
        )
        connection.commit()


def test_central_memory_defaults_to_current_instance_and_agent(tmp_path):
    database = tmp_path / "central.sqlite"
    _central_db(database)
    global_config = SimpleNamespace(
        instance_id="HASHI2",
        central_memory={"database_path": str(database), "model_dir": str(tmp_path / "missing")},
    )
    service = MemorySearchService(
        workspace_dir=tmp_path / "workspace",
        global_config=global_config,
        agent_id="rika",
    )

    result = service.search({"query": "decision", "source": "central"})

    assert [item["agent_id"] for item in result["results"]] == ["rika"]
    assert result["results"][0]["provenance"]["scope"]["cross_agent"] is False


def test_cross_agent_memory_requires_exact_authorization_purpose_and_provenance(tmp_path):
    database = tmp_path / "central.sqlite"
    _central_db(database)
    service = MemorySearchService(
        workspace_dir=tmp_path / "workspace",
        global_config=SimpleNamespace(
            instance_id="HASHI2",
            central_memory={"database_path": str(database), "model_dir": str(tmp_path / "missing")},
        ),
        agent_id="rika",
    )
    with pytest.raises(MemorySearchAuthorizationError):
        service.search(
            {
                "query": "decision",
                "source": "central",
                "scope": "cross_agent",
                "agent_id": "arale",
                "authorization": "explicit_user_authorization",
            }
        )
    with pytest.raises(MemorySearchAuthorizationError):
        service.search(
            {
                "query": "decision",
                "source": "central",
                "scope": "cross_agent",
                "instance_id": "HASHI2",
                "agent_id": "arale",
                "purpose": "fabricated",
                "_trusted_authorization": {
                    "authorization": "explicit_user_authorization",
                    "instance_id": "HASHI2",
                    "agent_id": "arale",
                    "purpose": "fabricated",
                },
            }
        )

    authorized_service = MemorySearchService(
        workspace_dir=tmp_path / "workspace",
        global_config=service.global_config,
        agent_id="rika",
        trusted_authorization={
            "authorization": "explicit_user_authorization",
            "instance_id": "HASHI2",
            "agent_id": "arale",
            "purpose": "user-requested audit comparison",
        },
    )
    result = authorized_service.search(
        {
            "query": "decision",
            "source": "central",
            "scope": "cross_agent",
            "instance_id": "HASHI2",
            "agent_id": "arale",
            "purpose": "user-requested audit comparison",
        }
    )
    assert [item["agent_id"] for item in result["results"]] == ["arale"]
    assert result["results"][0]["provenance"]["scope"]["purpose"]


@pytest.mark.asyncio
async def test_memory_raw_command_binds_exact_cross_agent_authority_to_request():
    runtime = SimpleNamespace(
        _is_authorized_user=lambda _user_id: True,
        _get_available_tool_catalogue=lambda: [{"name": "memory_search"}],
        enqueue_request=AsyncMock(return_value="req-1"),
        _reply_text=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=9),
    )

    await runtime_workspace.cmd_memory(
        runtime,
        update,
        SimpleNamespace(args=["raw", "HASHI2", "arale", "find", "decision"]),
    )

    kwargs = runtime.enqueue_request.await_args.kwargs
    assert kwargs["source"] == "memory:raw-search"
    assert kwargs["request_metadata"]["memory_search_authorization"] == {
        "authorization": "explicit_user_authorization",
        "instance_id": "HASHI2",
        "agent_id": "arale",
        "purpose": "User invoked /memory raw for HASHI2/arale",
        "authorizing_user_id": 7,
    }


def test_memory_search_skill_visibility_tracks_real_tool_authority(tmp_path):
    manager = SkillManager(Path(__file__).resolve().parents[1], tmp_path / "tasks.json")
    backend = SimpleNamespace(tool_registry=None)
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.skill_manager = manager
    runtime.workspace_dir = tmp_path
    runtime.config = SimpleNamespace(active_backend="openrouter-api")
    runtime.backend_manager = SimpleNamespace(current_backend=backend)

    names = {item["name"] for item in runtime._get_available_skill_catalogue()}
    assert "memory-search" not in names

    backend.tool_registry = ToolRegistry(
        allowed_tools=["memory_search"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
    )
    names = {item["name"] for item in runtime._get_available_skill_catalogue()}
    assert "memory-search" in names


def test_wiki_prompt_is_generic_and_contains_no_private_deployment_data():
    prompt = build_wiki_prompt(
        query="What is the release decision?",
        provider_id="configured-provider",
        capability="knowledge_search",
    )
    assert "What is the release decision?" in prompt
    assert "knowledge_search" in prompt
    assert "Obsidian" not in prompt and "vault" not in prompt and "/home/" not in prompt


@pytest.mark.asyncio
async def test_wiki_core_command_fails_clearly_without_provider_or_capability():
    replies = []

    async def reply(_update, text, **_kwargs):
        replies.append(text)

    runtime = SimpleNamespace(
        global_config=SimpleNamespace(wiki_provider={}),
        _is_authorized_user=lambda _user_id: True,
        _reply_text=reply,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=2),
    )
    await wiki_command(runtime, update, SimpleNamespace(args=["question"]))
    assert "Wiki unavailable" in replies[-1]


@pytest.mark.asyncio
async def test_wiki_core_command_binds_request_to_configured_capability_only():
    runtime = SimpleNamespace(
        global_config=SimpleNamespace(
            wiki_provider={"id": "curated", "capability": "web_search"}
        ),
        _is_authorized_user=lambda _user_id: True,
        _get_available_tool_catalogue=lambda: [
            {"name": "web_search"},
            {"name": "file_read"},
        ],
        enqueue_request=AsyncMock(return_value="req-wiki"),
        _reply_text=AsyncMock(),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=2),
    )

    await wiki_command(runtime, update, SimpleNamespace(args=["release", "decision"]))

    kwargs = runtime.enqueue_request.await_args.kwargs
    assert kwargs["request_metadata"] == {
        "tool_allowlist": ["web_search"],
        "wiki_provider_id": "curated",
    }
    assert "file_read" not in kwargs["prompt"]


@pytest.mark.asyncio
async def test_tool_registry_request_scope_filters_catalogue_and_admission(tmp_path):
    registry = ToolRegistry(
        allowed_tools=["web_search", "file_read"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
        audit_context={"request_tool_allowlist": ["web_search"]},
    )

    assert {
        item["function"]["name"] for item in registry.get_tool_definitions()
    } == {"web_search"}
    denial = registry.evaluate_admission("file_read", {"path": "x"}, "call-1")
    assert denial is not None and denial.is_error is True
    assert "current request" in denial.output
