import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.stream_events import StreamEvent
from orchestrator.chat_transcript_projection import (
    build_chat_projection,
    read_chat_transcript,
)
from orchestrator.her_message_router import HERMessageRouter
from orchestrator.request_activity import RequestActivityStore
from orchestrator.session_store import SessionStore
from orchestrator.workbench_api import WorkbenchApiServer


def test_activity_projects_real_public_channels_and_keeps_disabled_internal_private():
    settings = {"think": True, "commentary": True, "verbose": False}
    store = RequestActivityStore()
    store.presentation_settings = lambda: settings
    store.start("r")
    for event in [
        StreamEvent(kind="thinking", summary="summary", raw_delta=" exact delta ", event_id="t"),
        StreamEvent(kind="commentary", summary="checkpoint", event_id="c"),
        StreamEvent(kind="tool_start", summary="tool", event_id="v"),
        StreamEvent(kind="thinking", summary="private", delivery_class="internal", event_id="i"),
    ]:
        store.publish_stream("r", event)
    events = store.poll("r")["events"][1:]
    assert [e["presentation_enabled"] for e in events] == [True, True, False, False]
    assert [e["presentation_channel"] for e in events[:2]] == ["thinking", "commentary"]
    assert events[0]["raw_delta"] == " exact delta "
    settings["think"] = False
    store.publish_stream("r", StreamEvent(kind="thinking", summary="off", raw_delta="off"))
    disabled = store.poll("r")["events"][-1]
    assert disabled["presentation_enabled"] is False
    assert "raw_delta" not in disabled


def test_activity_reports_retention_hole_instead_of_empty_complete_replay():
    store = RequestActivityStore(max_events_per_request=32)
    store.start("r")
    for number in range(40):
        store.publish_stream("r", StreamEvent(kind="commentary", summary=str(number)))
    payload = store.poll("r", after_sequence=0)
    assert payload["earliest_available_sequence"] == 10
    assert payload["replay_complete"] is False
    assert store.poll("r", after_sequence=9)["replay_complete"] is True


@pytest.mark.asyncio
async def test_transcript_identity_and_recovery_are_bound_to_current_session(tmp_path: Path):
    server = WorkbenchApiServer.__new__(WorkbenchApiServer)
    server.global_config = SimpleNamespace(instance_id="test", authorized_id=7, deployment_profile="personal")
    server.session_store = SessionStore(tmp_path / "sessions.sqlite", instance_id="test")
    server._runtime_map = lambda: {}
    server._load_agent_rows = lambda: [{"name": "a", "workspace_dir": str(tmp_path)}]
    session = server.session_store.resolve_session(owner_id="user:7", agent_id="a", surface="workbench", channel_key="default")
    accepted = server.session_store.accept_run(session_id=session["session_id"], owner_id="user:7", agent_id="a", request_id="r", text="hello", source="api", idempotency_key="first")
    workspace = server.session_store.session_workspace(session["session_id"], session["context_generation"])
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / "transcript.jsonl"
    first = json.dumps({"role": "user", "text": "重复", "ts": "2026-09-12T09:00:00.123456+00:00"}, ensure_ascii=False) + "\n"
    encoded_first = first.encode("utf-8")
    path.write_bytes(encoded_first + encoded_first)
    request = SimpleNamespace(match_info={"name": "a"}, query={})
    payload = json.loads((await server.handle_transcript_recent(request)).text)
    assert payload["session_id"] == session["session_id"]
    assert payload["context_generation"] == 1
    refs = [m["message_ref"] for m in payload["messages"]]
    assert len(set(refs)) == 3
    assert payload["messages"][0]["text"] == "hello"
    assert payload["messages"][0]["canonical"] is True
    # Exercise recovery from an explicitly invalid byte cursor on every OS.
    # Path.write_text() newline translation previously made this invalid only
    # on Windows and a valid record boundary on Linux.
    poll = SimpleNamespace(
        match_info={"name": "a"},
        query={"offset": str(len(encoded_first) - 1)},
    )
    increment = json.loads((await server.handle_transcript_poll(poll)).text)
    assert increment["cursor_reset"] is True
    assert increment["messages"][0]["message_ref"] == refs[1]
    assert payload["requests"][0]["request_id"] == accepted.request_id
    assert payload["requests"][0]["session_id"] == session["session_id"]
    assert "text" not in payload["requests"][0]


