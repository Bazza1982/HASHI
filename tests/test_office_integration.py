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
    assert denied.is_error and denied.details["control_disposition"]=="unavailable"

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

def test_office_selects_old_visible_attachment_and_never_crosses_message_boundary(tmp_path):
    import hashlib
    from orchestrator.session_store import SessionConflict, SessionStore
    from tools.office.contract import OfficeInputError, validate_office_arguments
    from tools.office.gateway import GatewayError, OfficeGateway
    from tools.office.integration import canonical_attachments
    from tests.test_office_gateway_remediation import TestSidecar, call

    store = SessionStore(tmp_path / "session.sqlite", instance_id="TEST")
    owner, agent = "user:office", "office"
    sid = store.ensure_default_session(owner_id=owner, agent_id=agent)["session_id"]

    def attach(target_session, target_owner, target_agent, name, raw, key):
        attachment = store.stage_attachment(
            session_id=target_session, owner_id=target_owner, filename=name,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
        )
        aid = attachment["attachment_id"]
        store.upload_attachment_bytes(session_id=target_session, owner_id=target_owner,
                                      attachment_id=aid, payload=raw)
        store.commit_attachment(session_id=target_session, owner_id=target_owner, attachment_id=aid)
        store.accept_run(session_id=target_session, owner_id=target_owner, agent_id=target_agent,
                         request_id=key, text=name, source="api", idempotency_key=key,
                         content=[{"type": "text", "text": name},
                                  {"type": "attachment", "attachment_id": aid}])
        return aid

    old_raw = b"old-document"
    old_id = attach(sid, owner, agent, "old.docx", old_raw, "old-upload")
    for index in range(205):
        store.append_presentation_message(
            session_id=sid, owner_id=owner, agent_id=agent, role="assistant",
            text="filler", source="api", idempotency_key=f"filler-{index}",
        )
    store.accept_run(session_id=sid, owner_id=owner, agent_id=agent, request_id="old-only",
                     text="read the old document", source="api", idempotency_key="old-only")
    old_message = store.recent_visible_messages(sid, owner_id=owner)[-1]
    old_context = {"owner_id": owner, "hashi_session_id": sid,
                   "hashi_message_id": old_message["message_id"], "agent_name": agent,
                   "_runtime": SimpleNamespace(session_store=store)}
    assert canonical_attachments(old_context)[2]["attachment_id"] == old_id

    new_id = attach(sid, owner, agent, "new.docx", b"new-document", "new-upload")
    store.accept_run(session_id=sid, owner_id=owner, agent_id=agent, request_id="current",
                     text="compare the documents", source="api", idempotency_key="current")
    current = store.recent_visible_messages(sid, owner_id=owner)[-1]
    context = {"owner_id": owner, "hashi_session_id": sid, "hashi_message_id": current["message_id"],
               "agent_name": agent, "_runtime": SimpleNamespace(session_store=store)}
    assert canonical_attachments(context)[2]["attachment_id"] == new_id
    filename, data, evidence = canonical_attachments(context, old_id)
    assert (filename, data, evidence["attachment_id"]) == ("old.docx", old_raw, old_id)

    future_id = attach(sid, owner, agent, "future.docx", b"later", "future-upload")
    other_owner = "user:other"
    other_sid = store.ensure_default_session(owner_id=other_owner, agent_id=agent)["session_id"]
    foreign_id = attach(other_sid, other_owner, agent, "foreign.docx", b"foreign", "foreign-upload")
    for inaccessible in (future_id, foreign_id):
        with pytest.raises(GatewayError, match="No matching Office attachment"):
            canonical_attachments(context, inaccessible)
    assert canonical_attachments(context)[2]["attachment_id"] == new_id
    listed = canonical_attachments(context, list_only=True)
    assert [entry["attachment_id"] for entry in listed["attachments"]] == [new_id, old_id]
    assert listed["next_cursor"] is None
    with pytest.raises(GatewayError, match="cursor is invalid"):
        canonical_attachments(context, list_only=True, cursor="999999:0")

    assert validate_office_arguments("office_inspect", {"attachment_id": old_id})["attachment_id"] == old_id
    with pytest.raises(OfficeInputError):
        validate_office_arguments("office_inspect", {"attachment_id": old_id, "source": "last_attachment"})
    runtime = TestSidecar(tmp_path / "office-state")
    gateway = OfficeGateway(runtime, read_roots=(tmp_path,), write_roots=(tmp_path,), cwd=tmp_path,
                            context={**context, "instance_id": "TEST", "request_id": "request"},
                            attachment_loader=lambda selected=None, **kw: canonical_attachments(context, selected, **kw))
    inspected = call(gateway, "office_inspect", {"attachment_id": old_id}, "explicit-old")
    assert inspected["status"] == "ok", inspected
    assert inspected["data"]["attachment_id"] == old_id
    inventory = call(gateway, "office_inspect", {"source": "list_attachments"}, "inventory")
    assert inventory["status"] == "ok", inventory
    assert [entry["attachment_id"] for entry in inventory["data"]["attachments"]] == [new_id, old_id]

    part = store.attachment_canonical_part(session_id=sid, owner_id=owner,
                                           attachment_id=old_id, item_index=0)
    Path(part["local_ref"]).write_bytes(b"X" * len(old_raw))
    with pytest.raises(SessionConflict, match="digest changed"):
        canonical_attachments(context, old_id)
    damaged = canonical_attachments(context, list_only=True)["attachments"][-1]
    assert damaged["attachment_id"] == old_id and damaged["available"] is False


