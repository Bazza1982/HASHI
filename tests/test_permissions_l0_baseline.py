"""Observable L0 baseline captured from main before permission integration."""
from hashlib import sha256
import json
from pathlib import Path

import pytest

from orchestrator.her_v2.audit import DurableAuditLog
from orchestrator.her_v2.config import HERv2Config
from orchestrator.her_v2.interfaces import RecordingDelivery
from orchestrator.her_v2.ledger import LedgerStore
from orchestrator.her_v2.models import StageResponse
from orchestrator.her_v2.runtime import HERv2Runtime
from orchestrator.her_v2.v3_prompt import compile_main_prompt
from orchestrator.permissions.authority import PermissionAuthority
from tools.registry import ToolRegistry


def _digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return sha256(raw).hexdigest()


class DeterministicProvider:
    def __init__(self):
        self.calls = []

    def tool_catalogue(self, *, allow_side_effects, delegated_tools=None):
        return ()

    async def invoke(self, profile, request):
        self.calls.append((profile, request))
        return StageResponse(data={"message": "Main answer."}, provider=profile.engine, model=profile.model)


@pytest.mark.asyncio
async def test_l0_matches_frozen_main_without_constructing_permission_authority(tmp_path, monkeypatch):
    def forbidden_authority(*args, **kwargs):
        raise AssertionError("L0 must not construct a permission authority")

    monkeypatch.setattr(PermissionAuthority, "__init__", forbidden_authority)
    toolroot = tmp_path / "tools"
    toolroot.mkdir()
    registry = ToolRegistry(
        allowed_tools=["file_read", "file_write", "file_list"],
        access_root=toolroot,
        workspace_dir=toolroot,
        secrets={},
        audit_context={"agent_name": "baseline-agent", "workspace_dir": str(toolroot), "safety_mode": "read_write"},
    )
    definitions = registry.get_tool_definitions()
    write = await registry.execute("file_write", {"path": "note.txt", "content": "baseline data"}, tool_call_id="write")
    read = await registry.execute("file_read", {"path": "note.txt"}, tool_call_id="read")
    denied = await registry.execute("shell", {"command": "should-not-run"}, tool_call_id="deny")
    audit = [
        json.loads(line)
        for line in (toolroot / "tool_action_audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    system, user = compile_main_prompt(
        pcm_input={"current_request": "do the task", "sections": [], "history": []},
        fallback_request="fallback",
        context={"strategy_playbook": {"cards": [{"id": "SAFE"}]}, "habit_catalogue": []},
    )
    herroot = tmp_path / "her"
    herroot.mkdir()
    provider = DeterministicProvider()
    runtime = HERv2Runtime(
        config=HERv2Config.from_mapping({"main": {"provider": "fake-api", "model": "main-model"}}),
        provider=provider,
        ledger_store=LedgerStore(herroot / "ledgers"),
        audit_log=DurableAuditLog(herroot / "audit.jsonl", herroot / "audit-fallback.jsonl"),
        delivery=RecordingDelivery(),
    )
    result = await runtime.run_turn("Answer directly", "request-l0")
    observed = {
        "tool_names": [item["function"]["name"] for item in definitions],
        "schemas": _digest(definitions),
        "file_contents": (toolroot / "note.txt").read_text(encoding="utf-8"),
        "file_write_error": write.is_error,
        "file_read_error": read.is_error,
        "file_read_contains": "baseline data" in read.output,
        "denial_error": denied.is_error,
        "denial_disposition": denied.details.get("control_disposition"),
        "audit": [{"tool": row.get("tool_name"), "status": row.get("status")} for row in audit],
        "prompt_system": _digest(system),
        "prompt_user": _digest(user),
        "stage": provider.calls[0][1].stage.value,
        "provider_calls": len(provider.calls),
        "terminal": result.terminal_state.value,
    }
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "permission_l0_baseline.json").read_text(encoding="utf-8")
    )
    assert observed == fixture["observation"]
