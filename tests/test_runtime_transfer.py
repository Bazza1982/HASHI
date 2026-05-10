from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.runtime_common import QueuedRequest
from orchestrator import runtime_transfer


def _runtime(tmp_path: Path):
    package_calls = []

    def build_transfer_package(**kwargs):
        package_calls.append(kwargs)
        return {"transfer_id": kwargs["transfer_id"]}

    runtime = SimpleNamespace()
    runtime._transfer_state = {"status": "accepted", "target_agent": "sunny", "target_instance": "HASHI1", "transfer_id": "trf-123", "cutoff_seq": 5}
    runtime._suppressed_transfer_results = []
    runtime.transfer_state_path = tmp_path / "transfer_state.json"
    runtime._parse_request_seq = lambda request_id: int(str(request_id).split("-")[1]) if request_id else None
    runtime.config = SimpleNamespace(active_backend="codex-cli")
    runtime.sent = []

    async def send_long_message(**kwargs):
        runtime.sent.append(kwargs)

    runtime.send_long_message = send_long_message
    runtime._normalize_instance_name = lambda value: "HASHI2" if str(value).lower() in {"hashi2", "2", ""} else str(value).upper()
    runtime._detect_instance_name = lambda: "HASHI2"
    runtime.global_config = SimpleNamespace(workbench_port=8765)
    runtime._load_instances = lambda: {"HASHI1": {"api_host": "10.0.0.5", "workbench_port": 18865}}
    runtime.handoff_builder = SimpleNamespace(build_transfer_package=build_transfer_package)
    runtime.name = "lin_yueru"
    runtime.get_runtime_metadata = lambda: {"mode": "flex"}
    runtime.workspace_dir = Path("/tmp/hashi2/workspace")
    runtime.transcript_log_path = Path("/tmp/hashi2/workspace/transcript.jsonl")
    return runtime, package_calls


def test_transfer_state_helpers_cover_redirect_and_buffering(tmp_path: Path):
    runtime, _ = _runtime(tmp_path)
    assert runtime_transfer.has_active_transfer(runtime) is True
    assert "sunny@HASHI1" in runtime_transfer.transfer_redirect_text(runtime)
    assert runtime_transfer.should_redirect_after_transfer(runtime) is True
    assert runtime_transfer.should_buffer_during_transfer(runtime, "req-4") is True
    assert runtime_transfer.should_buffer_during_transfer(runtime, "req-8") is False


def test_strip_transfer_accept_prefix_only_for_transfer_items():
    item = QueuedRequest(
        request_id="req-1",
        chat_id=1,
        prompt="p",
        source="bridge-transfer:abc",
        summary="s",
        created_at="2026-05-10T00:00:00+10:00",
    )
    assert runtime_transfer.strip_transfer_accept_prefix(item, "TRANSFER_ACCEPTED abc\nhello") == "hello"


def test_resolve_bridge_handoff_endpoint_and_payload(tmp_path: Path):
    runtime, package_calls = _runtime(tmp_path)
    assert runtime_transfer.resolve_bridge_handoff_endpoint(runtime, "HASHI2", "transfer") == (
        "HASHI2",
        "http://127.0.0.1:8765/api/bridge/transfer",
    )
    assert runtime_transfer.resolve_bridge_handoff_endpoint(runtime, "HASHI1", "fork") == (
        "HASHI1",
        "http://10.0.0.5:18865/api/bridge/fork",
    )

    payload = runtime_transfer.build_handoff_payload(runtime, "sunny", "HASHI1", "fork")
    assert payload["mode"] == "fork"
    assert payload["source_runtime"] == {"mode": "flex"}
    assert package_calls[0]["target_agent"] == "sunny"


@pytest.mark.asyncio
async def test_flush_suppressed_transfer_results(tmp_path: Path):
    runtime, _ = _runtime(tmp_path)
    runtime._suppressed_transfer_results = [
        {"request_id": "req-1", "chat_id": 9, "success": True, "text": "done", "error": None},
        {"request_id": "req-2", "chat_id": 9, "success": False, "text": None, "error": "boom"},
    ]
    await runtime_transfer.flush_suppressed_transfer_results(runtime)
    assert runtime.sent[0]["text"] == "done"
    assert "boom" in runtime.sent[1]["text"]
