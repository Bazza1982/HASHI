"""Private provider evidence retains the real canonical owner across LPAC."""
import asyncio
from types import SimpleNamespace

import pytest

from adapters.openrouter_api import _APIResult, ProviderProtocolForensicError
from orchestrator.canonical_audit import CanonicalAuditStore
from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.host_provider import BrokeredProviderFactory
from orchestrator.permissions.host_protocol import pack, unpack
from orchestrator.permissions.runtime_ports import invoke


@pytest.fixture
def owner(tmp_path):
    store = CanonicalAuditStore(tmp_path / "worker", instance_id="qualification", agent_id="agent")
    binding = SimpleNamespace(request_id="canonical-request", context=SimpleNamespace(
        turn_id="canonical-turn", execution_epoch="canonical-epoch"))
    turn = SimpleNamespace(
        binding=binding, runtime=SimpleNamespace(),
        adapter=SimpleNamespace(config=SimpleNamespace(_hashi_runtime=SimpleNamespace(canonical_audit=store))),
    )
    class Connection:
        def call(self, method, arguments):
            assert method == "runtime_port"
            return asyncio.run(invoke(turn, **arguments))
    host = tmp_path / "host"
    host.mkdir()
    factory = BrokeredProviderFactory(Connection(), profile_config=[
        {"engine": "deepseek-api", "model": "deepseek-v4-pro"}],
        scratch_root=host, privacy_level=1, brokered_forensics=True)
    backend = factory.create_ephemeral_backend("deepseek-api", "deepseek-v4-pro")
    return store, turn, host, backend


def events(store):
    return store.read_events({"actor": "qualification", "purpose": "verify private persistence",
                              "allow_raw_audit": True})


def test_wire_evidence_is_durable_under_the_original_owner_not_the_host(owner):
    store, turn, host, backend = owner
    reference = backend._record_provider_wire_evidence(
        "request", request_id="physical-provider-request", call_serial=1,
        payload={"messages": [{"role": "user", "content": "private-wire-sentinel"}]})
    records = events(store)
    assert reference == "canonical-audit:" + records[0]["event_id"]
    assert records[0]["request_id"] == turn.binding.request_id
    assert records[0]["provenance"]["execution_epoch"] == "canonical-epoch"
    assert records[0]["payload"]["payload"]["messages"][0]["content"] == "private-wire-sentinel"
    assert not (host / "logs").exists()


def test_bad_tool_forensics_keeps_complete_evidence_and_a_canonical_reference(owner):
    store, _, host, backend = owner
    reference, details = backend._write_invalid_tool_call_forensic(
        path=None, request_id="physical-provider-request", call_serial=1,
        payload={"messages": []},
        result=_APIResult("", [{"id": "bad", "type": "function",
            "function": {"name": "file_write", "arguments": '{"broken"'}}], "tool_calls"),
        provider_call_record={}, repair_response_number=1, next_repair_number=2, incident_number=1)
    records = events(store)
    assert str(reference) == "canonical-audit:" + records[0]["event_id"]
    assert records[0]["event_type"] == "provider_tool_forensic"
    assert records[0]["payload"]["raw_provider"]["response"]["tool_calls"][0]["id"] == "bad"
    assert details and not (host / "logs").exists()


def test_audit_failure_stops_without_local_fallback_or_blind_retry(owner):
    store, _, host, backend = owner
    attempts = []
    def failed(*args, **kwargs):
        attempts.append(1)
        raise OSError("canonical durability failure")
    store.record = failed
    with pytest.raises(ProviderProtocolForensicError):
        backend._record_provider_wire_evidence("request", request_id="physical", call_serial=1, payload={})
    assert attempts == [1]
    assert not (host / "logs").exists()


@pytest.mark.asyncio
async def test_the_private_port_cannot_choose_an_audit_namespace_or_a_file(owner):
    store, turn, host, _ = owner
    before = events(store)
    for kind in ("approve", "arbitrary_file"):
        with pytest.raises(PermissionDenied):
            await invoke(turn, "provider_forensic", {
                "args": [], "kwargs": {"kind": pack(kind), "record": pack({"path": str(host / "escape")})}})
    assert events(store) == before and not (host / "escape").exists()


def test_buffered_owner_is_flushed_before_the_host_receives_an_acknowledgement(owner):
    from orchestrator.canonical_audit import BufferedCanonicalAuditWriter

    store, turn, _, backend = owner
    writer = BufferedCanonicalAuditWriter(store, coalesce_window_s=10)
    turn.adapter.config._hashi_runtime.canonical_audit = writer
    try:
        reference = backend._record_provider_wire_evidence(
            "request", request_id="physical", call_serial=1, payload={"messages": []})
        records = events(store)
        assert len(records) == 1
        assert reference == "canonical-audit:" + records[0]["event_id"]
    finally:
        writer.close()


def test_missing_canonical_owner_is_not_replaced_with_host_local_storage(owner):
    _, turn, host, backend = owner
    turn.adapter.config._hashi_runtime.canonical_audit = None
    with pytest.raises(ProviderProtocolForensicError):
        backend._record_provider_wire_evidence("request", request_id="physical", call_serial=1, payload={})
    assert not (host / "logs").exists()
# HASHI-Fingerprint: hfp_845fa7fb9d944c17965d76fbc1a69717