def test_office_attachment_inventory_paginates_within_one_message(tmp_path):
    import hashlib
    from orchestrator.session_store import SessionStore
    from tools.office.integration import canonical_attachments

    store = SessionStore(tmp_path / "inventory.sqlite", instance_id="TEST")
    owner, agent = "user:inventory", "office"
    sid = store.ensure_default_session(owner_id=owner, agent_id=agent)["session_id"]
    groups = []
    for group in range(2):
        ids = []
        for index in range(16):
            raw = f"{group}-{index}".encode()
            record = store.stage_attachment(
                session_id=sid, owner_id=owner, filename=f"{group}-{index}.docx",
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
            )
            aid = record["attachment_id"]
            store.upload_attachment_bytes(session_id=sid, owner_id=owner, attachment_id=aid, payload=raw)
            store.commit_attachment(session_id=sid, owner_id=owner, attachment_id=aid)
            ids.append(aid)
        groups.append(ids)
        store.accept_run(
            session_id=sid, owner_id=owner, agent_id=agent, request_id=f"group-{group}",
            text="uploaded", source="api", idempotency_key=f"group-{group}",
            content=[{"type": "text", "text": "uploaded"},
                     *[{"type": "attachment", "attachment_id": aid} for aid in ids]],
        )
    store.accept_run(session_id=sid, owner_id=owner, agent_id=agent, request_id="inventory",
                     text="list the documents", source="api", idempotency_key="inventory")
    current = store.recent_visible_messages(sid, owner_id=owner)[-1]
    context = {"owner_id": owner, "hashi_session_id": sid, "hashi_message_id": current["message_id"],
               "agent_name": agent, "_runtime": SimpleNamespace(session_store=store)}
    first = canonical_attachments(context, list_only=True)
    assert len(first["attachments"]) == 25 and first["next_cursor"]
    second = canonical_attachments(context, list_only=True, cursor=first["next_cursor"])
    assert len(second["attachments"]) == 7 and second["next_cursor"] is None
    actual = [item["attachment_id"] for item in first["attachments"] + second["attachments"]]
    assert actual == list(reversed(groups[1])) + list(reversed(groups[0]))


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
# HASHI-Fingerprint: hfp_43b8755440334a48ae5f0177640d40c9



