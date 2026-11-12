"""Thin ToolRegistry adapter; no second permission, audit or delivery service."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .gateway import OfficeGateway, GatewayError, result
from .runtime import OfficeRuntime


def canonical_attachments(context):
    """Resolve the latest visible inbound Office attachment at this Message."""

    from orchestrator.session_store import SessionStore
    runtime = context.get("_runtime")
    store = getattr(runtime, "session_store", None)
    if not isinstance(store, SessionStore):
        descriptor = context.get("session_store_descriptor") or {}
        if not descriptor:
            raise GatewayError("ATTACHMENT_UNAVAILABLE", "Canonical Session attachment storage is unavailable.")
        store = SessionStore(str(descriptor["db_path"]), instance_id=str(descriptor["instance_id"]),
                             attachment_root=str(descriptor["attachment_root"]))
    owner, session = str(context.get("owner_id") or ""), str(context.get("hashi_session_id") or "")
    actual = store.get_session(session, owner_id=owner)
    if str(actual.get("agent_id") or "").casefold() != str(context.get("agent_name") or "").casefold():
        raise GatewayError("ATTACHMENT_DENIED", "Attachment Session belongs to a different Agent.")
    message = store.get_message(str(context.get("hashi_message_id") or ""),
                                session_id=session, owner_id=owner)
    if message.get("role") != "user":
        raise GatewayError("ATTACHMENT_UNAVAILABLE", "Current Message is not an inbound user attachment source.")
    visible = store.recent_visible_messages(session,owner_id=owner,limit=200)
    candidates = [item for item in visible if item.get("role")=="user"
                  and int(item.get("ordinal",0))<=int(message["ordinal"])]
    attachment_id = None
    for item in reversed(candidates):
        content = item.get("content") or {}
        parts = content.get("parts",[]) if isinstance(content,dict) else content
        for part in reversed(parts):
            if not isinstance(part,dict) or part.get("type") not in {"media","attachment"}:
                continue
            part_id = str(part.get("attachment_id") or "")
            metadata = store.attachment_canonical_part(session_id=session,owner_id=owner,
                                                       attachment_id=part_id,item_index=0)
            filename = str(metadata.get("filename") or "")
            if Path(filename).suffix.lower() not in {".xlsx",".xlsm",".docx",".docm",".pptx",".pptm",".pdf",".csv"}:
                continue
            if int(metadata.get("size_bytes") or 0)>32*1024*1024:
                raise GatewayError("FILE_LIMIT","Office attachment exceeds 32 MiB.")
            attachment_id = part_id
            break
        if attachment_id:
            break
    if not attachment_id:
        raise GatewayError("ATTACHMENT_UNAVAILABLE","No Office attachment in the last 200 visible messages up to this request. Attach a document or provide an authorized path.")
    attachment, raw = store.attachment_bytes(session_id=session, owner_id=owner,
                                             attachment_id=attachment_id)
    if len(raw) > 32 * 1024 * 1024:
        raise GatewayError("FILE_LIMIT", "Office attachment exceeds 32 MiB.")
    return str(attachment["filename"]), raw, {"attachment_id":attachment_id,"attachment_sha256":attachment["sha256"]}


async def execute_office(registry, name, arguments, call_id):
    from tools.builtins import BuiltinExecutionResult, execute_frontend_publish_deliverable
    context = dict(registry._effective_audit_context())
    read_roots, _, cwd = registry._read_scope()

    def guard(path, write):
        tool = "file_write" if write else "file_read"
        for check in (registry._check_live_runtime_gate, registry._check_enterprise_path_gate):
            denial = check(tool, {"path": str(path)}, tool_call_id=call_id)
            if denial is not None:
                raise GatewayError("PATH_DENIED", denial.output)

    async def deliver(path, publication):
        # The existing service freezes the originating Run's destinations and
        # current executor fencing token. No owner/chat/route is model-selectable.
        raw = await execute_frontend_publish_deliverable(
            {"publication_id": publication, "attachments": [{"path": str(path)}],
             "text": "Office output: " + path.name},
            access_root=registry.access_roots, workspace_dir=cwd,
            audit_context=context, tool_call_id=call_id)
        if raw.startswith("Error:"):
            raise GatewayError("DELIVERY_UNCONFIRMED", raw)
        return json.loads(raw)

    try:
        gateway = OfficeGateway(OfficeRuntime(registry.tool_options.get("office") or {}),
                                read_roots=read_roots, write_roots=registry.access_roots,
                                cwd=cwd, context=context, path_guard=guard,
                                attachment_loader=lambda: canonical_attachments(context),
                                delivery=deliver, effect_guard=context.get("_tool_dispatch_guard"))
        value = await gateway.execute(name, arguments, call_id)
    except (ValueError, OSError) as exc:
        value = result("error", getattr(exc, "code", "OFFICE_UNAVAILABLE"), summary=str(exc))
    details = {"office_result": value, "retryable": False}
    # Reuse canonical effect projection: file content is binary; the readback
    # evidence is its verified structure plus its actual byte digest.
    if name in {"office_inspect","office_read"} and value["status"] == "ok":
        from tools.effect_receipts import _render_effect
        observed = json.dumps(value["data"],ensure_ascii=False,sort_keys=True)
        details["effect_receipt"] = _render_effect(name,call_id,"read",value["file_id"],observed,hashlib.sha256(observed.encode()).hexdigest())
    if value["checks"]["saved"]:
        from tools.effect_receipts import _render_effect
        output = Path(value["data"]["output"])
        expected = value["data"]["sha256"]
        if output.is_file() and hashlib.sha256(output.read_bytes()).hexdigest() == expected:
            receipt = _render_effect(name, call_id, "write", str(output),
                                     json.dumps(value["checks"], sort_keys=True), expected)
            details["effect_receipt"] = receipt
    return BuiltinExecutionResult(json.dumps(value, ensure_ascii=False), details)
