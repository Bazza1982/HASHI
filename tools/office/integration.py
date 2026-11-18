"""Thin ToolRegistry adapter; no second permission, audit or delivery service."""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

from .gateway import OfficeGateway, GatewayError, result
from .runtime import OfficeRuntime


def _attachment_page(store, owner, session, message, cursor):
    """Page newest-first without crossing the request's visible message boundary."""
    from orchestrator.session_store import SessionStoreError

    anchor = int(message["ordinal"])
    part_before = None
    upper = anchor
    if cursor:
        try:
            ordinal, index = str(cursor).split(":", 1)
            upper, part_before = int(ordinal), int(index)
            if upper < 1 or upper > anchor or part_before < 0:
                raise ValueError()
        except (TypeError, ValueError):
            raise GatewayError("INVALID_CURSOR", "Office attachment cursor is invalid.") from None
    generation = int(message["context_generation"])
    before = upper + 1
    entries = []
    while before > 0:
        page = store.recent_visible_messages(
            session, owner_id=owner, context_generation=generation,
            limit=200, before_ordinal=before,
        )
        if not page:
            break
        for item in reversed(page):
            if item.get("role") != "user":
                continue
            content = item.get("content") or {}
            parts = content.get("parts", []) if isinstance(content, dict) else content
            if not isinstance(parts, list):
                continue
            ordinal = int(item["ordinal"])
            end = min(len(parts), part_before) if ordinal == upper and part_before is not None else len(parts)
            for index in range(end - 1, -1, -1):
                part = parts[index]
                if not isinstance(part, dict) or part.get("type") not in {"media", "attachment"}:
                    continue
                part_id = str(part.get("attachment_id") or "")
                if not part_id:
                    continue
                available = True
                try:
                    metadata = store.attachment_canonical_part(
                        session_id=session, owner_id=owner, attachment_id=part_id, item_index=index,
                    )
                except (SessionStoreError, OSError, ValueError):
                    metadata, available = part, False
                filename = str(metadata.get("filename") or "")
                if filename and Path(filename).suffix.lower() not in {".xlsx", ".xlsm", ".docx", ".docm", ".pptx", ".pptm", ".pdf", ".csv"}:
                    continue
                entries.append({
                    "attachment_id": part_id, "filename": filename,
                    "message_id": str(item["message_id"]), "available": available,
                })
                if len(entries) == 25:
                    return {"attachments": entries, "next_cursor": f"{ordinal}:{index}"}
        before = int(page[0]["ordinal"])
        if len(page) < 200:
            break
    return {"attachments": entries, "next_cursor": None}


def canonical_attachments(context, attachment_id=None, *, list_only=False, cursor=None, max_bytes=32 * 1024 * 1024):
    """Resolve a visible inbound Office attachment at or before this Message."""

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
    if list_only:
        return _attachment_page(store, owner, session, message, cursor)
    requested = str(attachment_id or "").strip()
    selected = None
    before = int(message["ordinal"]) + 1
    generation = int(message["context_generation"])
    while before > 0 and selected is None:
        page = store.recent_visible_messages(
            session, owner_id=owner, context_generation=generation,
            limit=200, before_ordinal=before,
        )
        if not page:
            break
        for item in reversed(page):
            if item.get("role") != "user":
                continue
            content = item.get("content") or {}
            parts = content.get("parts", []) if isinstance(content, dict) else content
            for part in reversed(parts):
                if not isinstance(part, dict) or part.get("type") not in {"media", "attachment"}:
                    continue
                part_id = str(part.get("attachment_id") or "")
                if not part_id or (requested and part_id != requested):
                    continue
                metadata = store.attachment_canonical_part(
                    session_id=session, owner_id=owner, attachment_id=part_id, item_index=0,
                )
                filename = str(metadata.get("filename") or "")
                if Path(filename).suffix.lower() not in {".xlsx", ".xlsm", ".docx", ".docm", ".pptx", ".pptm", ".pdf", ".csv"}:
                    if requested:
                        raise GatewayError("FORMAT_UNSUPPORTED", "The selected attachment is not a supported Office file.")
                    continue
                size = int(metadata.get("size_bytes") or 0)
                if size > max_bytes:
                    raise GatewayError("FILE_LIMIT", f"Attachment {size} bytes exceeds file_bytes budget {max_bytes}; increase the configured resource budget.")
                selected = part_id
                break
            if selected:
                break
        before = int(page[0]["ordinal"])
        if len(page) < 200:
            break
    if not selected:
        raise GatewayError("ATTACHMENT_UNAVAILABLE", "No matching Office attachment is visible in this Session up to the current message.")
    attachment, raw = store.attachment_bytes(session_id=session, owner_id=owner,
                                             attachment_id=selected)
    if len(raw) > max_bytes:
        raise GatewayError("FILE_LIMIT", f"Attachment {len(raw)} bytes exceeds file_bytes budget {max_bytes}; increase the configured resource budget.")
    if hashlib.sha256(raw).hexdigest() != str(attachment["sha256"]):
        raise GatewayError("ATTACHMENT_CHANGED", "The selected attachment changed during reading.")
    return str(attachment["filename"]), raw, {"attachment_id":selected,"attachment_sha256":attachment["sha256"]}


