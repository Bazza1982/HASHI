from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.session_store import SessionStore
from tools.registry import TOOL_TIERS, ToolRegistry
from tools.schemas import TOOL_SCHEMA_MAP


class _ToolLogger:
    def info(self, *_args, **_kwargs):
        return None

    def warning(self, *_args, **_kwargs):
        return None


class _ToolBot:
    def __init__(self):
        self.messages = []
        self.media = []
        self._next_id = 100

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    async def send_document(self, **kwargs):
        handle = kwargs.pop("document")
        self.media.append({"payload": handle.read(), **kwargs})
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)


class _ToolRuntime:
    def __init__(self, tmp_path, store):
        self.app = SimpleNamespace(bot=_ToolBot())
        self.config = SimpleNamespace(active_backend="codex-cli", extra={})
        self.global_config = SimpleNamespace(
            authorized_id=7,
            instance_id="HASHI3",
            project_root=tmp_path,
        )
        self.logger = _ToolLogger()
        self.telegram_logger = _ToolLogger()
        self.error_logger = _ToolLogger()
        self.name = "agent1"
        self.session_dir = tmp_path
        self.session_store = store
        self.telegram_connected = True
        self.workspace_dir = tmp_path
        self._notify_enabled = False

    async def send_long_message(self, chat_id, text, **kwargs):
        from orchestrator.runtime_delivery import send_long_message

        return await send_long_message(
            self,
            chat_id=chat_id,
            text=text,
            **kwargs,
        )


def _running_session(tmp_path, *, delivery_route=None):
    store = SessionStore(
        tmp_path / "state" / "sessions.sqlite3",
        instance_id="HASHI3",
    )
    owner = "user:7"
    session = store.ensure_default_session(owner_id=owner, agent_id="agent1")
    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="agent1",
        request_id="request-frontend-output",
        text="send the generated files",
        source="session-api",
        idempotency_key="frontend-output-run",
        delivery_route=delivery_route,
    )
    assert store.mark_request_running(
        accepted.request_id, worker_id="HASHI3:agent1"
    ) == 1
    return store, owner, session, accepted


def _registry(
    tmp_path,
    store,
    owner,
    session,
    *,
    surface="generic-desktop",
    request_source="session-api",
    access_root=None,
    workspace_dir=None,
    allowed_tools=None,
    runtime=None,
):
    run = store.get_run_by_request("request-frontend-output", owner_id=owner)
    return ToolRegistry(
        allowed_tools=allowed_tools or ["frontend_send_attachments"],
        access_root=access_root or tmp_path,
        workspace_dir=workspace_dir or tmp_path,
        secrets={},
        audit_context={
            "agent_name": "agent1",
            "request_id": "request-frontend-output",
            "request_source": request_source,
            "hashi_session_id": session["session_id"],
            "hashi_run_id": run["run_id"],
            "hashi_fencing_token": run["fencing_token"],
            "owner_id": owner,
            "session_surface": surface,
            "session_store_descriptor": {
                "db_path": str(store.db_path),
                "instance_id": store.instance_id,
                "attachment_root": str(store.attachment_files_root),
            },
            "_runtime": runtime,
        },
    )


@pytest.mark.asyncio
async def test_hchat_attachment_selection_uses_hchat_limits_and_streaming_file_copy(
    tmp_path, monkeypatch
):
    from orchestrator import session_store as session_store_module

    store, owner, session, _accepted = _running_session(tmp_path)
    target = tmp_path / "bundle.enc"
    target.write_bytes(b"0123456789ab")
    monkeypatch.setattr(session_store_module, "MAX_SESSION_ATTACHMENT_BYTES", 8)
    monkeypatch.setattr(session_store_module, "MAX_SESSION_ATTACHMENT_TOTAL_BYTES", 8)
    monkeypatch.setattr(session_store_module, "HCHAT_SESSION_ATTACHMENT_BYTES", 32)
    monkeypatch.setattr(session_store_module, "HCHAT_SESSION_ATTACHMENT_TOTAL_BYTES", 32)
    monkeypatch.setattr(
        SessionStore,
        "upload_attachment_bytes",
        lambda *args, **kwargs: pytest.fail("HChat attachment was buffered in memory"),
    )

    standard = _registry(tmp_path, store, owner, session)
    rejected = await standard.execute(
        "frontend_send_attachments",
        {"attachments": [{"path": str(target)}]},
        tool_call_id="standard-limit",
    )
    assert rejected.is_error is True

    hchat = _registry(
        tmp_path,
        store,
        owner,
        session,
        request_source="bridge:hchat-draft",
    )
    selected = await hchat.execute(
        "frontend_send_attachments",
        {"attachments": [{"path": str(target)}]},
        tool_call_id="hchat-stream-selection",
    )

    assert selected.is_error is False, selected.output
    published = json.loads(selected.output)
    assert published["attachment_count"] == 1
    [part] = store.run_output_attachment_content(
        "request-frontend-output", owner_id=owner, agent_id="agent1"
    )
    assert part["filename"] == "bundle.enc"
    assert part["local_ref"]


