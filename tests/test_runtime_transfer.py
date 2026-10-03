import json
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from orchestrator import runtime_transfer, ui_language
from orchestrator.agent_move.remote_client import AgentMoveRemoteError
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime


def _runtime(tmp_path):
    sent = []
    return SimpleNamespace(
        _transfer_state=None,
        _suppressed_transfer_results=[],
        transfer_state_path=tmp_path / "active_transfer.json",
        config=SimpleNamespace(active_backend="codex-cli"),
        _parse_request_seq=lambda request_id: int(request_id.split("-", 1)[1]) if request_id and request_id.startswith("req-") else None,
        _detect_instance_name=lambda: "HASHI1",
        _normalize_instance_name=lambda value: str(value or "").upper(),
        _load_instances=lambda: {},
        _persist_transfer_state=None,
        global_config=SimpleNamespace(instance_id="HASHI1", workbench_port=8765),
        orchestrator=SimpleNamespace(
            resolve_service_endpoint=lambda service, expected_instance=None: {
                "service": service,
                "instance_id": expected_instance,
                "host": "172.29.144.7",
                "port": 8765,
                "base_url": "http://172.29.144.7:8765",
            }
        ),
        handoff_builder=None,
        name="zelda",
        workspace_dir=tmp_path / "workspace",
        transcript_log_path=tmp_path / "workspace" / "transcript.jsonl",
        get_runtime_metadata=lambda: {"name": "zelda"},
        send_long_message=lambda **kwargs: _send(sent, kwargs),
        sent_messages=sent,
    )


def _remote_target(instance="HASHI2", host="10.0.0.2", port=9001):
    return {
        instance.casefold(): {
            "instance_id": instance,
            "api_host": host,
            "remote_port": port,
        }
    }


async def _send(sent, kwargs):
    sent.append(kwargs)