def _working_copy_authority(context):
    """Resolve the same canonical SessionStore and identity used for a request."""
    from orchestrator.session_store import SessionStore, SessionStoreError

    try:
        runtime = context.get("_runtime")
        store = getattr(runtime, "session_store", None)
        if not isinstance(store, SessionStore):
            descriptor = context.get("session_store_descriptor") or {}
            if not descriptor:
                raise ValueError("Session store is unavailable")
            store = SessionStore(
                str(descriptor["db_path"]), instance_id=str(descriptor["instance_id"]),
                attachment_root=str(descriptor["attachment_root"]),
            )
        config = context.get("global_config")
        instance = str(context.get("instance_id") or getattr(config, "instance_id", "")).strip()
        owner = str(context.get("owner_id") or "").strip()
        agent = str(context.get("agent_name") or "").strip().lower()
        current = str(context.get("hashi_session_id") or "").strip()
        if not all((instance, owner, agent, current)) or str(store.instance_id).casefold() != instance.casefold():
            raise ValueError("Canonical Session identity is unavailable")
        return store, (instance.casefold(), owner, agent, current)
    except (SessionStoreError, OSError, ValueError, KeyError, TypeError):
        raise GatewayError("WORKING_COPY_UNAVAILABLE", "The selected Session working copy is unavailable.") from None


def canonical_working_copy_context(context, source_session=None):
    """Authorize current and, when requested, source Sessions for this owner."""
    from orchestrator.session_store import SessionStoreError

    store, (_, owner, agent, current) = _working_copy_authority(context)
    try:
        sessions = (current,) if source_session is None else (current, source_session)
        for session in sessions:
            store.get_session(session, owner_id=owner, agent_id=agent, include_deleted=False)
    except (SessionStoreError, OSError, ValueError, KeyError, TypeError):
        raise GatewayError("WORKING_COPY_UNAVAILABLE", "The selected Session working copy is unavailable.") from None
    return dict(context) if source_session is None else {**context, "hashi_session_id": source_session}


@contextmanager
def _working_copy_commit_guard(context, expected_context, source_session):
    """Fence Session deletion/ownership changes through the short Office commit."""
    import os
    import sqlite3

    store, identity = _working_copy_authority(context)
    original_store, original_identity = _working_copy_authority(expected_context)
    if (identity != original_identity
            or os.path.normcase(os.path.realpath(store.db_path))
            != os.path.normcase(os.path.realpath(original_store.db_path))):
        raise GatewayError("WORKING_COPY_UNAVAILABLE", "The selected Session working copy is unavailable.")
    _, owner, agent, current = identity
    # BEGIN IMMEDIATE also fences writers using another SessionStore instance.
    # Keep this region short: no Office worker, network or user interaction.
    with store._lock, store._connection() as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            for session in (current, source_session):
                row = connection.execute(
                    "SELECT 1 FROM sessions WHERE session_id=? AND instance_id=? AND owner_id=? "
                    "AND agent_id=? AND status!='deleted'",
                    (str(session), store.instance_id, owner, agent),
                ).fetchone()
                if row is None:
                    raise GatewayError(
                        "WORKING_COPY_UNAVAILABLE", "The selected Session working copy is unavailable."
                    )
        except sqlite3.Error:
            raise GatewayError("WORKING_COPY_UNAVAILABLE", "The selected Session working copy is unavailable.") from None
        yield


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
        office_runtime = OfficeRuntime(registry.tool_options.get("office") or {})
        if (name == "office_inspect" and isinstance(arguments, dict)
                and arguments.get("source") in {"working_copy", "list_working_copies"}):
            # A stored idempotent result cannot substitute for the current
            # Session's authenticated owner.
            canonical_working_copy_context(context)

        def source_working_gateway(source_session):
            from .gateway import _no_links
            from .runtime import canonical

            source_context = canonical_working_copy_context(context, source_session)
            config = source_context.get("global_config")
            identity = [
                str(source_context.get("instance_id") or getattr(config, "instance_id", "")),
                str(source_context.get("agent_name") or ""),
                str(source_session),
            ]
            scope = hashlib.sha256(canonical(identity)).hexdigest()
            source_db = office_runtime.state_root / scope / "office.sqlite"
            _no_links(source_db)
            if not source_db.is_file():
                return None
            return OfficeGateway(
                office_runtime, read_roots=read_roots, write_roots=registry.access_roots,
                cwd=cwd, context=source_context, path_guard=guard,
            )

        def reopen_working_copy(source_session, source_file_id, revision, job):
            source_gateway = source_working_gateway(source_session)
            if source_gateway is None:
                raise GatewayError("FILE_NOT_FOUND", "The selected working file is unavailable.")
            return source_gateway.snapshot_working_copy(source_file_id, revision, job)

        def list_working_copies(source_session, after):
            source_gateway = source_working_gateway(source_session)
            if source_gateway is None:
                return {"files": [], "last_id": None}
            return source_gateway.list_working_copies(after)

        @contextmanager
        def working_copy_commit_guard(source_session):
            current_context = dict(registry._effective_audit_context())
            with _working_copy_commit_guard(current_context, context, source_session):
                yield

        gateway = OfficeGateway(office_runtime,
                                read_roots=read_roots, write_roots=registry.access_roots,
                                cwd=cwd, context=context, path_guard=guard,
                                attachment_loader=lambda attachment_id=None, **options: canonical_attachments(
                                    context, attachment_id, max_bytes=office_runtime.budgets["file_bytes"], **options),
                                delivery=deliver, effect_guard=context.get("_tool_dispatch_guard"),
                                working_copy_loader=reopen_working_copy,
                                working_copy_lister=list_working_copies,
                                working_copy_commit_guard=working_copy_commit_guard)
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
# HASHI-Fingerprint: hfp_3424c6083b954fb28b42fa1a08bf0e75