@pytest.mark.platform
@pytest.mark.skipif(os.name == "nt", reason="WSL attachment paths require a POSIX filesystem")
def test_wsl_drive_scope_translates_windows_attachment_path_without_widening_default_scope(
    tmp_path, monkeypatch
):
    from tools import builtins

    monkeypatch.setattr(builtins, "is_wsl", lambda: True)
    windows_path = r"C:\Users\Example\Downloads\review.xlsx"

    with pytest.raises(ValueError, match="outside the allowed access scopes"):
        builtins._resolve_path(
            "/mnt/c/Users/Example/Downloads/review.xlsx",
            tmp_path,
            tmp_path,
        )

    resolved = builtins._resolve_path(
        windows_path,
        tmp_path,
        tmp_path,
        allow_wsl_windows_drive_paths=True,
    )

    assert resolved == Path("/mnt/c/Users/Example/Downloads/review.xlsx")


def test_native_windows_drive_optin_does_not_widen_attachment_scope(tmp_path, monkeypatch):
    from tools import builtins

    monkeypatch.setattr(builtins, "is_wsl", lambda: False)
    outside = tmp_path.parent / "outside-attachment.txt"
    with pytest.raises(ValueError, match="outside the allowed access scopes"):
        builtins._resolve_path(str(outside), tmp_path, tmp_path,
                               allow_wsl_windows_drive_paths=True)


@pytest.mark.asyncio
async def test_hchat_failed_attachment_selection_is_terminal_and_observable(tmp_path):
    store, owner, session, _accepted = _running_session(tmp_path)
    registry = _registry(
        tmp_path,
        store,
        owner,
        session,
        request_source="bridge:hchat-draft",
    )

    first = await registry.execute(
        "frontend_send_attachments",
        {"attachments": [{"path": str(tmp_path / "missing.xlsx")}]},
        tool_call_id="hchat-selection-failure",
    )
    second = await registry.execute(
        "frontend_send_attachments",
        {"attachments": [{"path": str(tmp_path / "different.xlsx")}]},
        tool_call_id="hchat-selection-retry",
    )
    selection = registry.consume_hchat_attachment_selection(
        "request-frontend-output"
    )

    assert first.is_error is True
    assert second.is_error is True
    assert "already attempted" in second.output
    assert selection is not None
    assert selection["success"] is False
    assert "file not found" in selection["error"]

    selected_file = tmp_path / "selected.xlsx"
    selected_file.write_bytes(b"selected")
    successful_registry = _registry(
        tmp_path,
        store,
        owner,
        session,
        request_source="bridge:hchat-draft",
    )
    selected = await successful_registry.execute(
        "frontend_send_attachments",
        {"attachments": [{"path": str(selected_file)}]},
        tool_call_id="hchat-selection-success",
    )
    repeated = await successful_registry.execute(
        "frontend_send_attachments",
        {"attachments": [{"path": str(selected_file)}]},
        tool_call_id="hchat-selection-after-success",
    )
    repeated_outcome = successful_registry.consume_hchat_attachment_selection(
        "request-frontend-output"
    )

    assert selected.is_error is False
    assert repeated.is_error is True
    assert repeated_outcome is not None
    assert repeated_outcome["success"] is False
    assert "already attempted" in repeated_outcome["error"]


def test_frontend_attachment_tool_is_standard_multi_attachment_contract():
    function = TOOL_SCHEMA_MAP["frontend_send_attachments"]["function"]
    parameters = function["parameters"]

    assert "current Session's final assistant reply" in function["description"]
    assert parameters["required"] == ["attachments"]
    assert parameters["properties"]["attachments"]["maxItems"] == 16
    assert "frontend_send_attachments" in TOOL_TIERS["communication"]
    incremental = TOOL_SCHEMA_MAP["frontend_publish_deliverable"]["function"]
    assert incremental["parameters"]["required"] == ["publication_id", "attachments"]
    assert "frontend_publish_deliverable" in TOOL_TIERS["communication"]


def test_telegram_file_cli_requires_explicit_runtime_identity_not_folder_name(
    tmp_path, monkeypatch
):
    from tools import telegram_send_file_cli

    workspace = tmp_path / "workspaces" / "looks-like-an-agent"
    workspace.mkdir(parents=True)
    monkeypatch.chdir(workspace)
    monkeypatch.delenv("HASHI_AGENT_NAME", raising=False)
    monkeypatch.delenv("AGENT_NAME", raising=False)

    assert telegram_send_file_cli._detect_current_agent() is None
    assert telegram_send_file_cli._detect_current_agent("Lily") == "lily"


