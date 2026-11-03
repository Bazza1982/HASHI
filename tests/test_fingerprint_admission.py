"""Real persistent PAO admission and restart/replay boundaries; no providers."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from orchestrator.config import GlobalConfig
from orchestrator.fingerprint.models import FingerprintPolicy
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.request_activity import RequestActivityStore
from orchestrator.session_store import SessionStore
from orchestrator import runtime_session


def runtime_at(root: Path, enabled: bool, *, instance_id="TEST", agent="agent"):
    workspace = root / "agent"
    workspace.mkdir(exist_ok=True)
    sequence = iter(range(1, 100))
    runtime = SimpleNamespace(
        name=agent, workspace_dir=workspace,
        global_config=GlobalConfig(7, instance_id=instance_id, bridge_home=root, project_root=root,
            fingerprint=FingerprintPolicy(enabled=enabled)),
        session_store=SessionStore(root / "sessions.sqlite3", instance_id=instance_id),
        config=SimpleNamespace(workspace_dir=workspace),
        backend_manager=SimpleNamespace(_read_state_dict=lambda: {}),
        queue=asyncio.Queue(), _execution_admissions={},
        error_logger=logging.getLogger("fingerprint.error"), message_logger=logging.getLogger("fingerprint.messages"),
        logger=logging.getLogger("fingerprint"), request_activity=RequestActivityStore(),
        next_request_id=lambda: f"test-request-{next(sequence)}",
    )
    runtime.enqueue_request = MethodType(FlexibleAgentRuntime.enqueue_request, runtime)
    return runtime


@pytest.mark.asyncio
async def test_actual_admission_ignores_supplied_policy_and_replay_keeps_original(tmp_path):
    runtime = runtime_at(tmp_path, True)
    request = await runtime.enqueue_request(7, "write a file", "api", "test",
        request_metadata={"fingerprint_policy": FingerprintPolicy().snapshot()}, idempotency_key="same-message")
    item = runtime.queue.get_nowait()
    original = runtime.session_store.get_run(item.run_id)
    assert original["fingerprint_policy"] == FingerprintPolicy(enabled=True).snapshot()
    assert item.request_metadata["fingerprint_policy"] == original["fingerprint_policy"]
    runtime.global_config.fingerprint = FingerprintPolicy()
    assert await runtime.enqueue_request(7, "write a file", "api", "test", idempotency_key="same-message") == request
    assert runtime.queue.empty()
    reopened = SessionStore(tmp_path / "sessions.sqlite3", instance_id="TEST")
    assert reopened.get_run(item.run_id)["fingerprint_policy"] == original["fingerprint_policy"]


def test_direct_session_admission_uses_trusted_config_not_metadata(tmp_path):
    runtime = runtime_at(tmp_path, False)
    session, accepted, *_ = runtime_session.accept_request(runtime,
        request_id="direct", chat_id=7, prompt="hello", source="api", request_content=None,
        request_metadata={"fingerprint_policy": FingerprintPolicy(enabled=True).snapshot()},
        idempotency_key="direct")
    assert runtime.session_store.get_run(accepted.run_id)["fingerprint_policy"]["enabled"] is False


def test_migration_and_same_message_recovery_retain_run_policy(tmp_path):
    runtime = runtime_at(tmp_path, True)
    store = runtime.session_store
    session = store.create_session(owner_id="owner", agent_id="agent")
    args = dict(session_id=session["session_id"], owner_id="owner", agent_id="agent",
        request_id="one", text="hello", source="api", idempotency_key="key")
    first = store.accept_run(**args, fingerprint_policy=FingerprintPolicy(enabled=True).snapshot())
    reopened = SessionStore(store.db_path, instance_id="TEST")
    replay = reopened.accept_run(**{**args, "request_id": "two"}, fingerprint_policy=FingerprintPolicy().snapshot())
    assert replay.replayed and replay.run_id == first.run_id
    assert reopened.get_run(first.run_id)["fingerprint_policy"]["enabled"] is True
    # Represent a real pre-feature schema; reopening must migrate without changing old Run policy.
    with reopened._connection() as connection:
        connection.execute("ALTER TABLE runs DROP COLUMN fingerprint_policy_json")
        connection.execute("PRAGMA user_version=30")
    migrated = SessionStore(store.db_path, instance_id="TEST")
    assert FingerprintPolicy.from_snapshot(migrated.get_run(first.run_id)["fingerprint_policy"]).enabled is False