@pytest.mark.asyncio
async def test_open_transcript_receives_history_generation_reset_after_continuity_import(
    tmp_path: Path,
):
    server = WorkbenchApiServer.__new__(WorkbenchApiServer)
    server.global_config = SimpleNamespace(
        instance_id="HASHI3",
        authorized_id=7,
        deployment_profile="personal",
    )
    server.session_store = SessionStore(
        tmp_path / "target.sqlite", instance_id="HASHI3"
    )
    server._runtime_map = lambda: {}
    server._load_agent_rows = lambda: [{"name": "a", "workspace_dir": str(tmp_path)}]
    target_session = server.session_store.resolve_session(
        owner_id="user:7",
        agent_id="a",
        surface="workbench",
        channel_key="default",
    )
    target_run = server.session_store.accept_run(
        session_id=target_session["session_id"],
        owner_id="user:7",
        agent_id="a",
        request_id="target-new",
        text="new target question",
        source="workbench",
        idempotency_key="target-new",
    )
    server.session_store.mark_request_running(
        target_run.request_id, worker_id="fixture"
    )
    server.session_store.finish_request(
        target_run.request_id,
        success=True,
        assistant_text="new target answer",
        assistant_source="fixture",
    )
    initial = json.loads(
        (
            await server.handle_transcript_recent(
                SimpleNamespace(match_info={"name": "a"}, query={})
            )
        ).text
    )
    assert initial["history_generation"] == 1

    source = SessionStore(tmp_path / "source.sqlite", instance_id="HASHI2")
    source_session = source.ensure_default_session(owner_id="user:7", agent_id="a")
    source_run = source.accept_run(
        session_id=source_session["session_id"],
        owner_id="user:7",
        agent_id="a",
        request_id="source-old",
        text="old source question",
        source="workbench",
        idempotency_key="source-old",
    )
    source.mark_request_running(source_run.request_id, worker_id="fixture")
    source.finish_request(
        source_run.request_id,
        success=True,
        assistant_text="old source answer",
        assistant_source="fixture",
    )
    capsule = source.export_conversation_continuity(
        owner_id="user:7",
        agent_id="a",
        source_instance="HASHI2",
        transfer_id="open-page-reset",
        history_mode="move",
    )
    server.session_store.import_conversation_continuity(
        capsule,
        owner_id="user:7",
        agent_id="a",
        transfer_id="open-page-reset",
        history_mode="move",
    )

    reset = json.loads(
        (
            await server.handle_transcript_poll(
                SimpleNamespace(
                    match_info={"name": "a"},
                    query={
                        "offset": str(initial["offset"]),
                        "history_generation": str(initial["history_generation"]),
                    },
                )
            )
        ).text
    )

    assert reset["history_generation"] == 2
    assert reset["history_reset"] is True
    assert reset["cursor_reset"] is True
    assert [item["text"] for item in reset["messages"]] == [
        "old source question",
        "old source answer",
        "new target question",
        "new target answer",
    ]
    assert len({item["message_ref"] for item in reset["messages"]}) == 4


def test_canonical_snapshot_keeps_transcript_only_thinking_between_run_messages(
    tmp_path: Path,
):
    store = SessionStore(tmp_path / "ordered.sqlite", instance_id="HASHI3")
    session = store.ensure_default_session(owner_id="user:7", agent_id="a")
    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id="user:7",
        agent_id="a",
        request_id="ordered",
        text="question",
        source="workbench",
        idempotency_key="ordered",
    )
    store.mark_request_running(accepted.request_id, worker_id="fixture")
    store.finish_request(
        accepted.request_id,
        success=True,
        assistant_text="answer",
        assistant_source="fixture",
    )
    workspace = store.session_workspace(
        session["session_id"], session["context_generation"]
    )
    workspace.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "role": "user",
            "text": "question",
            "message_ref": f"run:{accepted.run_id}:user",
        },
        {"role": "thinking", "text": "working"},
        {
            "role": "assistant",
            "text": "answer",
            "message_ref": f"run:{accepted.run_id}:assistant",
        },
    ]
    (workspace / "transcript.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in rows),
        encoding="utf-8",
    )

    payload = build_chat_projection(
        store,
        session=store.get_session(session["session_id"]),
        owner_id="user:7",
    )

    assert [item["text"] for item in payload["messages"]] == [
        "question",
        "working",
        "answer",
    ]