@pytest.mark.asyncio
async def test_agent_publishes_ordered_multi_attachment_as_one_assistant_message(
    tmp_path,
):
    store, owner, session, accepted = _running_session(tmp_path)
    first = tmp_path / "first.png"
    second = tmp_path / "notes.txt"
    first.write_bytes(b"\x89PNG\r\n\x1a\nfrontend-output")
    second.write_text("ordered document", encoding="utf-8")
    registry = _registry(tmp_path, store, owner, session)
    arguments = {
        "attachments": [
            {"path": str(first), "caption": "first image"},
            {"path": str(second), "caption": "second document"},
        ]
    }

    result = await registry.execute(
        "frontend_send_attachments", arguments, tool_call_id="publish-call-1"
    )

    assert result.is_error is False, result.output
    published = json.loads(result.output)
    assert published["ok"] is True
    assert published["attachment_count"] == 2
    assert published["replayed"] is False
    assert published["media_group"]["retention_class"] == "message_bound"
    assert published["media_group"]["group_id"] == published["group_id"]
    assert [item["ordinal"] for item in published["media_group"]["attachments"]] == [0, 1]
    assert [part["filename"] for part in published["attachments"]] == [
        "first.png",
        "notes.txt",
    ]

    replay = await registry.execute(
        "frontend_send_attachments", arguments, tool_call_id="publish-call-1"
    )
    assert replay.is_error is False
    assert json.loads(replay.output)["replayed"] is True
    assert json.loads(replay.output)["group_id"] == published["group_id"]
    assert [
        part["attachment_id"] for part in json.loads(replay.output)["attachments"]
    ] == [part["attachment_id"] for part in published["attachments"]]

    finished = store.finish_request(
        accepted.request_id,
        success=True,
        assistant_text="Here are both files.",
        assistant_source="test-backend",
    )
    assert finished["state"] == "completed"
    messages = store.messages(session["session_id"], owner_id=owner)
    assistant = messages[-1]
    assert assistant["role"] == "assistant"
    assert [part["type"] for part in assistant["content"]] == [
        "text",
        "media",
        "media",
    ]
    assert [part.get("filename") for part in assistant["content"][1:]] == [
        "first.png",
        "notes.txt",
    ]
    assert [part.get("caption") for part in assistant["content"][1:]] == [
        "first image",
        "second document",
    ]
    from orchestrator.frontend_projection import poll_frontend_feed

    feed = poll_frontend_feed(
        store,
        session["session_id"],
        owner_id=owner,
        run_id=accepted.run_id,
    )
    output_event = next(
        event
        for event in feed["durable_events"]
        if event["semantic_kind"] == "status"
        and any(
            block["type"] == "media_ref"
            for block in event["content_blocks"]
        )
    )
    media_refs = [
        block
        for block in output_event["content_blocks"]
        if block["type"] == "media_ref"
    ]
    assert [block["attachment_id"] for block in media_refs] == [
        part["attachment_id"] for part in published["attachments"]
    ]
    assert [block["caption"] for block in media_refs] == [
        "first image",
        "second document",
    ]
    durable = store.events(
        session["session_id"], owner_id=owner
    )
    available = next(
        event
        for event in durable
        if event["kind"] == "assistant.output.available"
    )
    terminal = next(
        event for event in durable if event["kind"] == "run.completed"
    )
    assert store.claim_delivery_outbox(
        session_id=session["session_id"],
        owner_id=owner,
        worker_id="no-duplicate-media-worker",
        event_id=available["event_id"],
        limit=1,
    ) == []
    assert len(
        store.claim_delivery_outbox(
            session_id=session["session_id"],
            owner_id=owner,
            worker_id="terminal-media-worker",
            event_id=terminal["event_id"],
            limit=1,
        )
    ) == 1


@pytest.mark.asyncio
async def test_incremental_deliverables_are_visible_before_final_and_not_repeated(tmp_path):
    from orchestrator.chat_transcript_projection import build_chat_projection
    from orchestrator.frontend_projection import poll_frontend_feed

    store, owner, session, accepted = _running_session(tmp_path)
    first = tmp_path / "part-a.txt"
    second = tmp_path / "part-b.txt"
    first.write_text("ready A", encoding="utf-8")
    second.write_text("ready B", encoding="utf-8")
    registry = _registry(
        tmp_path, store, owner, session,
        allowed_tools=["frontend_send_attachments", "frontend_publish_deliverable"],
    )
    first_args = {
        "publication_id": "deliverable-a",
        "attachments": [{"path": str(first), "caption": "Part A"}],
    }
    published_a = await registry.execute(
        "frontend_publish_deliverable", first_args, tool_call_id="call-a-1"
    )
    assert published_a.is_error is False, published_a.output
    first_result = json.loads(published_a.output)
    assert first_result["persisted"] is True
    assert first_result["replayed"] is False
    assert first_result["message_id"] and first_result["event_id"]
    assert store.get_run_by_request(accepted.request_id, owner_id=owner)["state"] == "running"
    assert store.get_message(
        first_result["message_id"], session_id=session["session_id"], owner_id=owner
    )["content"][0]["filename"] == "part-a.txt"
    claims = store.claim_delivery_outbox(
        session_id=session["session_id"], owner_id=owner,
        worker_id="incremental-test", event_id=first_result["event_id"],
    )
    assert len(claims) == 1

    replay = await registry.execute(
        "frontend_publish_deliverable", first_args, tool_call_id="call-a-2"
    )
    assert json.loads(replay.output)["replayed"] is True
    assert json.loads(replay.output)["message_id"] == first_result["message_id"]
    changed = await registry.execute(
        "frontend_publish_deliverable",
        {**first_args, "text": "different"},
        tool_call_id="call-a-3",
    )
    assert changed.is_error is True

    published_b = await registry.execute(
        "frontend_publish_deliverable",
        {"publication_id": "deliverable-b", "attachments": [{"path": str(second)}]},
        tool_call_id="call-b",
    )
    assert published_b.is_error is False, published_b.output
    second_result = json.loads(published_b.output)
    assert second_result["message_id"] != first_result["message_id"]
    assert second_result["event_id"] != first_result["event_id"]
    projection = build_chat_projection(
        store, session=session, owner_id=owner, limit=20
    )
    deliverables = [
        row for row in projection["messages"]
        if row.get("message_id") in {first_result["message_id"], second_result["message_id"]}
    ]
    assert len(deliverables) == 2
    assert len({row["message_ref"] for row in deliverables}) == 2
    delta = build_chat_projection(
        store, session=session, owner_id=owner, offset=0,
        after_message_ordinal=deliverables[0]["source_sequence"], limit=20,
    )
    assert [
        row["message_id"] for row in delta["messages"]
        if row.get("message_id") == second_result["message_id"]
    ] == [second_result["message_id"]]
    feed = poll_frontend_feed(
        store, session["session_id"], owner_id=owner,
        run_id=accepted.run_id,
    )
    early_event = next(
        row for row in feed["durable_events"]
        if row["event_id"] == first_result["event_id"]
    )
    assert early_event["semantic_kind"] != "final"
    assert any(
        block["type"] == "media_ref"
        for block in early_event["content_blocks"]
    )

    store.finish_request(
        accepted.request_id, success=True, assistant_text="Both parts are ready."
    )
    final = store.get_message(
        store.get_run_by_request(accepted.request_id, owner_id=owner)["final_message_id"],
        session_id=session["session_id"], owner_id=owner,
    )
    assert final["text"] == "Both parts are ready."
    assert not any(part.get("attachment_id") for part in final["content"])
    assert len([
        event for event in store.events(session["session_id"], owner_id=owner)
        if event["kind"] == "assistant.output.available"
        and event["detail"].get("disposition") == "incremental"
    ]) == 2


