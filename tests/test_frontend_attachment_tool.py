from __future__ import annotations

import json

import pytest

from orchestrator.session_store import SessionStore
from tools.registry import TOOL_TIERS, ToolRegistry
from tools.schemas import TOOL_SCHEMA_MAP


def _running_session(tmp_path):
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
    )
    assert store.mark_request_running(
        accepted.request_id, worker_id="HASHI3:agent1"
    ) == 1
    return store, owner, session, accepted


def _registry(tmp_path, store, owner, session, *, surface="generic-desktop"):
    return ToolRegistry(
        allowed_tools=["frontend_send_attachments"],
        access_root=tmp_path,
        workspace_dir=tmp_path,
        secrets={},
        audit_context={
            "agent_name": "agent1",
            "request_id": "request-frontend-output",
            "hashi_session_id": session["session_id"],
            "owner_id": owner,
            "session_surface": surface,
            "session_store_descriptor": {
                "db_path": str(store.db_path),
                "instance_id": store.instance_id,
                "attachment_root": str(store.attachment_files_root),
            },
        },
    )


def test_frontend_attachment_tool_is_standard_multi_attachment_contract():
    function = TOOL_SCHEMA_MAP["frontend_send_attachments"]["function"]
    parameters = function["parameters"]

    assert "current frontend Session reply" in function["description"]
    assert parameters["required"] == ["attachments"]
    assert parameters["properties"]["attachments"]["maxItems"] == 16
    assert "frontend_send_attachments" in TOOL_TIERS["communication"]


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
    assert [part["filename"] for part in published["attachments"]] == [
        "first.png",
        "notes.txt",
    ]

    replay = await registry.execute(
        "frontend_send_attachments", arguments, tool_call_id="publish-call-1"
    )
    assert replay.is_error is False
    assert json.loads(replay.output)["replayed"] is True
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
async def test_frontend_attachment_publish_is_idempotent_and_tui_is_separate(tmp_path):
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
    rejected = await tui.execute(
        "frontend_send_attachments", arguments, tool_call_id="tui-output-call"
    )
    assert rejected.is_error is True
    assert "built-in tui" in rejected.output.casefold()

    # Unified attachment delivery contract: Telegram turns may bind to the
    # canonical Session (push stays the caller's concern).
    telegram = _registry(tmp_path, store, owner, session, surface="telegram")
    bound = await telegram.execute(
        "frontend_send_attachments", arguments, tool_call_id="telegram-output-call"
    )
    assert bound.is_error is False
    assert '"ok": true' in bound.output