def test_canonical_projection_keeps_final_identity_and_safe_media_metadata(tmp_path: Path):
    store = SessionStore(tmp_path / "media.sqlite", instance_id="HASHI2")
    session = store.ensure_default_session(owner_id="user:7", agent_id="a")
    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id="user:7",
        agent_id="a",
        request_id="media-request",
        text="User sent a photo saved at C:\\private\\photo.jpg",
        source="photo",
        idempotency_key="media-request",
        content=[
            {
                "type": "text",
                "text": "User sent a photo saved at C:\\private\\photo.jpg",
            },
            {
                "type": "media",
                "attachment_id": "attachment-photo",
                "modality": "image",
                "kind": "photo",
                "mime_type": "image/jpeg",
                "filename": "photo.jpg",
                "caption": "look here",
                "local_ref": "C:\\private\\photo.jpg",
                "sha256": "secret-digest",
                "size_bytes": 123,
            },
        ],
    )
    store.mark_request_running(accepted.request_id, worker_id="fixture")
    store.finish_request(
        accepted.request_id,
        success=True,
        assistant_text="image answer",
        assistant_source="fixture",
    )

    payload = build_chat_projection(
        store,
        session=store.get_session(session["session_id"]),
        owner_id="user:7",
    )
    user, answer = payload["messages"]

    assert user["text"] == "look here"
    assert user["attachments"] == [
        {
            "attachment_id": "attachment-photo",
            "modality": "image",
            "kind": "photo",
            "mime_type": "image/jpeg",
            "filename": "photo.jpg",
            "caption": "look here",
            "size_bytes": 123,
            "message_id": accepted.message_id,
        }
    ]
    assert "local_ref" not in user["attachments"][0]
    assert "sha256" not in user["attachments"][0]
    assert answer["request_id"] == accepted.request_id
    assert answer["run_id"] == accepted.run_id
    assert answer["kind"] == "final"
    assert answer["created_at"] == answer["ts"]


def test_canonical_projection_groups_staged_attachments_in_message_order(
    tmp_path: Path,
):
    store = SessionStore(tmp_path / "frontend.sqlite", instance_id="HASHI3")
    owner_id = "user:7"
    session = store.ensure_default_session(owner_id=owner_id, agent_id="a")
    fixtures = [
        ("first.png", "image/png", b"\x89PNG\r\n\x1a\nfirst"),
        ("notes.txt", "text/plain", b"second"),
        ("clip.webm", "video/webm", bytes.fromhex("1a45dfa3") + b"third"),
    ]
    attachment_ids = []
    for filename, media_type, body in fixtures:
        staged = store.stage_attachment(
            session_id=session["session_id"],
            owner_id=owner_id,
            filename=filename,
            media_type=media_type,
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
        )
        store.upload_attachment_bytes(
            session_id=session["session_id"],
            owner_id=owner_id,
            attachment_id=staged["attachment_id"],
            payload=body,
        )
        store.commit_attachment(
            session_id=session["session_id"],
            owner_id=owner_id,
            attachment_id=staged["attachment_id"],
        )
        attachment_ids.append(staged["attachment_id"])

    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner_id,
        agent_id="a",
        request_id="frontend-attachments",
        text="inspect these in order",
        source="session-api",
        idempotency_key="frontend-attachments",
        content=[
            {"type": "text", "text": "inspect these in order"},
            *[
                {"type": "attachment", "attachment_id": attachment_id}
                for attachment_id in attachment_ids
            ],
        ],
    )
    store.mark_request_running(accepted.request_id, worker_id="fixture")
    store.finish_request(
        accepted.request_id,
        success=True,
        assistant_text="grouped",
        assistant_source="fixture",
    )

    payload = build_chat_projection(
        store,
        session=store.get_session(session["session_id"]),
        owner_id=owner_id,
    )
    user = payload["messages"][0]

    assert user["message_id"] == accepted.message_id
    assert [item["attachment_id"] for item in user["attachments"]] == attachment_ids
    assert [item["filename"] for item in user["attachments"]] == [
        item[0] for item in fixtures
    ]
    assert [item["modality"] for item in user["attachments"]] == [
        "image",
        "document",
        "video",
    ]
    assert all(
        item["message_id"] == accepted.message_id for item in user["attachments"]
    )
    assert all("local_ref" not in item for item in user["attachments"])
    assert all("sha256" not in item for item in user["attachments"])