@pytest.mark.asyncio
async def test_only_incremental_output_can_complete_without_duplicate_final(tmp_path):
    store, owner, session, accepted = _running_session(tmp_path)
    part = tmp_path / "only.txt"
    part.write_text("complete", encoding="utf-8")
    registry = _registry(
        tmp_path, store, owner, session,
        allowed_tools=["frontend_publish_deliverable"],
    )
    result = await registry.execute(
        "frontend_publish_deliverable",
        {"publication_id": "only-output", "attachments": [{"path": str(part)}]},
        tool_call_id="only-call",
    )
    assert result.is_error is False, result.output
    publication = json.loads(result.output)
    finished = store.finish_request(accepted.request_id, success=True)
    assert finished["state"] == "completed"
    assert finished["final_message_id"] is None
    assert len([
        row for row in store.messages(session["session_id"], owner_id=owner)
        if row["role"] == "assistant"
    ]) == 1
    assert store.get_message(
        publication["message_id"], session_id=session["session_id"], owner_id=owner
    )


@pytest.mark.asyncio
async def test_incremental_telegram_mirror_dispatches_while_workbench_stays_queued(
    tmp_path, monkeypatch
):
    from orchestrator import telegram_delivery_failover
    from orchestrator.frontend_delivery import freeze_run_delivery_route

    async def not_blocked(*_args, **_kwargs):
        return False

    monkeypatch.setattr(telegram_delivery_failover, "handle_blocked_send", not_blocked)
    route = freeze_run_delivery_route(
        message_source_id="session_api",
        session_surface="workbench",
        session_channel_key="default",
        chat_id=7,
        telegram_requested=True,
    )
    store, owner, session, accepted = _running_session(
        tmp_path, delivery_route=route
    )
    runtime = _ToolRuntime(tmp_path, store)
    part = tmp_path / "report.txt"
    part.write_text("incremental report", encoding="utf-8")
    registry = _registry(
        tmp_path, store, owner, session, runtime=runtime,
        allowed_tools=["frontend_publish_deliverable"],
    )
    arguments = {
        "publication_id": "report-1",
        "attachments": [{"path": str(part)}],
    }
    published = await registry.execute(
        "frontend_publish_deliverable", arguments, tool_call_id="first-call"
    )
    assert published.is_error is False, published.output
    result = json.loads(published.output)
    by_connector = {item["connector_id"]: item for item in result["deliveries"]}
    assert by_connector["backend_api"]["state"] == "queued"
    assert by_connector["telegram"]["state"] == "delivered"
    assert by_connector["telegram"]["proof"]
    assert len(runtime.app.bot.media) == 1
    assert runtime.app.bot.media[0]["payload"] == b"incremental report"
    assert store.get_run_by_request(accepted.request_id, owner_id=owner)["state"] == "running"
    replay = await registry.execute(
        "frontend_publish_deliverable", arguments, tool_call_id="second-call"
    )
    assert replay.is_error is False
    assert json.loads(replay.output)["replayed"] is True
    assert len(runtime.app.bot.media) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_before_dispatch", [False, True])
