from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.request_activity import RequestActivityStore
from orchestrator.session_store import SessionStore
from orchestrator.workbench_api import WorkbenchApiServer


def test_request_activity_tracks_lifecycle_and_stream_events() -> None:
    store = RequestActivityStore(max_requests=8, max_events_per_request=32)
    store.start("req-0001", source="api", created_at=10.0)
    store.mark_running("req-0001", timestamp=11.0)
    store.publish_stream(
        "req-0001",
        SimpleNamespace(
            kind="thinking",
            summary="Checking the project",
            detail="Looking at requirements",
            tool_name="",
            file_path="C:/Projects/frontend-client/readme.md",
            current=12,
            total=20,
            unit="pages",
            event_id="req-0001:reasoning:1",
            delivery_class="reasoning",
            origin="provider",
            phase="execution",
            revision=2,
            required=False,
            provenance="provider_returned",
            timestamp=12.0,
        ),
    )
    store.complete("req-0001", success=True, timestamp=13.0)

    result = store.poll("req-0001")

    assert result["ok"] is True
    assert result["state"] == "completed"
    assert result["terminal"] is True
    assert [event["kind"] for event in result["events"]] == [
        "queued",
        "started",
        "thinking",
        "completed",
    ]
    assert result["events"][2]["file_path"].endswith("readme.md")
    assert result["events"][2]["current"] == 12.0
    assert result["events"][2]["total"] == 20.0
    assert result["events"][2]["unit"] == "pages"
    assert result["events"][2]["event_id"] == "req-0001:reasoning:1"
    assert result["events"][2]["delivery_class"] == "reasoning"
    assert result["events"][2]["origin"] == "provider"
    assert result["events"][2]["phase"] == "execution"
    assert result["events"][2]["revision"] == 2.0
    assert result["events"][2]["required"] is False
    assert result["events"][2]["provenance"] == "provider_returned"


def test_request_activity_poll_uses_sequence_cursor() -> None:
    store = RequestActivityStore(max_requests=8, max_events_per_request=32)
    store.start("req-0002")
    store.mark_running("req-0002")

    result = store.poll("req-0002", after_sequence=1)

    assert [event["sequence"] for event in result["events"]] == [2]
    assert result["latest_sequence"] == 2


def test_request_activity_clamps_regressing_timestamps_to_sequence_order() -> None:
    store = RequestActivityStore(max_requests=8, max_events_per_request=32)
    store.start("req-clock-step", source="api", created_at=10.0)
    store.mark_running("req-clock-step", timestamp=11.0)
    store.publish_stream(
        "req-clock-step",
        SimpleNamespace(kind="progress", summary="first", timestamp=12.0),
    )
    store.publish_stream(
        "req-clock-step",
        SimpleNamespace(kind="progress", summary="second", timestamp=11.0),
    )
    store.complete("req-clock-step", success=True, timestamp=9.0)

    result = store.poll("req-clock-step")

    assert [event["timestamp"] for event in result["events"]] == [
        10.0,
        11.0,
        12.0,
        12.0,
        12.0,
    ]
    assert result["started_at"] == 11.0
    assert result["completed_at"] == 12.0


def test_request_activity_redacts_credentials_but_preserves_verbose_detail() -> None:
    store = RequestActivityStore(max_requests=8, max_events_per_request=32)
    store.start("req-0003")
    store.publish_stream(
        "req-0003",
        SimpleNamespace(
            kind="tool_start",
            summary="Calling service with token=super-secret-value",
            detail="Bearer abcdefghijklmnopqrstuvwxyz; reading full project notes",
            tool_name="Search",
            file_path="C:/Users/Test/Documents/Project Notes.md",
            timestamp=20.0,
        ),
    )

    event = store.poll("req-0003")["events"][-1]

    assert "super-secret-value" not in event["summary"]
    assert "abcdefghijklmnopqrstuvwxyz" not in event["detail"]
    assert "Project Notes.md" in event["file_path"]


def test_request_activity_is_bounded_and_missing_request_is_explicit() -> None:
    store = RequestActivityStore(max_requests=8, max_events_per_request=32)
    for number in range(10):
        request_id = f"req-{number:04d}"
        store.start(request_id)
        store.complete(request_id, success=True)

    assert store.poll("req-0000")["error_code"] == "request_activity_not_found"
    assert store.poll("req-0009")["ok"] is True