@pytest.mark.asyncio
async def test_transcript_image_attachment_is_bounded_to_visible_agent_media(
    tmp_path: Path,
):
    server = _server(tmp_path)
    media_root = tmp_path / "media"
    agent_media = media_root / "a"
    agent_media.mkdir(parents=True)
    payload = b"\x89PNG\r\n\x1a\nworkbench-preview"
    image = agent_media / "photo.png"
    image.write_bytes(payload)
    server.global_config.base_media_dir = media_root
    server._runtime_map = lambda: {"a": SimpleNamespace(media_dir=agent_media)}
    session = server.session_store.ensure_default_session(
        owner_id="user:7", agent_id="a"
    )
    accepted = server.session_store.accept_run(
        session_id=session["session_id"],
        owner_id="user:7",
        agent_id="a",
        request_id="image-preview",
        text="photo",
        source="photo",
        idempotency_key="image-preview",
        content=[
            {
                "type": "media",
                "attachment_id": "attachment-image",
                "modality": "image",
                "kind": "photo",
                "mime_type": "image/png",
                "filename": "photo.png",
                "local_ref": str(image),
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    )
    request = SimpleNamespace(
        match_info={
            "name": "a",
            "message_id": accepted.message_id,
            "attachment_id": "attachment-image",
        },
        headers={},
    )

    response = await server.handle_transcript_attachment(request)

    assert response.status == 200
    assert response.body == payload
    assert response.content_type == "image/png"
    assert response.headers["Cache-Control"] == "private, no-store"
    assert "local_ref" not in response.headers

    image.rename(tmp_path / "escaped.png")
    missing = await server.handle_transcript_attachment(request)
    assert missing.status == 404

    escaped_image = tmp_path / "escaped.png"
    escaped_payload = escaped_image.read_bytes()
    escaped_run = server.session_store.accept_run(
        session_id=session["session_id"],
        owner_id="user:7",
        agent_id="a",
        request_id="escaped-preview",
        text="outside photo",
        source="photo",
        idempotency_key="escaped-preview",
        content=[
            {
                "type": "media",
                "attachment_id": "attachment-escaped",
                "modality": "image",
                "kind": "photo",
                "mime_type": "image/png",
                "filename": "escaped.png",
                "local_ref": str(escaped_image),
                "size_bytes": len(escaped_payload),
                "sha256": hashlib.sha256(escaped_payload).hexdigest(),
            }
        ],
    )
    escaped_request = SimpleNamespace(
        match_info={
            "name": "a",
            "message_id": escaped_run.message_id,
            "attachment_id": "attachment-escaped",
        },
        headers={},
    )
    escaped = await server.handle_transcript_attachment(escaped_request)
    assert escaped.status == 409
    assert str(escaped_image) not in escaped.text


@pytest.mark.asyncio
async def test_staged_frontend_image_is_visible_through_transcript_route(
    tmp_path: Path,
):
    server = _server(tmp_path)
    owner_id = "user:7"
    payload = b"\x89PNG\r\n\x1a\nfrontend-preview"
    session = server.session_store.ensure_default_session(
        owner_id=owner_id, agent_id="a"
    )
    staged = server.session_store.stage_attachment(
        session_id=session["session_id"],
        owner_id=owner_id,
        filename="frontend.png",
        media_type="image/png",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    server.session_store.upload_attachment_bytes(
        session_id=session["session_id"],
        owner_id=owner_id,
        attachment_id=staged["attachment_id"],
        payload=payload,
    )
    server.session_store.commit_attachment(
        session_id=session["session_id"],
        owner_id=owner_id,
        attachment_id=staged["attachment_id"],
    )
    accepted = server.session_store.accept_run(
        session_id=session["session_id"],
        owner_id=owner_id,
        agent_id="a",
        request_id="frontend-image-preview",
        text="frontend photo",
        source="session-api",
        idempotency_key="frontend-image-preview",
        content=[
            {"type": "text", "text": "frontend photo"},
            {"type": "attachment", "attachment_id": staged["attachment_id"]},
        ],
    )
    request = SimpleNamespace(
        match_info={
            "name": "a",
            "message_id": accepted.message_id,
            "attachment_id": staged["attachment_id"],
        },
        headers={},
    )

    response = await server.handle_transcript_attachment(request)

    assert response.status == 200
    assert response.body == payload
    assert response.content_type == "image/png"


def test_projection_message_cursor_streams_presentation_only_messages(tmp_path: Path):
    store = SessionStore(tmp_path / "presentation.sqlite", instance_id="HASHI2")
    session = store.ensure_default_session(owner_id="user:7", agent_id="a")
    snapshot = build_chat_projection(
        store,
        session=session,
        owner_id="user:7",
    )
    store.append_presentation_message(
        session_id=session["session_id"],
        owner_id="user:7",
        agent_id="a",
        role="assistant",
        text="✅ restart completed",
        source="telegram.runtime_notice",
        idempotency_key="reboot:one:final",
        content_format="telegram-html",
    )

    polled = build_chat_projection(
        store,
        session=store.get_session(session["session_id"]),
        owner_id="user:7",
        offset=snapshot["offset"],
        after_message_ordinal=snapshot["message_cursor"],
    )

    assert [message["text"] for message in polled["messages"]] == [
        "✅ restart completed"
    ]
    assert polled["messages"][0]["history_eligible"] is False
    assert polled["message_cursor"] > snapshot["message_cursor"]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["bad", "0", "-1"])
async def test_transcript_poll_rejects_invalid_history_generation(
    tmp_path: Path,
    value: str,
):
    server = _server(tmp_path)
    response = await server.handle_transcript_poll(
        SimpleNamespace(
            match_info={"name": "a"},
            query={"offset": "0", "history_generation": value},
        )
    )

    assert response.status == 400
    assert json.loads(response.text)["error_code"] == "invalid_history_generation"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["bad", "-1", "1.2"])
async def test_transcript_poll_rejects_invalid_message_cursor(
    tmp_path: Path,
    value: str,
):
    server = _server(tmp_path)
    response = await server.handle_transcript_poll(
        SimpleNamespace(
            match_info={"name": "a"},
            query={"offset": "0", "message_cursor": value},
        )
    )

    assert response.status == 400
    assert json.loads(response.text)["error_code"] == "invalid_message_cursor"


def _server(tmp_path: Path):
    server = WorkbenchApiServer.__new__(WorkbenchApiServer)
    server.global_config = SimpleNamespace(instance_id="test", authorized_id=7, deployment_profile="personal")
    server.session_store = SessionStore(tmp_path / "sessions.sqlite", instance_id="test")
    server._load_agent_rows = lambda: [{"name": "a", "workspace_dir": str(tmp_path)}]
    server._runtime_map = lambda: {}
    return server


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", ["handle_transcript_recent", "handle_transcript_poll"])
async def test_transcript_requires_authenticated_owner_without_creating_anonymous_session(tmp_path: Path, handler: str):
    server = _server(tmp_path)
    server.global_config.deployment_profile = "enterprise"
    server.identity_service = None
    request = SimpleNamespace(match_info={"name": "a"}, query={}, headers={})

    response = await getattr(server, handler)(request)

    assert response.status == 401
    assert json.loads(response.text)["error_code"] == "not_authenticated"
    assert server.session_store.list_sessions(owner_id="None") == []
    assert server.session_store.list_sessions(owner_id="user:7") == []


@pytest.mark.asyncio
async def test_transcript_path_rows_and_discovery_share_one_binding_snapshot(tmp_path: Path, monkeypatch):
    server = _server(tmp_path)
    store = server.session_store
    first = store.resolve_session(owner_id="user:7", agent_id="a", surface="workbench", channel_key="default")
    second = store.create_session(owner_id="user:7", agent_id="a")
    for session, request_id in [(first, "r-first"), (second, "r-second")]:
        store.accept_run(session_id=session["session_id"], owner_id="user:7", agent_id="a",
                         request_id=request_id, text=request_id, source="api", idempotency_key=request_id)
        workspace = store.session_workspace(session["session_id"], session["context_generation"])
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "transcript.jsonl").write_text(json.dumps({"role": "assistant", "text": request_id}) + "\n")

    original_resolve = store.resolve_primary_session
    resolutions = []

    def resolve_then_switch(**kwargs):
        captured = original_resolve(**kwargs)
        resolutions.append(captured["session_id"])
        store.bind_primary_session(
            owner_id="user:7",
            agent_id="a",
            session_id=second["session_id"],
        )
        return captured

    monkeypatch.setattr(store, "resolve_primary_session", resolve_then_switch)
    response = await server.handle_transcript_recent(SimpleNamespace(match_info={"name": "a"}, query={}))
    payload = json.loads(response.text)

    assert payload["messages"][0]["text"] == "r-first"
    assert payload["messages"][0]["session_id"] == first["session_id"]
    assert payload["session_id"] == first["session_id"]
    assert [run["request_id"] for run in payload["requests"]] == ["r-first"]
    assert resolutions == [first["session_id"]]