async def test_detached_gateway_incremental_telegram_is_delivered_by_worker(
    tmp_path, monkeypatch, finish_before_dispatch
):
    from orchestrator import telegram_delivery_failover
    from orchestrator.frontend_delivery import freeze_run_delivery_route
    from orchestrator.frontend_incremental_delivery import (
        dispatch_pending_telegram_deliverables,
    )

    async def not_blocked(*_args, **_kwargs):
        return False

    monkeypatch.setattr(telegram_delivery_failover, "handle_blocked_send", not_blocked)
    route = freeze_run_delivery_route(
        message_source_id="session_api",
        session_surface="workbench",
        session_channel_key="default",
        chat_id=7,
        telegram_requested=True,
    )
    store, owner, session, accepted = _running_session(tmp_path, delivery_route=route)
    part = tmp_path / "detached.txt"
    part.write_text("available before final", encoding="utf-8")
    registry = _registry(
        tmp_path, store, owner, session,
        allowed_tools=["frontend_publish_deliverable"], runtime=None,
    )
    published = await registry.execute(
        "frontend_publish_deliverable",
        {"publication_id": "detached-a", "attachments": [{"path": str(part)}]},
        tool_call_id="detached-call",
    )
    assert published.is_error is False, published.output
    publication = json.loads(published.output)
    assert next(
        item for item in publication["deliveries"]
        if item["connector_id"] == "telegram"
    )["state"] == "queued"
    if finish_before_dispatch:
        store.finish_request(accepted.request_id, success=True, assistant_text="All done")
    else:
        assert store.get_run_by_request(accepted.request_id, owner_id=owner)["state"] == "running"

    runtime = _ToolRuntime(tmp_path, store)
    runtime.name = "another-agent"
    assert await dispatch_pending_telegram_deliverables(runtime) == 0
    runtime.name = "agent1"
    assert await dispatch_pending_telegram_deliverables(runtime) == 1
    assert len(runtime.app.bot.media) == 1
    assert runtime.app.bot.media[0]["payload"] == b"available before final"
    assert await dispatch_pending_telegram_deliverables(runtime) == 0
    assert len(runtime.app.bot.media) == 1
    persisted = store.run_deliverable_publication(
        request_id=accepted.request_id, session_id=session["session_id"],
        owner_id=owner, agent_id="agent1", publication_id="detached-a",
    )
    telegram = next(
        item for item in persisted["deliveries"] if item["connector_id"] == "telegram"
    )
    assert telegram["state"] == "delivered"
    assert telegram["proof"]


@pytest.mark.asyncio
async def test_worker_marks_expired_incremental_claim_unknown_without_resending(tmp_path):
    from orchestrator.frontend_delivery import freeze_run_delivery_route
    from orchestrator.frontend_incremental_delivery import (
        dispatch_pending_telegram_deliverables,
    )

    route = freeze_run_delivery_route(
        message_source_id="telegram", session_surface="telegram",
        session_channel_key="7", chat_id=7, telegram_requested=False,
    )
    store, owner, session, _accepted = _running_session(tmp_path, delivery_route=route)
    part = tmp_path / "uncertain-worker.txt"
    part.write_text("one result", encoding="utf-8")
    registry = _registry(
        tmp_path, store, owner, session,
        allowed_tools=["frontend_publish_deliverable"], runtime=None,
    )
    published = await registry.execute(
        "frontend_publish_deliverable",
        {"publication_id": "worker-uncertain", "attachments": [{"path": str(part)}]},
        tool_call_id="worker-call",
    )
    assert published.is_error is False, published.output
    result = json.loads(published.output)
    endpoint_id = result["deliveries"][0]["endpoint_id"]
    claim = store.claim_delivery_outbox(
        session_id=session["session_id"], owner_id=owner,
        worker_id="worker-that-crashed", event_id=result["event_id"],
        connector_id="telegram", endpoint_id=endpoint_id, limit=1,
    )
    assert len(claim) == 1
    with store._connection() as connection:
        connection.execute(
            "UPDATE connector_delivery_tasks SET lease_expires_at=? WHERE task_id=?",
            ("2000-01-01T00:00:00+00:00", claim[0]["outbox_id"]),
        )
    runtime = _ToolRuntime(tmp_path, store)
    assert await dispatch_pending_telegram_deliverables(runtime) == 0
    assert runtime.app.bot.media == []
    publication = store.run_deliverable_publication(
        request_id="request-frontend-output", session_id=session["session_id"],
        owner_id=owner, agent_id="agent1", publication_id="worker-uncertain",
    )
    assert publication["deliveries"][0]["state"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["failed", "stopped"])
async def test_incremental_publication_rejects_stale_executor_and_retains_after_terminal(
    tmp_path, terminal
):
    store, owner, session, accepted = _running_session(tmp_path)
    part = tmp_path / "evidence.txt"
    part.write_text("durable evidence", encoding="utf-8")
    registry = _registry(
        tmp_path, store, owner, session,
        allowed_tools=["frontend_publish_deliverable"],
    )
    registry.audit_context["hashi_fencing_token"] = 0
    stale = await registry.execute(
        "frontend_publish_deliverable",
        {"publication_id": "evidence", "attachments": [{"path": str(part)}]},
        tool_call_id="stale-call",
    )
    assert stale.is_error is True
    assert not store.run_output_attachment_content(accepted.request_id)
    registry.audit_context["hashi_fencing_token"] = 1
    good = await registry.execute(
        "frontend_publish_deliverable",
        {"publication_id": "evidence", "attachments": [{"path": str(part)}]},
        tool_call_id="good-call",
    )
    assert good.is_error is False, good.output
    result = json.loads(good.output)
    if terminal == "stopped":
        store.cancel_run(accepted.run_id, owner_id=owner)
    else:
        store.finish_request(
            accepted.request_id, success=False, error_text="later work failed"
        )
    assert store.get_message(
        result["message_id"], session_id=session["session_id"], owner_id=owner
    )["content"][0]["attachment_id"] == result["attachments"][0]["attachment_id"]
    assert store.run_deliverable_publication(
        request_id=accepted.request_id, session_id=session["session_id"],
        owner_id=owner, agent_id="agent1", publication_id="evidence",
    )["event_id"] == result["event_id"]