def test_office_attachment_uses_selected_file_budget(tmp_path):
    import hashlib
    from orchestrator.session_store import SessionStore
    from tools.office.integration import canonical_attachments
    from tools.office.gateway import GatewayError

    store = SessionStore(tmp_path / "large-session.sqlite", instance_id="TEST")
    owner, agent = "user:office", "office"
    sid = store.ensure_default_session(owner_id=owner, agent_id=agent)["session_id"]
    raw = b"x" * (33 * 1024 * 1024)
    digest = hashlib.sha256(raw).hexdigest()
    staged = store.stage_attachment(session_id=sid, owner_id=owner, filename="large.pdf",
                                    media_type="application/pdf", size_bytes=len(raw), sha256=digest)
    aid = staged["attachment_id"]
    store.upload_attachment_bytes(session_id=sid, owner_id=owner, attachment_id=aid, payload=raw)
    store.commit_attachment(session_id=sid, owner_id=owner, attachment_id=aid)
    store.accept_run(session_id=sid, owner_id=owner, agent_id=agent, request_id="large-attachment",
                     text="read", source="api", idempotency_key="large-attachment",
                     content=[{"type": "text", "text": "read"}, {"type": "attachment", "attachment_id": aid}])
    message = store.recent_visible_messages(sid, owner_id=owner)[-1]
    context = {"owner_id": owner, "hashi_session_id": sid, "hashi_message_id": message["message_id"],
               "agent_name": agent, "_runtime": SimpleNamespace(session_store=store)}
    with pytest.raises(GatewayError, match="file_bytes budget"):
        canonical_attachments(context, aid, max_bytes=32 * 1024 * 1024)
    filename, content, facts = canonical_attachments(context, aid, max_bytes=40 * 1024 * 1024)
    assert filename == "large.pdf" and len(content) == len(raw)
    assert hashlib.sha256(content).hexdigest() == facts["attachment_sha256"] == digest



def test_real_office_working_copy_reopens_in_authorized_session(tmp_path, office_manifest):  # noqa: F811 - pytest fixture import
    import zipfile
    from orchestrator.session_store import SessionStore

    store = SessionStore(tmp_path / "sessions.sqlite", instance_id="TEST")
    owner, agent = "user:office", "office"
    first = store.create_session(owner_id=owner, agent_id=agent)["session_id"]
    second = store.create_session(owner_id=owner, agent_id=agent)["session_id"]
    options = {"office": {
        "manifest": str(office_manifest), "state_root": str(tmp_path / "office-state"),
        "work_root": str(tmp_path / "office-transport"),
    }}

    def make(session):
        return ToolRegistry(
            allowed_tools=list(OFFICE_TOOL_NAMES), access_root=tmp_path,
            workspace_dir=tmp_path, secrets={}, tool_options=options,
            audit_context={
                "instance_id": "TEST", "agent_name": agent, "owner_id": owner,
                "hashi_session_id": session, "request_id": "office-reopen-" + session,
                "_runtime": SimpleNamespace(session_store=store),
            },
        )

    source, destination = make(first), make(second)
    _, created = execute(source, "office_create", {
        "format": "docx", "output": "original.docx", "content": "# Document\n\nOriginal",
    }, "create")
    _, edited = execute(source, "office_edit", {
        "file_id": created["file_id"], "action": "append_section", "content": "Unsaved addition",
    }, "edit")
    assert edited["data"]["revision"] == 2
    assert not (tmp_path / "original.docx").exists()

    _, inventory = execute(destination, "office_inspect", {
        "source": "list_working_copies", "source_session_id": first,
    }, "discover")
    selected = next(item for item in inventory["data"]["files"] if item["file_id"] == created["file_id"])
    assert selected["name"] == "original.docx" and selected["revision"] == 2
    request = {
        "source": "working_copy", "source_session_id": first,
        "source_file_id": selected["file_id"], "source_revision": selected["revision"],
    }
    _, restored = execute(destination, "office_inspect", request, "reopen")
    assert restored["file_id"] != created["file_id"]
    assert restored["data"]["source_revision"] == 2
    _, read = execute(destination, "office_read", {
        "file_id": restored["file_id"], "target": "paragraph:1-10", "view": "text",
    }, "read")
    assert any("Unsaved addition" in item["text"] for item in read["data"]["paragraphs"])
    _, continued = execute(destination, "office_edit", {
        "file_id": restored["file_id"], "action": "append_section", "content": "New session addition",
    }, "continue-edit")
    assert continued["data"]["revision"] == 2
    _, saved = execute(destination, "office_finish", {
        "file_id": restored["file_id"], "output": "reopened.docx",
    }, "finish")
    assert saved["checks"]["saved"] and saved["checks"]["structure"] == "passed"
    with zipfile.ZipFile(tmp_path / "reopened.docx") as archive:
        document = archive.read("word/document.xml")
        assert b"Unsaved addition" in document and b"New session addition" in document
    assert not (tmp_path / "original.docx").exists()