@pytest.mark.parametrize("cursor_kind", ["middle", "utf8-middle", "negative", "past-end", "partial-end"])
def test_invalid_transcript_cursors_return_a_bounded_snapshot_with_an_explicit_gap(tmp_path: Path, cursor_kind: str):
    session = {"session_id": "fixture-session", "context_generation": 1}
    path = tmp_path / "transcript.jsonl"
    lines = [(json.dumps({"role": "assistant", "text": text}, ensure_ascii=False) + "\n").encode()
             for text in ["first", "第二条", "third"]]
    complete = b"".join(lines)
    partial = b'{"role":"assistant","text":"pending'
    path.write_bytes(complete + partial)
    offsets = {"middle": len(lines[0]) + 2,
               "utf8-middle": len(lines[0]) + lines[1].index("第".encode()) + 1,
               "negative": -1, "past-end": len(complete + partial) + 30,
               "partial-end": len(complete + partial)}

    payload = read_chat_transcript(path, session=session, offset=offsets[cursor_kind], limit=2)

    assert payload["cursor_reset"] is True
    assert payload["history_complete"] is False
    assert [row["text"] for row in payload["messages"]] == ["第二条", "third"]
    assert payload["offset"] == len(complete)
    assert [row["source_sequence"] for row in payload["messages"]] == [len(lines[0]), len(lines[0] + lines[1])]


