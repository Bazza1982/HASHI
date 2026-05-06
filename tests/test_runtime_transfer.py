import json
from types import SimpleNamespace

import pytest

from orchestrator import runtime_transfer


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
        global_config=SimpleNamespace(workbench_port=8765),
        handoff_builder=None,
        name="zelda",
        workspace_dir=tmp_path / "workspace",
        transcript_log_path=tmp_path / "workspace" / "transcript.jsonl",
        get_runtime_metadata=lambda: {"name": "zelda"},
        send_long_message=lambda **kwargs: _send(sent, kwargs),
        sent_messages=sent,
    )


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
            "text": "Flex Backend Error (codex-cli): boom",
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
        "http://127.0.0.1:8765/api/bridge/transfer",
    )

    runtime._load_instances = lambda: {
        "hashi2": {"api_host": "10.0.0.2", "workbench_port": 9000},
    }
    assert runtime_transfer.resolve_bridge_handoff_endpoint(runtime, "HASHI2", "fork") == (
        "HASHI2",
        "http://10.0.0.2:9000/api/bridge/fork",
    )


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