@pytest.mark.asyncio
async def test_incremental_publish_transaction_rolls_back_and_retry_recovers(
    tmp_path, monkeypatch
):
    store, owner, session, accepted = _running_session(tmp_path)
    part = tmp_path / "checkpoint.txt"
    part.write_text("checkpoint", encoding="utf-8")
    registry = _registry(
        tmp_path, store, owner, session,
        allowed_tools=["frontend_publish_deliverable"],
    )
    args = {
        "publication_id": "checkpoint-1",
        "attachments": [{"path": str(part)}],
    }
    append_event = SessionStore._append_event

    def fail_before_commit(self, connection, **kwargs):
        if kwargs.get("kind") == "assistant.output.available":
            raise RuntimeError("injected transaction failure")
        return append_event(self, connection, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(SessionStore, "_append_event", fail_before_commit)
        failed = await registry.execute(
            "frontend_publish_deliverable", args, tool_call_id="attempt-1"
        )
    assert failed.is_error is True
    assert store.run_deliverable_publication(
        request_id=accepted.request_id, session_id=session["session_id"],
        owner_id=owner, agent_id="agent1", publication_id="checkpoint-1",
    ) is None
    assert not store.run_output_attachment_content(accepted.request_id)
    assert not [
        row for row in store.messages(session["session_id"], owner_id=owner)
        if row["role"] == "assistant"
    ]
    retried = await registry.execute(
        "frontend_publish_deliverable", args, tool_call_id="attempt-2"
    )
    assert retried.is_error is False, retried.output
    assert json.loads(retried.output)["replayed"] is False


@pytest.mark.asyncio
async def test_incremental_unknown_telegram_outcome_is_not_blindly_resent(
    tmp_path, monkeypatch
):
    from orchestrator import telegram_delivery_failover
    from orchestrator.frontend_delivery import freeze_run_delivery_route

    async def not_blocked(*_args, **_kwargs):
        return False

    monkeypatch.setattr(telegram_delivery_failover, "handle_blocked_send", not_blocked)
    route = freeze_run_delivery_route(
        message_source_id="telegram", session_surface="telegram",
        session_channel_key="7", chat_id=7, telegram_requested=False,
    )
    store, owner, session, _accepted = _running_session(
        tmp_path, delivery_route=route
    )
    runtime = _ToolRuntime(tmp_path, store)
    attempts = []

    async def uncertain_document(**kwargs):
        attempts.append(kwargs["chat_id"])
        raise RuntimeError("transport outcome unavailable")

    runtime.app.bot.send_document = uncertain_document
    part = tmp_path / "uncertain.txt"
    part.write_text("one logical result", encoding="utf-8")
    registry = _registry(
        tmp_path, store, owner, session, runtime=runtime,
        allowed_tools=["frontend_publish_deliverable"],
    )
    args = {
        "publication_id": "uncertain-1",
        "attachments": [{"path": str(part)}],
    }
    first = await registry.execute(
        "frontend_publish_deliverable", args, tool_call_id="first-attempt"
    )
    assert first.is_error is False, first.output
    first_result = json.loads(first.output)
    assert first_result["persisted"] is True
    assert first_result["deliveries"][0]["state"] == "unknown"
    replay = await registry.execute(
        "frontend_publish_deliverable", args, tool_call_id="replay-attempt"
    )
    assert replay.is_error is False
    assert json.loads(replay.output)["deliveries"][0]["state"] == "unknown"
    assert attempts == [7]


@pytest.mark.asyncio
async def test_incremental_and_final_bindings_share_run_attachment_cap(
    tmp_path, monkeypatch
):
    from orchestrator import session_store as session_store_module

    store, owner, session, _accepted = _running_session(tmp_path)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    monkeypatch.setattr(session_store_module, "MAX_SESSION_ATTACHMENTS_PER_MESSAGE", 1)
    registry = _registry(
        tmp_path, store, owner, session,
        allowed_tools=["frontend_publish_deliverable", "frontend_send_attachments"],
    )
    early = await registry.execute(
        "frontend_publish_deliverable",
        {"publication_id": "first", "attachments": [{"path": str(first)}]},
        tool_call_id="early",
    )
    assert early.is_error is False, early.output
    late = await registry.execute(
        "frontend_send_attachments",
        {"attachments": [{"path": str(second)}]},
        tool_call_id="late",
    )
    assert late.is_error is True
    assert len(store.run_output_attachment_content("request-frontend-output")) == 1


@pytest.mark.asyncio
async def test_incremental_media_marks_unsupported_mirror_failed_without_hiding_primary(
    tmp_path
):
    from orchestrator.frontend_delivery import freeze_run_delivery_route

    route = freeze_run_delivery_route(
        message_source_id="session_api", session_surface="workbench",
        session_channel_key="default", chat_id=7,
        telegram_requested=False, whatsapp_requested=True,
        whatsapp_channel_key="user@whatsapp",
    )
    store, owner, session, _accepted = _running_session(
        tmp_path, delivery_route=route
    )
    part = tmp_path / "report.txt"
    part.write_text("result", encoding="utf-8")
    registry = _registry(
        tmp_path, store, owner, session,
        allowed_tools=["frontend_publish_deliverable"],
    )
    published = await registry.execute(
        "frontend_publish_deliverable",
        {"publication_id": "report", "attachments": [{"path": str(part)}]},
        tool_call_id="report-call",
    )
    assert published.is_error is False, published.output
    by_connector = {
        row["connector_id"]: row
        for row in json.loads(published.output)["deliveries"]
    }
    assert by_connector["backend_api"]["state"] == "queued"
    assert by_connector["whatsapp"]["state"] == "failed"
    assert by_connector["whatsapp"]["last_error_code"] == "incremental_media_unsupported"


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["hchat", "remote", "exchange"])
async def test_incremental_media_reports_terminal_only_route_as_unsupported(
    tmp_path, surface
):
    from orchestrator.frontend_delivery import freeze_run_delivery_route

    route = freeze_run_delivery_route(
        message_source_id="hchat", session_surface=surface,
        session_channel_key="peer", chat_id=7, telegram_requested=False,
    )
    store, owner, session, _accepted = _running_session(
        tmp_path, delivery_route=route
    )
    part = tmp_path / "report.txt"
    part.write_text("result", encoding="utf-8")
    registry = _registry(
        tmp_path, store, owner, session,
        allowed_tools=["frontend_publish_deliverable"],
    )
    published = await registry.execute(
        "frontend_publish_deliverable",
        {"publication_id": "report", "attachments": [{"path": str(part)}]},
        tool_call_id="report-call",
    )
    assert published.is_error is False, published.output
    [destination] = json.loads(published.output)["deliveries"]
    assert destination["connector_id"] == surface
    assert destination["state"] == "failed"
    assert destination["last_error_code"] == "incremental_media_consumer_unavailable"