def test_transcript_half_record_is_not_acknowledged_and_replays_once_after_completion(tmp_path: Path):
    session = {"session_id": "fixture-session", "context_generation": 1}
    path = tmp_path / "transcript.jsonl"
    first = (json.dumps({"role": "user", "text": "first"}) + "\n").encode()
    last = (json.dumps({"role": "assistant", "text": "second"}) + "\n").encode()
    path.write_bytes(first + last[:20])
    pending = read_chat_transcript(path, session=session, offset=len(first))
    assert pending["cursor_reset"] is False
    assert pending["messages"] == []
    assert pending["offset"] == len(first)

    with path.open("ab") as stream:
        stream.write(last[20:])
    completed = read_chat_transcript(path, session=session, offset=pending["offset"])
    assert [row["text"] for row in completed["messages"]] == ["second"]
    assert completed["offset"] == len(first + last)
    assert read_chat_transcript(path, session=session, offset=completed["offset"])["messages"] == []


def test_transcript_foreign_identity_is_excluded_instead_of_relabeled(tmp_path: Path):
    session = {"session_id": "fixture-session", "context_generation": 2}
    path = tmp_path / "transcript.jsonl"
    rows = [
        {"role": "assistant", "text": "legacy"},
        {"role": "assistant", "text": "current", "session_id": "fixture-session", "context_generation": 2},
        {"role": "assistant", "text": "foreign", "session_id": "other-session", "context_generation": 2},
        {"role": "assistant", "text": "old-context", "session_id": "fixture-session", "context_generation": 1},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    result = read_chat_transcript(path, session=session)
    assert [row["text"] for row in result["messages"]] == ["legacy", "current"]
    assert all(row["session_id"] == session["session_id"] and row["context_generation"] == 2
               for row in result["messages"])
    assert result["offset"] == path.stat().st_size


@pytest.mark.asyncio
async def test_required_commentary_matches_canonical_router_with_optional_switch_off():
    store = RequestActivityStore()
    store.presentation_settings = lambda: {"commentary": False, "think": False, "verbose": False}
    store.start("r-required")
    event = StreamEvent(kind="commentary", summary="mandatory public notice", event_id="required",
                        delivery_class="user_commentary", required=True)
    delivered = []
    router = HERMessageRouter(request_id="r-required", logger=logging.getLogger("test"),
                              commentary_enabled=lambda: False, commentary_presenter=delivered.append)
    await router.route(event)
    store.publish_stream("r-required", event)
    assert delivered == [event]
    projected = store.poll("r-required")["events"][-1]
    assert projected["presentation_enabled"] is True
    assert projected["presentation_channel"] == "commentary"
    assert projected["summary"] == event.summary
