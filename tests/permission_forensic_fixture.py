"""Real canonical persistence for tests whose transport is injected."""
from types import SimpleNamespace

from orchestrator.canonical_audit import CanonicalAuditStore
from orchestrator.permissions.host_protocol import pack, unpack
from orchestrator.permissions.provider_forensics import persist_provider_forensic_sync


def forensic_port(root, binding):
    store = CanonicalAuditStore(root, instance_id="permission-test", agent_id="agent")
    turn = SimpleNamespace(binding=binding,
        adapter=SimpleNamespace(config=SimpleNamespace(_hashi_runtime=SimpleNamespace(canonical_audit=store))))
    def call(arguments):
        assert arguments["operation"] == "provider_forensic"
        values = arguments["arguments"]
        assert values["args"] == [] and set(values["kwargs"]) == {"kind", "record"}
        return pack(persist_provider_forensic_sync(turn, **{
            key: unpack(value) for key, value in values["kwargs"].items()}))
    return call, store
# HASHI-Fingerprint: hfp_3a936e941ccd41279ab23363484a90c3