def _item(**overrides):
    values = {
        "request_id": "req-1",
        "chat_id": 123,
        "summary": "Summary",
        "source": "text",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_persist_and_clear_transfer_state(tmp_path):
    runtime = _runtime(tmp_path)
    runtime._persist_transfer_state = lambda: runtime_transfer.persist_transfer_state(runtime)
    runtime._transfer_state = {"status": "pending", "transfer_id": "trf-1"}

    runtime_transfer.persist_transfer_state(runtime)
    assert json.loads(runtime.transfer_state_path.read_text(encoding="utf-8"))["transfer_id"] == "trf-1"

    runtime_transfer.clear_transfer_state(runtime)
    assert runtime._transfer_state is None
    assert runtime._suppressed_transfer_results == []
    assert not runtime.transfer_state_path.exists()


def test_record_transfer_accepted_preserves_identity_and_persists_fence(tmp_path):
    runtime = _runtime(tmp_path)
    runtime._persist_transfer_state = lambda: runtime_transfer.persist_transfer_state(
        runtime
    )
    runtime._transfer_state = {
        "status": "pending",
        "transfer_id": "trf-accepted",
        "target_agent": "akane",
        "target_instance": "HASHI2",
    }

    runtime_transfer.record_transfer_accepted(
        runtime,
        transfer_id="trf-accepted",
        target_status="accepted_but_chat_offline",
    )

    assert runtime_transfer.transfer_redirect_snapshot(runtime) == {
        "status": "accepted",
        "transfer_id": "trf-accepted",
        "target_agent": "akane",
        "target_instance": "HASHI2",
    }
    persisted = json.loads(runtime.transfer_state_path.read_text(encoding="utf-8"))
    assert persisted["target_status"] == "accepted_but_chat_offline"
    with pytest.raises(ValueError, match="identity changed"):
        runtime_transfer.record_transfer_accepted(
            runtime,
            transfer_id="trf-other",
            target_status="accepted",
        )


def test_transfer_redirect_and_buffer_rules(tmp_path):
    runtime = _runtime(tmp_path)
    runtime._transfer_state = {
        "status": "accepted",
        "target_agent": "akane",
        "target_instance": "HASHI2",
        "transfer_id": "trf-abc",
        "cutoff_seq": 7,
    }

    assert runtime_transfer.has_active_transfer(runtime) is True
    assert runtime_transfer.should_redirect_after_transfer(runtime) is True
    assert runtime_transfer.should_buffer_during_transfer(runtime, "req-7") is True
    assert runtime_transfer.should_buffer_during_transfer(runtime, "req-8") is False
    assert "akane@HASHI2" in runtime_transfer.transfer_redirect_text(runtime)


def test_transfer_redirect_snapshot_supports_worker_metadata_and_remote_error():
    snapshot = {
        "status": "accepted",
        "target_agent": "akane",
        "target_instance": "HASHI2",
        "transfer_id": "trf-metadata",
    }
    handle = SimpleNamespace(metadata={"transfer_redirect": snapshot})

    assert runtime_transfer.transfer_redirect_snapshot(handle) == snapshot
    direct = runtime_transfer.TransferRedirectRequired(snapshot)
    remote = SimpleNamespace(
        error={"type": "TransferRedirectRequired", "message": str(direct)}
    )
    assert runtime_transfer.transfer_redirect_from_exception(remote) == snapshot

    unknown_raw = {
        "status": "pending",
        "outcome_unknown": True,
        "target_agent": "akane",
        "target_instance": "HASHI2",
        "transfer_id": "trf-unknown-rpc",
    }
    unknown_snapshot = runtime_transfer.transfer_redirect_snapshot(
        SimpleNamespace(_transfer_state=unknown_raw)
    )
    assert unknown_snapshot == {
        "status": "unknown",
        "target_agent": "akane",
        "target_instance": "HASHI2",
        "transfer_id": "trf-unknown-rpc",
    }
    unknown_handle = SimpleNamespace(metadata={"transfer_redirect": unknown_snapshot})
    assert runtime_transfer.transfer_redirect_snapshot(unknown_handle) == unknown_snapshot
    unknown_direct = runtime_transfer.TransferRedirectRequired(unknown_snapshot)
    unknown_remote = SimpleNamespace(
        error={"type": "TransferRedirectRequired", "message": str(unknown_direct)}
    )
    assert runtime_transfer.transfer_redirect_from_exception(unknown_remote) == (
        unknown_snapshot
    )


def test_authoritative_transfer_redirect_snapshot_reads_workspace_fence(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_path = workspace / "active_transfer.json"
    state_path.write_text(
        json.dumps(
            {
                "status": "pending",
                "outcome_unknown": True,
                "transfer_id": "trf-workspace-unknown",
                "target_agent": "akane",
                "target_instance": "HASHI2",
            }
        ),
        encoding="utf-8",
    )
    runtime = SimpleNamespace(metadata={}, workspace_dir=workspace)

    assert runtime_transfer.authoritative_transfer_redirect_snapshot(runtime) == {
        "status": "unknown",
        "transfer_id": "trf-workspace-unknown",
        "target_agent": "akane",
        "target_instance": "HASHI2",
    }


def test_authoritative_transfer_redirect_snapshot_preserves_pending_policy(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "active_transfer.json").write_text(
        json.dumps(
            {
                "status": "pending",
                "transfer_id": "trf-still-pending",
                "target_agent": "akane",
                "target_instance": "HASHI2",
            }
        ),
        encoding="utf-8",
    )

    assert runtime_transfer.authoritative_transfer_redirect_snapshot(
        SimpleNamespace(metadata={}, workspace_dir=workspace)
    ) is None
    assert runtime_transfer.authoritative_transfer_redirect_snapshot(
        SimpleNamespace(metadata={}, workspace_dir=tmp_path / "no-fence")
    ) is None


@pytest.mark.parametrize(
    ("parts", "requires_fence"),
    [
        (
            [{"type": "text", "item_index": 1, "text": "ordinary message"}],
            True,
        ),
        (
            [
                {
                    "type": "media",
                    "item_index": 1,
                    "attachment_id": "att_voice",
                    "modality": "audio",
                    "semantic_role": "voice_message",
                    "mime_type": "audio/wav",
                    "local_ref": "attachments/att_voice.wav",
                    "size_bytes": 44,
                    "sha256": "a" * 64,
                    "transport": {},
                },
                {"type": "text", "item_index": 2, "text": "Optional caption"},
            ],
            False,
        ),
        (
            [
                {
                    "type": "media",
                    "item_index": 1,
                    "attachment_id": "att_voice",
                    "modality": "audio",
                    "semantic_role": "voice_message",
                    "mime_type": "audio/wav",
                    "local_ref": "attachments/att_voice.wav",
                    "size_bytes": 44,
                    "sha256": "a" * 64,
                    "transport": {},
                },
                {
                    "type": "media",
                    "item_index": 2,
                    "attachment_id": "att_document",
                    "modality": "document",
                    "mime_type": "application/pdf",
                    "local_ref": "attachments/att_document.pdf",
                    "size_bytes": 128,
                    "sha256": "b" * 64,
                    "transport": {},
                },
            ],
            True,
        ),
    ],
)
def test_session_request_transfer_fence_classifies_canonical_attachments(
    parts, requires_fence
):
    assert (
        runtime_transfer.session_request_requires_transfer_fence(
            {"version": 1, "parts": parts}
        )
        is requires_fence
    )


@pytest.mark.parametrize(
    "contents",
    [
        "{",
        "[]",
        "{}",
        '{"status":"garbage"}',
        '{"status":"accepted","target_agent":"akane","target_instance":"HASHI2"}',
        (
            '{"status":"pending","outcome_unknown":"true",'
            '"transfer_id":"trf-bad-bool","target_agent":"akane",'
            '"target_instance":"HASHI2"}'
        ),
    ],
)
def test_authoritative_transfer_redirect_snapshot_fails_closed_on_invalid_fence(
    tmp_path, contents
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "active_transfer.json").write_text(contents, encoding="utf-8")

    assert runtime_transfer.authoritative_transfer_redirect_snapshot(
        SimpleNamespace(
            name="zelda",
            metadata={"active_transfer": True},
            workspace_dir=workspace,
        )
    ) == {
        "status": "unknown",
        "transfer_id": "unknown",
        "target_agent": "zelda",
        "target_instance": "unknown",
    }


@pytest.mark.asyncio
async def test_nonvoice_api_ingress_fails_before_primary_telegram_redirect(tmp_path):
    sent = []
    runtime = SimpleNamespace(
        _transfer_state={
            "status": "accepted",
            "target_agent": "akane",
            "target_instance": "HASHI2",
            "transfer_id": "trf-no-telegram",
        },
        _should_redirect_after_transfer=lambda: True,
        send_long_message=lambda *args, **kwargs: sent.append((args, kwargs)),
    )

    with pytest.raises(runtime_transfer.TransferRedirectRequired):
        await FlexibleAgentRuntime.enqueue_api_text(runtime, "hello")
    with pytest.raises(runtime_transfer.TransferRedirectRequired):
        await FlexibleAgentRuntime.enqueue_api_media(
            runtime,
            local_path=tmp_path / "never-read.png",
            media_kind="photo",
            filename="never-read.png",
        )

    assert sent == []


@pytest.mark.asyncio
async def test_unknown_transfer_outcome_fails_closed_before_nonvoice_ingress(tmp_path):
    runtime = SimpleNamespace(
        _transfer_state={
            "status": "pending",
            "outcome_unknown": True,
            "target_agent": "akane",
            "target_instance": "HASHI2",
            "transfer_id": "trf-unknown",
        },
        _should_redirect_after_transfer=lambda: True,
    )

    with pytest.raises(runtime_transfer.TransferRedirectRequired) as text_error:
        await FlexibleAgentRuntime.enqueue_api_text(runtime, "must not enter a Run")
    with pytest.raises(runtime_transfer.TransferRedirectRequired) as media_error:
        await FlexibleAgentRuntime.enqueue_api_media(
            runtime,
            local_path=tmp_path / "never-read.png",
            media_kind="photo",
            filename="never-read.png",
        )

    assert text_error.value.redirect["status"] == "unknown"
    assert media_error.value.redirect["status"] == "unknown"


@pytest.mark.asyncio
async def test_record_and_flush_suppressed_transfer_results(tmp_path):
    runtime = _runtime(tmp_path)

    runtime_transfer.record_suppressed_transfer_result(runtime, _item(), success=True, text="visible")
    runtime_transfer.record_suppressed_transfer_result(
        runtime,
        _item(request_id="req-2", chat_id=456),
        success=False,
        error="boom",
    )

    await runtime_transfer.flush_suppressed_transfer_results(runtime)

    assert runtime._suppressed_transfer_results == []
    assert runtime.sent_messages == [
        {"chat_id": 123, "text": "visible", "request_id": "req-1", "purpose": "transfer-release"},
        {
            "chat_id": 456,
            "text": "Flex Backend Error (codex-cli): Error details: boom",
            "request_id": "req-2",
            "purpose": "transfer-release",
        },
    ]


def test_strip_transfer_accept_prefix_only_for_matching_transfer_source():
    item = _item(source="bridge-transfer:trf-1")

    assert runtime_transfer.strip_transfer_accept_prefix(item, "TRANSFER_ACCEPTED trf-1\nContinue") == "Continue"
    assert runtime_transfer.strip_transfer_accept_prefix(item, "No prefix") == "No prefix"
    assert runtime_transfer.strip_transfer_accept_prefix(_item(source="text"), "TRANSFER_ACCEPTED trf-1\nContinue") == (
        "TRANSFER_ACCEPTED trf-1\nContinue"
    )


def test_resolve_bridge_handoff_endpoint_handles_local_and_remote(tmp_path):
    runtime = _runtime(tmp_path)

    assert runtime_transfer.resolve_bridge_handoff_endpoint(runtime, "hashi1", "transfer") == (
        "HASHI1",
        "http://172.29.144.7:8765/api/bridge/transfer",
    )

    runtime._load_instances = lambda: {
        "hashi2": {
            "instance_id": "HASHI2",
            "api_host": "10.0.0.2",
            "workbench_port": 9000,
            "remote_port": 9001,
        },
    }
    assert runtime_transfer.resolve_bridge_handoff_endpoint(runtime, "HASHI2", "fork") == (
        "HASHI2",
        "http://10.0.0.2:9001/workbench/v1/proxy/api/bridge/fork",
    )
    assert runtime_transfer.handoff_health_endpoint(
        "http://10.0.0.2:9001/workbench/v1/proxy/api/bridge/fork"
    ) == "http://10.0.0.2:9001/workbench/v1/status"


def test_resolve_bridge_handoff_rejects_cross_instance_endpoint_alias(tmp_path):
    runtime = _runtime(tmp_path)
    runtime._load_instances = lambda: {
        "hashi2": {
            "instance_id": "HASHI2",
            "api_host": "172.29.144.7",
            "workbench_port": 8765,
            "remote_port": 8765,
        },
    }

    with pytest.raises(ValueError, match="local Workbench endpoint"):
        runtime_transfer.resolve_bridge_handoff_endpoint(
            runtime,
            "HASHI2",
            "transfer",
        )


def test_handoff_health_identity_must_match_target_instance():
    runtime_transfer.verify_handoff_instance_identity(
        {"instance_id": "HASHI3"},
        expected_instance="hashi3",
    )

    with pytest.raises(ValueError, match="identity check failed"):
        runtime_transfer.verify_handoff_instance_identity(
            {"instance_id": "HASHI1"},
            expected_instance="HASHI3",
        )


def test_remote_bridge_handoff_authenticates_status_identity_and_posts_once(
    tmp_path, monkeypatch
):
    runtime = _runtime(tmp_path)
    runtime.global_config.bridge_home = tmp_path
    runtime._load_instances = _remote_target
    (tmp_path / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": "shared-secret"}),
        encoding="utf-8",
    )
    endpoint = "http://10.0.0.2:9001/workbench/v1/proxy/api/bridge/transfer"
    package = {"transfer_id": "trf-exact", "target_agent": "akane"}
    calls = []

    def request(url, **kwargs):
        calls.append((url, kwargs))
        if kwargs["method"] == "GET":
            return {
                "ok": True,
                "gateway": "workbench_v1",
                "authenticated_instance": "HASHI1",
                "instance": {"instance_id": "HASHI2"},
                "workbench_online": True,
                "workbench_health": {"ok": True, "instance_id": "HASHI2"},
            }
        return {"ok": True, "status": "accepted"}

    monkeypatch.setattr(runtime_transfer, "request_authenticated_json", request)

    result = runtime_transfer._request_remote_handoff(
        runtime,
        endpoint=endpoint,
        expected_target="HASHI2",
        payload=package,
    )

    assert result == {"ok": True, "status": "accepted"}
    assert calls == [
        (
            "http://10.0.0.2:9001/workbench/v1/status",
            {
                "method": "GET",
                "shared_token": "shared-secret",
                "from_instance": "HASHI1",
                "timeout": 10,
            },
        ),
        (
            endpoint,
            {
                "method": "POST",
                "payload": package,
                "shared_token": "shared-secret",
                "from_instance": "HASHI1",
                "timeout": 3600,
            },
        ),
    ]


def test_remote_bridge_handoff_selects_reachable_explicit_target_before_post(
    tmp_path, monkeypatch
):
    runtime = _runtime(tmp_path)
    runtime.global_config.bridge_home = tmp_path
    runtime._load_instances = lambda: {
        "hashi2": {
            "instance_id": "HASHI2",
            "remote_port": 9001,
            "address_candidates": [
                {"host": "127.0.0.1"},
                {"host": "10.0.0.2"},
            ],
        }
    }
    (tmp_path / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": "shared-secret"}),
        encoding="utf-8",
    )
    calls = []

    def request(url, **kwargs):
        calls.append((url, kwargs["method"]))
        if url.startswith("http://127.0.0.1:"):
            try:
                raise URLError(ConnectionRefusedError(111, "connection refused"))
            except URLError as exc:
                raise AgentMoveRemoteError(f"receiver request failed: {exc}") from exc
        if kwargs["method"] == "GET":
            return {
                "ok": True,
                "gateway": "workbench_v1",
                "authenticated_instance": "HASHI1",
                "instance": {"instance_id": "HASHI2"},
                "workbench_online": True,
                "workbench_health": {"ok": True, "instance_id": "HASHI2"},
            }
        return {"ok": True, "status": "accepted"}

    monkeypatch.setattr(runtime_transfer, "request_authenticated_json", request)

    result = runtime_transfer._request_remote_handoff(
        runtime,
        endpoint="http://127.0.0.1:9001/workbench/v1/proxy/api/bridge/transfer",
        expected_target="HASHI2",
        payload={"transfer_id": "trf-select"},
    )

    assert result == {"ok": True, "status": "accepted"}
    assert calls == [
        ("http://127.0.0.1:9001/workbench/v1/status", "GET"),
        ("http://10.0.0.2:9001/workbench/v1/status", "GET"),
        (
            "http://10.0.0.2:9001/workbench/v1/proxy/api/bridge/transfer",
            "POST",
        ),
    ]


def test_remote_bridge_handoff_wrong_identity_does_not_try_later_candidate(
    tmp_path, monkeypatch
):
    runtime = _runtime(tmp_path)
    runtime.global_config.bridge_home = tmp_path
    runtime._load_instances = lambda: {
        "hashi2": {
            "instance_id": "HASHI2",
            "remote_port": 9001,
            "address_candidates": [
                {"host": "127.0.0.1"},
                {"host": "10.0.0.2"},
            ],
        }
    }
    (tmp_path / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": "shared-secret"}),
        encoding="utf-8",
    )
    calls = []

    def request(url, **kwargs):
        calls.append((url, kwargs["method"]))
        return {
            "ok": True,
            "gateway": "workbench_v1",
            "authenticated_instance": "HASHI1",
            "instance": {"instance_id": "HASHI4"},
            "workbench_online": True,
            "workbench_health": {"ok": True, "instance_id": "HASHI4"},
        }

    monkeypatch.setattr(runtime_transfer, "request_authenticated_json", request)

    with pytest.raises(ValueError, match="identity check failed"):
        runtime_transfer._request_remote_handoff(
            runtime,
            endpoint="http://127.0.0.1:9001/workbench/v1/proxy/api/bridge/transfer",
            expected_target="HASHI2",
            payload={"transfer_id": "trf-wrong-candidate"},
        )

    assert calls == [("http://127.0.0.1:9001/workbench/v1/status", "GET")]


def test_remote_bridge_handoff_auth_failure_does_not_try_later_candidate(
    tmp_path, monkeypatch
):
    runtime = _runtime(tmp_path)
    runtime.global_config.bridge_home = tmp_path
    runtime._load_instances = lambda: {
        "hashi2": {
            "instance_id": "HASHI2",
            "remote_port": 9001,
            "address_candidates": [
                {"host": "127.0.0.1"},
                {"host": "10.0.0.2"},
            ],
        }
    }
    (tmp_path / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": "shared-secret"}),
        encoding="utf-8",
    )
    calls = []

    def request(url, **kwargs):
        calls.append((url, kwargs["method"]))
        raise AgentMoveRemoteError("receiver response authentication failed")

    monkeypatch.setattr(runtime_transfer, "request_authenticated_json", request)

    with pytest.raises(AgentMoveRemoteError, match="authentication failed"):
        runtime_transfer._request_remote_handoff(
            runtime,
            endpoint="http://127.0.0.1:9001/workbench/v1/proxy/api/bridge/transfer",
            expected_target="HASHI2",
            payload={"transfer_id": "trf-auth-failed"},
        )

    assert calls == [("http://127.0.0.1:9001/workbench/v1/status", "GET")]


def test_remote_bridge_handoff_rejects_wrong_identity_before_post(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    runtime.global_config.bridge_home = tmp_path
    runtime._load_instances = _remote_target
    (tmp_path / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": "shared-secret"}),
        encoding="utf-8",
    )
    calls = []

    def request(url, **kwargs):
        calls.append((url, kwargs["method"]))
        return {
            "ok": True,
            "gateway": "workbench_v1",
            "authenticated_instance": "HASHI1",
            "instance": {"instance_id": "HASHI4"},
            "workbench_online": True,
            "workbench_health": {"ok": True, "instance_id": "HASHI4"},
        }

    monkeypatch.setattr(runtime_transfer, "request_authenticated_json", request)

    with pytest.raises(ValueError, match="identity check failed"):
        runtime_transfer._request_remote_handoff(
            runtime,
            endpoint="http://10.0.0.2:9001/workbench/v1/proxy/api/bridge/transfer",
            expected_target="HASHI2",
            payload={"transfer_id": "trf-wrong"},
        )

    assert calls == [("http://10.0.0.2:9001/workbench/v1/status", "GET")]


def test_remote_bridge_handoff_post_error_is_unknown_and_never_replayed(
    tmp_path, monkeypatch
):
    runtime = _runtime(tmp_path)
    runtime.global_config.bridge_home = tmp_path
    runtime._load_instances = _remote_target
    (tmp_path / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": "shared-secret"}),
        encoding="utf-8",
    )
    calls = []

    def request(url, **kwargs):
        calls.append((url, kwargs["method"]))
        if kwargs["method"] == "GET":
            return {
                "ok": True,
                "gateway": "workbench_v1",
                "authenticated_instance": "HASHI1",
                "instance": {"instance_id": "HASHI2"},
                "workbench_online": True,
                "workbench_health": {"ok": True, "instance_id": "HASHI2"},
            }
        raise TimeoutError("signed response was lost")

    monkeypatch.setattr(runtime_transfer, "request_authenticated_json", request)

    with pytest.raises(runtime_transfer.BridgeHandoffOutcomeUnknown) as raised:
        runtime_transfer._request_remote_handoff(
            runtime,
            endpoint="http://10.0.0.2:9001/workbench/v1/proxy/api/bridge/transfer",
            expected_target="HASHI2",
            payload={"transfer_id": "trf-unknown"},
        )

    assert raised.value.transfer_id == "trf-unknown"
    assert calls == [
        ("http://10.0.0.2:9001/workbench/v1/status", "GET"),
        (
            "http://10.0.0.2:9001/workbench/v1/proxy/api/bridge/transfer",
            "POST",
        ),
    ]


def test_record_unknown_outcome_preserves_pending_transfer_fence(tmp_path):
    runtime = _runtime(tmp_path)
    runtime._persist_transfer_state = lambda: runtime_transfer.persist_transfer_state(
        runtime
    )
    runtime._transfer_state = {
        "status": "pending",
        "transfer_id": "trf-unknown",
        "target_agent": "akane",
        "target_instance": "HASHI2",
        "cutoff_seq": 9,
    }

    runtime_transfer.record_transfer_outcome_unknown(
        runtime,
        transfer_id="trf-unknown",
        error=TimeoutError("reply lost"),
    )

    assert runtime._transfer_state["status"] == "pending"
    assert runtime._transfer_state["outcome_unknown"] is True
    assert runtime._transfer_state["transfer_id"] == "trf-unknown"
    assert runtime_transfer.has_active_transfer(runtime) is True
    assert runtime_transfer.transfer_redirect_snapshot(runtime)["status"] == "unknown"
    with ui_language.language_scope(runtime, locale="zh-CN"):
        assert "结果未知" in runtime_transfer.transfer_redirect_text(runtime)
    persisted = json.loads(runtime.transfer_state_path.read_text(encoding="utf-8"))
    assert persisted["outcome_unknown"] is True


def test_build_handoff_payload_adds_runtime_metadata(tmp_path):
    runtime = _runtime(tmp_path)

    class _HandoffBuilder:
        def build_transfer_package(self, **kwargs):
            return {**kwargs, "exchange_count": 2}

    runtime.handoff_builder = _HandoffBuilder()

    package = runtime_transfer.build_handoff_payload(runtime, "akane", "HASHI2", "transfer")

    assert package["transfer_id"].startswith("trf-")
    assert package["source_agent"] == "zelda"
    assert package["source_instance"] == "HASHI1"
    assert package["target_agent"] == "akane"
    assert package["mode"] == "transfer"
    assert package["source_runtime"] == {"name": "zelda"}
    assert package["source_workspace_dir"].endswith("workspace")
    assert package["source_transcript_path"].endswith("transcript.jsonl")
    assert package["max_rounds"] == 10
    assert package["max_words"] == 6000
