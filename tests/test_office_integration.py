"""Exercise the actual Registry, HERV3 adapter projection and CLI MCP process."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.test_office_runtime import office_manifest
from tools.office.contract import OFFICE_TOOL_NAMES
from tools.registry import ToolRegistry
from tools.gateway.context import write_gateway_context, load_gateway_context

ROOT = Path(__file__).resolve().parents[1]


def registry(tmp_path, manifest, allowed=OFFICE_TOOL_NAMES, **kwargs):
    from orchestrator.canonical_audit import CanonicalAuditStore
    audit = CanonicalAuditStore(tmp_path / "audit", instance_id="OFFICE-TEST", agent_id="office")
    return ToolRegistry(allowed_tools=list(allowed), access_root=tmp_path, workspace_dir=tmp_path,
                        secrets={}, tool_options={"office":{"manifest":str(manifest),"state_root":str(tmp_path/"state"),"work_root":str(Path(os.environ.get("TEMP",str(tmp_path)))/"hashi-office-test-work")}},
                        audit_context={"agent_name":"office","instance_id":"OFFICE-TEST",
                                       "hashi_session_id":"session-office","request_id":"request-office"},
                        canonical_audit=audit, **kwargs)


def execute(registry, name, args, call_id):
    response = asyncio.run(registry.execute(name, args, call_id))
    assert not response.is_error, response.output
    return response, json.loads(response.output)


def test_registry_runtime_filter_permissions_and_canonical_audit(tmp_path, office_manifest):
    missing = ToolRegistry(allowed_tools=list(OFFICE_TOOL_NAMES), access_root=tmp_path,
                           workspace_dir=tmp_path, secrets={})
    assert missing.get_tool_definitions(tiers=["office_basic"]) == []
    reg = registry(tmp_path, office_manifest)
    assert [t["function"]["name"] for t in reg.get_tool_definitions(tiers=["office_basic"])] == list(OFFICE_TOOL_NAMES)
    assert reg.is_read_only("office_inspect") and not reg.is_read_only("office_edit")
    _, created = execute(reg, "office_create", {"format":"xlsx","output":"registry.xlsx","content":"A,B\n1,2"}, "create")
    response, saved = execute(reg, "office_finish", {"file_id":created["file_id"]}, "finish")
    assert saved["checks"]["saved"] and (tmp_path / "registry.xlsx").is_file()
    assert response.details["effect_receipt"]["readback"]
    events = [json.loads(line) for line in reg.canonical_audit.events_path.read_text(encoding="utf-8").splitlines()]
    assert len(events) >= 2
    restricted = registry(tmp_path, office_manifest, allowed=["office_inspect"])
    denied = asyncio.run(restricted.execute("office_create", {"format":"docx","output":"denied.docx","content":"x"}, "deny"))
    assert denied.is_error and not (tmp_path/"denied.docx").exists()
    assert not any(name in sys.modules for name in ("openpyxl","docx","pptx","pymupdf"))


def test_managed_catalogue_hides_unqualified_office_channel(tmp_path,office_manifest):
    from orchestrator.permissions.registry import ManagedCatalogueRegistry
    ordinary = registry(tmp_path,office_manifest)
    managed = ManagedCatalogueRegistry(allowed_tools=list(OFFICE_TOOL_NAMES),access_root=tmp_path,
        workspace_dir=tmp_path,secrets={},tool_options=ordinary.tool_options)
    names = [d["function"]["name"] for d in managed.get_tool_definitions(tiers=["office_basic"])]
    assert not set(names).intersection(OFFICE_TOOL_NAMES)
    assert managed.tool_availability("office_edit")["reason"]=="office_managed_channel_unqualified"
    denied = asyncio.run(managed.execute("office_edit",{"file_id":"invented","action":"set_text"}))
    assert denied.details["control_disposition"]=="denied"

def test_canonical_last_attachment_is_scoped_and_follows_visible_history(tmp_path):
    import hashlib
    from orchestrator.session_store import SessionStore
    from tools.office.integration import canonical_attachments
    from tools.office.gateway import GatewayError
    store = SessionStore(tmp_path/"session.sqlite",instance_id="TEST")
    owner = "user:office"
    session = store.ensure_default_session(owner_id=owner,agent_id="office")
    sid = session["session_id"]
    raw = b"Office attachment bytes are subsequently validated by the worker"
    attachment = store.stage_attachment(session_id=sid,owner_id=owner,filename="uploaded.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
    aid = attachment["attachment_id"]
    store.upload_attachment_bytes(session_id=sid,owner_id=owner,attachment_id=aid,payload=raw)
    store.commit_attachment(session_id=sid,owner_id=owner,attachment_id=aid)
    store.accept_run(session_id=sid,owner_id=owner,agent_id="office",request_id="attach",
        text="uploaded",source="api",idempotency_key="attach",
        content=[{"type":"text","text":"uploaded"},{"type":"attachment","attachment_id":aid}])
    store.accept_run(session_id=sid,owner_id=owner,agent_id="office",request_id="follow-up",
        text="read the attached report",source="api",idempotency_key="follow-up")
    current = store.recent_visible_messages(sid,owner_id=owner)[-1]
    context = {"owner_id":owner,"hashi_session_id":sid,"hashi_message_id":current["message_id"],
               "agent_name":"office","_runtime":SimpleNamespace(session_store=store)}
    filename,content,provenance = canonical_attachments(context)
    assert (filename,content)==("uploaded.docx",raw)
    assert provenance=={"attachment_id":aid,"attachment_sha256":hashlib.sha256(raw).hexdigest()}
    with pytest.raises(GatewayError,match="different Agent"):
        canonical_attachments({**context,"agent_name":"another"})

def test_herv3_attachment_evidence_uses_resolved_id():
    from adapters.her_v2_provider import _EvidenceRecordingToolRegistry
    from orchestrator.her_v2.models import StageRequest,Stage,Effort
    from tools.registry import ToolResult
    class Resolved:
        def is_read_only(self,name): return True
        async def execute(self,*args):
            return ToolResult(tool_call_id="inspect",output='{"status":"ok"}',is_error=False,
                details={"office_result":{"status":"ok","data":{"attachment_id":"first"}}})
    request = StageRequest(turn_id="office",request_ref="office",stage=Stage.DIRECT,role="lightweight",
        attempt=1,goal="Inspect uploaded workbook",classification=None,effort=Effort.ZERO,
        allow_tools=True,allow_side_effects=True,invocation_id="office-evidence",
        attachment_manifest=({"attachment_id":"first"},{"attachment_id":"second"}))
    wrapped = _EvidenceRecordingToolRegistry(Resolved(),request)
    asyncio.run(wrapped.execute("office_inspect",{"source":"last_attachment"},"inspect"))
    assert wrapped.receipts[0].details["attachment_ids"]==["first"]

def test_local_adapter_discovers_office_only_for_relevant_turn(tmp_path, office_manifest):
    from adapters.ollama_api import OllamaAdapter
    reg = registry(tmp_path, office_manifest, allowed=(*OFFICE_TOOL_NAMES, "file_read"))
    adapter = OllamaAdapter.__new__(OllamaAdapter)
    adapter.config = SimpleNamespace(model="test-local-model", extra={})
    adapter.tool_registry = reg
    adapter.ollama_url = "http://127.0.0.1:11434/v1/chat/completions"
    adapter.effort = None
    unrelated = adapter._build_payload([{"role":"user","content":"What is 2+2?"}])
    assert not {x["function"]["name"] for x in unrelated.get("tools", [])}.intersection(OFFICE_TOOL_NAMES)
    requested = adapter._build_payload([{"role":"user","content":"Create an Excel workbook with these totals."}])
    assert {x["function"]["name"] for x in requested["tools"]}.issuperset(OFFICE_TOOL_NAMES)
    attachment = adapter._build_payload([{"role":"user","content":"Read the attached report.xlsx."}])
    assert "office_inspect" in {x["function"]["name"] for x in attachment["tools"]}


def test_shared_configuration_is_frozen_for_cli(tmp_path, office_manifest, monkeypatch):
    shared = tmp_path / "shared.json"
    shared.write_text(json.dumps({"manifest":str(office_manifest),"state_root":str(tmp_path/"state"),"work_root":str(Path(os.environ.get("TEMP",str(tmp_path)))/"hashi-office-test-work")}),encoding="utf-8")
    monkeypatch.setenv("HASHI_OFFICE_RUNTIME_CONFIG",str(shared))
    reg = ToolRegistry(allowed_tools=list(OFFICE_TOOL_NAMES), access_root=tmp_path,workspace_dir=tmp_path,secrets={},
                       audit_context={"agent_name":"office","instance_id":"OFFICE-TEST","hashi_session_id":"session"})
    write_gateway_context(reg,tmp_path/"context.json",backend="codex")
    context = load_gateway_context(tmp_path/"context.json")
    monkeypatch.delenv("HASHI_OFFICE_RUNTIME_CONFIG")
    assert context.tool_options["office"]["manifest"] == str(office_manifest)
    assert len(context.build_registry().get_tool_definitions(tiers=["office_basic"])) == 5


def test_real_cli_mcp_creates_and_finishes_actual_file(tmp_path, office_manifest):
    reg = registry(tmp_path, office_manifest)
    context_path = tmp_path/"context.json"
    write_gateway_context(reg,context_path,backend="codex")
    env = {key:value for key,value in os.environ.items() if key.upper() in {
        "SYSTEMROOT","WINDIR","PATH","COMSPEC","PROCESSOR_ARCHITECTURE","NUMBER_OF_PROCESSORS"}}
    env.update(PYTHONPATH=str(ROOT),PYTHONDONTWRITEBYTECODE="1",TEMP=str(tmp_path),TMP=str(tmp_path))
    def rpc(number, tool, arguments):
        request = {"jsonrpc":"2.0","id":number,"method":"tools/call",
                   "params":{"name":tool,"arguments":arguments}}
        process = subprocess.run([sys.executable,"-B","-m","tools.gateway.mcp_stdio","--context",str(context_path)],
                                 cwd=ROOT,env=env,input=json.dumps(request)+"\n",text=True,
                                 capture_output=True,timeout=90,check=True)
        # JSON-line input selects JSON-line output, including the real MCP server.
        response = json.loads(process.stdout.strip())
        assert "error" not in response, response
        value = response["result"]
        assert not value.get("isError"), value
        return json.loads(value["content"][0]["text"])
    created = rpc(10,"office_create",{"format":"docx","output":"cli.docx","content":"# CLI acceptance\n\n中文内容"})
    assert created["status"] == "ok", created
    finished = rpc(20,"office_finish",{"file_id":created["file_id"]})
    assert finished["checks"]["structure"] == "passed"
    import zipfile
    with zipfile.ZipFile(tmp_path/"cli.docx") as package:
        assert "中文内容" in package.read("word/document.xml").decode()
