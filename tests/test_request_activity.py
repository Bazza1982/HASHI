from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from orchestrator.request_activity import RequestActivityStore
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
            file_path="C:/Projects/Aptenra/readme.md",
            current=12,
            total=20,
            unit="pages",
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


def test_request_activity_poll_uses_sequence_cursor() -> None:
    store = RequestActivityStore(max_requests=8, max_events_per_request=32)
    store.start("req-0002")
    store.mark_running("req-0002")

    result = store.poll("req-0002", after_sequence=1)

    assert [event["sequence"] for event in result["events"]] == [2]
    assert result["latest_sequence"] == 2


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


@pytest.mark.asyncio
async def test_workbench_request_activity_handler_returns_cursor_stream() -> None:
    store = RequestActivityStore()
    store.start("req-0042")
    store.mark_running("req-0042")
    runtime = SimpleNamespace(request_activity=store)
    server = WorkbenchApiServer.__new__(WorkbenchApiServer)
    server._runtime_map = lambda: {"akane": runtime}
    request = SimpleNamespace(
        match_info={"name": "akane", "request_id": "req-0042"},
        query={"after_sequence": "1", "limit": "20"},
    )

    response = await server.handle_request_activity(request)
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["request_id"] == "req-0042"
    assert [event["kind"] for event in payload["events"]] == ["started"]
