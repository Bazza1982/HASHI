"""The fixed LPAC entry must keep runtime evidence with the original owner."""
from types import SimpleNamespace

import pytest

from adapters.openrouter_api import _APIResult
from orchestrator.her_v2.models import TerminalState
from orchestrator.permissions.host_protocol import pack, unpack
from orchestrator.permissions.model_broker import api_result_to_wire
from orchestrator.permissions.restricted_her_host import run_command
from tests.permission_forensic_fixture import forensic_port
from tests.test_permission_restricted_her import command


@pytest.mark.asyncio
async def test_required_owner_failure_never_continues_using_a_host_local_audit_spool(tmp_path):
    forensic, _ = forensic_port(tmp_path / "owner-forensics", SimpleNamespace(
        request_id="request", context=SimpleNamespace(turn_id="turn", execution_epoch="fixed-host")))
    calls = []
    class Connection:
        def call(self, method, arguments):
            if method == "runtime_port":
                if arguments["operation"] == "provider_forensic":
                    return forensic(arguments)
                assert arguments["operation"] == "manifest"
                return pack({"operations": [], "allow_immediate_response": False, "workzone_ref": str(tmp_path)})
            if method == "provider_checkpoint":
                return {}
            assert method == "audit_record"
            raise OSError("original audit owner unavailable")
        async def call_async(self, method, arguments, **kwargs):
            assert method == "provider_call"
            calls.append(arguments)
            return api_result_to_wire(_APIResult("Should not be reached.", None, "stop"))
    async def emit(event):
        pass
    host = tmp_path / "host"
    host.mkdir()
    payload = command(SimpleNamespace(get_tool_definitions=lambda: []))
    response = await run_command(payload, client=Connection(), scratch_root=host, emit=emit,
                                 brokered_forensics=True)
    result = unpack(response["result"])
    assert result.terminal_state is TerminalState.ERROR
    assert "AUDIT_PERSISTENCE_FAILURE" in result.error
    assert not calls
    assert not (host / "audit-fallback.jsonl").exists()
# HASHI-Fingerprint: hfp_f78daa0f71b145ee92cce958b7be0e89