def test_request_activity_covers_display_kinds_and_rejects_duplicate_unknown_and_late_events() -> None:
    store = RequestActivityStore()
    store.start("req-owned")
    for kind, event_id in (
        ("commentary", "commentary-1"),
        ("progress", "progress-1"),
        ("tool_start", "tool-1"),
        ("tool_end", "tool-2"),
    ):
        event = SimpleNamespace(kind=kind, event_id=event_id, summary=kind)
        store.publish_stream("req-owned", event)
        store.publish_stream("req-owned", event)
    store.publish_stream(
        "req-unknown",
        SimpleNamespace(kind="progress", event_id="unknown-1", summary="unknown"),
    )
    store.complete("req-owned", success=True)
    store.publish_stream(
        "req-owned",
        SimpleNamespace(kind="progress", event_id="late-1", summary="late"),
    )

    result = store.poll("req-owned")
    assert [event["kind"] for event in result["events"]] == [
        "queued",
        "commentary",
        "progress",
        "tool_start",
        "tool_end",
        "completed",
    ]
    assert store.poll("req-unknown")["error_code"] == "request_activity_not_found"


@pytest.mark.asyncio
async def test_workbench_request_activity_handler_returns_cursor_stream(tmp_path: Path) -> None:
    store = RequestActivityStore()
    store.start("req-0042")
    store.mark_running("req-0042")
    runtime = SimpleNamespace(request_activity=store)
    server = WorkbenchApiServer.__new__(WorkbenchApiServer)
    server._runtime_map = lambda: {"akane": runtime}
    server.global_config = SimpleNamespace(
        instance_id="HASHI1", authorized_id=7, deployment_profile="personal"
    )
    server.session_store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI1")
    session = server.session_store.ensure_default_session(owner_id="user:7", agent_id="akane")
    accepted = server.session_store.accept_run(
        session_id=session["session_id"],
        owner_id="user:7",
        agent_id="akane",
        request_id="req-0042",
        text="hello",
        source="api",
        idempotency_key="activity-live",
    )
    request = SimpleNamespace(
        match_info={"name": "akane", "request_id": "req-0042"},
        query={"after_sequence": "1", "limit": "20"},
    )

    response = await server.handle_request_activity(request)
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["request_id"] == "req-0042"
    assert payload["session_id"] == session["session_id"]
    assert payload["run_id"] == accepted.run_id
    assert [event["kind"] for event in payload["events"]] == ["started"]


@pytest.mark.asyncio
async def test_workbench_request_activity_recovers_terminal_run_after_store_loss(tmp_path: Path) -> None:
    server = WorkbenchApiServer.__new__(WorkbenchApiServer)
    server.global_config = SimpleNamespace(
        instance_id="HASHI1", authorized_id=7, deployment_profile="personal"
    )
    server.session_store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI1")
    session = server.session_store.ensure_default_session(owner_id="user:7", agent_id="akane")
    accepted = server.session_store.accept_run(
        session_id=session["session_id"],
        owner_id="user:7",
        agent_id="akane",
        request_id="req-recovered",
        text="hello",
        source="api",
        idempotency_key="activity-recovered",
    )
    server.session_store.finish_request(
        "req-recovered", success=True, assistant_text="done", assistant_source="test"
    )
    server._runtime_map = lambda: {
        "akane": SimpleNamespace(request_activity=RequestActivityStore())
    }
    request = SimpleNamespace(
        match_info={"name": "akane", "request_id": "req-recovered"},
        query={"after_sequence": "9", "limit": "20"},
    )

    response = await server.handle_request_activity(request)
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["terminal"] is True
    assert payload["success"] is True
    assert payload["recovered_from"] == "session_store"
    assert payload["session_id"] == session["session_id"]
    assert payload["run_id"] == accepted.run_id
    assert payload["latest_sequence"] == 9


@pytest.mark.asyncio
async def test_workbench_request_activity_hides_cross_owner_and_cross_agent_runs(tmp_path: Path) -> None:
    server = WorkbenchApiServer.__new__(WorkbenchApiServer)
    server.global_config = SimpleNamespace(
        instance_id="HASHI1", authorized_id=7, deployment_profile="personal"
    )
    server.session_store = SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI1")
    session = server.session_store.ensure_default_session(owner_id="user:8", agent_id="akane")
    server.session_store.accept_run(
        session_id=session["session_id"],
        owner_id="user:8",
        agent_id="akane",
        request_id="req-private",
        text="private",
        source="api",
        idempotency_key="activity-private",
    )
    server._runtime_map = lambda: {
        "akane": SimpleNamespace(request_activity=RequestActivityStore()),
        "lily": SimpleNamespace(request_activity=RequestActivityStore()),
    }

    for agent in ("akane", "lily"):
        response = await server.handle_request_activity(
            SimpleNamespace(
                match_info={"name": agent, "request_id": "req-private"},
                query={},
            )
        )
        assert response.status == 404
        assert json.loads(response.text)["error_code"] == "request_activity_not_found"
