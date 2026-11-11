"""The ordinary managed path keeps its existing local forensic behavior."""
from orchestrator.permissions.host_provider import BrokeredProviderFactory


def test_ordinary_managed_adapter_does_not_require_the_lpac_forensic_port(tmp_path):
    class OrdinaryClient:
        pass
    factory = BrokeredProviderFactory(OrdinaryClient(), profile_config=[
        {"engine": "deepseek-api", "model": "deepseek-v4-pro"}],
        scratch_root=tmp_path, privacy_level=1)
    backend = factory.create_ephemeral_backend("deepseek-api", "deepseek-v4-pro")
    reference = backend._record_provider_wire_evidence(
        "request", request_id="ordinary", call_serial=1, payload={"messages": []})
    assert reference.startswith("provider-wire:")
    assert (tmp_path / "logs/provider_protocol_forensics/provider-wire.jsonl").is_file()
# HASHI-Fingerprint: hfp_d9c8eb3928cf4e2fa6fda5f28ac6de1f