@pytest.mark.asyncio
async def test_bound_output_audio_is_promoted_to_durable_message_retention(tmp_path):
    store, owner, session, _accepted = _running_session(tmp_path)
    audio = tmp_path / "briefing.ogg"
    audio.write_bytes(b"OggS-workbench-output-audio")
    registry = _registry(tmp_path, store, owner, session)

    result = await registry.execute(
        "frontend_send_attachments",
        {"attachments": [{"path": str(audio), "caption": "Morning voice"}]},
        tool_call_id="durable-audio-output",
    )

    assert result.is_error is False, result.output
    published = json.loads(result.output)
    part = published["attachments"][0]
    assert part["modality"] == "audio"
    metadata = store.audio_assets.describe(
        part["attachment_id"],
        owner_id=owner,
        session_id=session["session_id"],
    )
    assert metadata["retention_indefinite"] is True
    assert metadata["retention_expires_at"] is None


@pytest.mark.asyncio
async def test_frontend_attachment_publish_is_idempotent_across_connectors(tmp_path):
    store, owner, session, _accepted = _running_session(tmp_path)
    target = tmp_path / "proof.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\nproof")
    arguments = {"attachments": [{"path": str(target)}]}
    registry = _registry(tmp_path, store, owner, session)

    first = await registry.execute(
        "frontend_send_attachments", arguments, tool_call_id="stable-output-call"
    )
    assert first.is_error is False
    target.write_bytes(b"\x89PNG\r\n\x1a\nchanged")
    conflict = await registry.execute(
        "frontend_send_attachments", arguments, tool_call_id="stable-output-call"
    )
    assert conflict.is_error is True
    assert "idempotency" in conflict.output.casefold()

    tui = _registry(tmp_path, store, owner, session, surface="tui")
    accepted = await tui.execute(
        "frontend_send_attachments", arguments, tool_call_id="tui-output-call"
    )
    assert accepted.is_error is False
    assert json.loads(accepted.output)["ok"] is True

    # Unified attachment delivery contract: Telegram turns may bind to the
    # canonical Session (push stays the caller's concern).
    telegram = _registry(tmp_path, store, owner, session, surface="telegram")
    bound = await telegram.execute(
        "frontend_send_attachments", arguments, tool_call_id="telegram-output-call"
    )
    assert bound.is_error is False
    assert '"ok": true' in bound.output


@pytest.mark.asyncio
async def test_frontend_attachment_rejects_files_outside_authorized_roots(tmp_path):
    store, owner, session, _accepted = _running_session(tmp_path)
    authorized = tmp_path / "authorized"
    authorized.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("not an authorized attachment", encoding="utf-8")
    registry = _registry(
        tmp_path,
        store,
        owner,
        session,
        access_root=authorized,
        workspace_dir=authorized,
    )

    result = await registry.execute(
        "frontend_send_attachments",
        {"attachments": [{"path": str(outside)}]},
        tool_call_id="outside-root-output",
    )

    assert result.is_error is True
    assert "authorized" in result.output.casefold() or "access" in result.output.casefold()


@pytest.mark.asyncio
async def test_explicit_telegram_text_tool_publishes_through_fc(
    tmp_path, monkeypatch
):
    from orchestrator import runtime_delivery

    store = SessionStore(
        tmp_path / "state" / "sessions.sqlite3",
        instance_id="HASHI3",
    )
    runtime = _ToolRuntime(tmp_path, store)

    async def not_blocked(*_args, **_kwargs):
        return False

    monkeypatch.setattr(
        runtime_delivery.telegram_delivery_failover,
        "handle_blocked_send",
        not_blocked,
    )
    registry = ToolRegistry(
        allowed_tools=["telegram_send"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
        audit_context={
            "_runtime": runtime,
            "global_config": runtime.global_config,
            "request_id": "req-explicit-text",
        },
    )

    result = await registry.execute(
        "telegram_send",
        {"chat_id": "7", "text": "standard notification"},
        tool_call_id="explicit-text-call",
    )

    assert result.is_error is False, result.output
    assert json.loads(result.output)["delivery_state"] == "accepted"
    assert len(runtime.app.bot.messages) == 1
    assert runtime.app.bot.messages[0]["text"] == "standard notification"
    session = store.resolve_primary_session(owner_id="user:7", agent_id="agent1")
    event = next(
        item
        for item in store.events(session["session_id"], owner_id="user:7")
        if item["kind"] == "frontend.message.recorded"
    )
    receipts = store.frontend_delivery_receipts(
        session_id=session["session_id"],
        owner_id="user:7",
        event_id=event["event_id"],
    )
    assert [item["status"] for item in receipts] == ["delivered"]


@pytest.mark.asyncio
async def test_explicit_telegram_file_tool_is_fc_media_and_replay_safe(
    tmp_path, monkeypatch
):
    from orchestrator import runtime_delivery

    store = SessionStore(
        tmp_path / "state" / "sessions.sqlite3",
        instance_id="HASHI3",
    )
    runtime = _ToolRuntime(tmp_path, store)

    async def not_blocked(*_args, **_kwargs):
        return False

    monkeypatch.setattr(
        runtime_delivery.telegram_delivery_failover,
        "handle_blocked_send",
        not_blocked,
    )
    report = tmp_path / "report.txt"
    report.write_bytes(b"destination-scoped FC media")
    registry = ToolRegistry(
        allowed_tools=["telegram_send_file"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
        audit_context={
            "_runtime": runtime,
            "global_config": runtime.global_config,
            "request_id": "req-explicit-media",
        },
    )
    arguments = {
        "path": str(report),
        "chat_id": "7",
        "caption": "report",
    }

    first = await registry.execute(
        "telegram_send_file",
        arguments,
        tool_call_id="explicit-media-call",
    )
    replay = await registry.execute(
        "telegram_send_file",
        arguments,
        tool_call_id="explicit-media-call",
    )

    assert first.is_error is False, first.output
    assert replay.is_error is False, replay.output
    first_payload = json.loads(first.output)
    replay_payload = json.loads(replay.output)
    assert first_payload["delivery_state"] == "delivered"
    assert first_payload["event_id"] == replay_payload["event_id"]
    assert replay_payload["replayed"] is True
    assert len(runtime.app.bot.media) == 1
    assert runtime.app.bot.media[0]["payload"] == report.read_bytes()
    assert runtime.app.bot.media[0]["caption"] == "report"


@pytest.mark.asyncio
async def test_standalone_telegram_file_compatibility_uses_fc_outbox_once(tmp_path):
    from orchestrator.frontend_telegram_connector import (
        publish_explicit_telegram_notification,
    )

    # Preserve the caller's local rendition choice even when MIME inference
    # would normally make Telegram display this as a photo.
    source = tmp_path / "report.png"
    source.write_text("standard FC attachment", encoding="utf-8")
    database = tmp_path / "state" / "sessions.sqlite3"
    attachment_root = tmp_path / "managed"
    bot = _ToolBot()
    kwargs = {
        "root": tmp_path,
        "instance_id": "HASHI1",
        "agent_id": "test-agent",
        "owner_id": "user:7",
        "authorized_id": 7,
        "chat_id": 7,
        "token": "test-token",
        "publication_id": "standalone-file-publication",
        "file_path": source,
        "caption": "report",
        "presentation_role": "document",
        "session_db_path": database,
        "attachment_root": attachment_root,
        "agent_lifecycle_id": "a" * 32,
        "bot": bot,
    }

    first = await publish_explicit_telegram_notification(**kwargs)
    replay = await publish_explicit_telegram_notification(**kwargs)

    assert first["accepted"] is True
    assert replay["accepted"] is True
    assert len(bot.media) == 1
    assert bot.media[0]["payload"] == b"standard FC attachment"
    store = SessionStore(
        database,
        instance_id="HASHI1",
        attachment_root=attachment_root,
    )
    session = store.resolve_primary_session(
        owner_id="user:7", agent_id="test-agent"
    )
    event = next(
        item
        for item in store.events(session["session_id"], owner_id="user:7")
        if item["kind"] == "frontend.message.recorded"
    )
    receipts = store.frontend_delivery_receipts(
        session_id=session["session_id"],
        owner_id="user:7",
        event_id=event["event_id"],
    )
    assert receipts[0]["status"] == "delivered"
    with store._connection() as connection:
        retained = connection.execute(
            "SELECT retention_indefinite FROM session_attachments"
        ).fetchone()[0]
    assert retained == 1
