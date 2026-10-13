"""Deterministic observations of existing tool effects, never model claims."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from collections.abc import Mapping


# Only tools whose implementation is observational belong here.  In particular,
# ``verification_run`` is deliberately absent: its argv/recipe can execute
# arbitrary workspace code even though SmartTools groups it under ``verify``.
_READ_EFFECT_TOOLS = frozenset(
    {
        "file_read",
        "log_query",
        "media_read",
        "vision_inspect",
        "web_search",
        "web_fetch",
        "file_list",
        "process_list",
        "request_diagnostics",
        "browser_active_tab",
        "browser_get_media_state",
        "browser_screenshot",
        "browser_get_text",
        "browser_get_html",
        "browser_get_attribute",
        "windows_screenshot",
        "windows_info",
        "windows_window_list",
        "desktop_screenshot",
        "desktop_info",
        "desktop_window_list",
        "hashi_scheduler_list",
        "hashi_scheduler_run_history",
        "hashi_superloop_list",
        "hashi_superloop_get",
        "obsidian_read_note",
        "obsidian_list_folder",
        "obsidian_search",
        "obsidian_get_active",
        "memory_search",
        "workspace_inspect",
    }
)


def is_verified_read_effect_receipt(
    *,
    tool_name: str,
    tool_call_id: str,
    receipt: Mapping[str, Any] | None,
    completed: object,
    status: object,
) -> bool:
    """Validate durable, exact evidence for one observational tool call.

    This owner also rejects old receipts for tools that were previously
    misclassified as read-only; consumers must not trust receipt shape alone.
    """

    name = str(tool_name or "")
    call_id = str(tool_call_id or "")
    if (
        not name
        or not call_id
        or name not in _READ_EFFECT_TOOLS
        or completed is not True
        or str(status or "").casefold() != "success"
        or not isinstance(receipt, Mapping)
        or receipt.get("kind") != "read"
        or str(receipt.get("tool_name") or "") != name
    ):
        return False
    revision = str(receipt.get("revision") or "")
    digest = revision.removeprefix("sha256:")
    return bool(
        revision.startswith("sha256:")
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
        and str(receipt.get("evidence_ref") or "")
        == f"tool:{call_id}:{revision}"
    )


def observe_tool_effect(*, tool_name: str, call_id: str, arguments: dict,
                        output: str, is_error: bool, workspace_dir: Path,
                        access_roots: tuple[Path, ...]) -> dict[str, Any] | None:
    if is_error:
        return None
    from tools.tool_audit import sanitize_value

    kind = "read" if tool_name in _READ_EFFECT_TOOLS else ""
    target = str(arguments.get("path") or arguments.get("url") or arguments.get("query") or "")
    observed = str(output or "")
    digest = hashlib.sha256(observed.encode("utf-8")).hexdigest()
    if tool_name in {"file_write", "apply_patch"}:
        from tools.builtins import _resolve_path
        try:
            path = _resolve_path(target, access_roots, workspace_dir)
            if path.stat().st_size > 8 * 1024 * 1024:
                return None
            data = path.read_bytes()
            actual = data.decode("utf-8")
            expected = str(arguments.get("content") or "")
            if tool_name == "file_write" and actual.replace("\r\n", "\n") != expected.replace("\r\n", "\n"):
                return None
            kind, target, observed = "write", str(path), actual
            digest = hashlib.sha256(data).hexdigest()
        except (OSError, UnicodeError, ValueError):
            return None
    if not kind:
        return None
    head = str(sanitize_value(observed[:600]))
    tail = str(sanitize_value(observed[-600:])) if len(observed) > 600 else ""
    return {"evidence_ref": f"tool:{call_id}:sha256:{digest}", "kind": kind,
            "tool_name": tool_name, "target": str(sanitize_value(target)),
            "revision": "sha256:" + digest, "readback": kind == "write",
            "observed": head, "observed_tail": tail,
            "observed_characters": len(observed),
            "complete_content": len(observed) <= 600 and head == observed}


def publish_phone_effect(*, context: Mapping[str, Any], receipt: Mapping[str, Any],
                         tool_call_id: str, workspace_dir: Path) -> None:
    """Project a real observation onto its existing Phone Run, not today's workzone.

    The receipt remains derived tool evidence. SessionStore owns the event writer;
    both local Workers and tool gateways use their existing authenticated scope.
    The caller isolates all observation failures from the actual tool result.
    """
    import json

    owner, session_id, run_id, request_id = (str(context.get(key) or "") for key in
        ("owner_id", "hashi_session_id", "hashi_run_id", "request_id"))
    if not all((owner, session_id, run_id, request_id)):
        return
    store = getattr(context.get("_runtime"), "session_store", None)
    if store is None:
        descriptor = context.get("session_store_descriptor")
        if not isinstance(descriptor, Mapping) or not descriptor.get("db_path"):
            return
        from orchestrator.session_store import SessionStore
        store = SessionStore(descriptor["db_path"], instance_id=descriptor["instance_id"],
                             attachment_root=descriptor.get("attachment_root"))
    with store._lock, store._connection() as connection:
        row = connection.execute(
            """SELECT c.call_id, c.call_epoch, c.agent_id FROM runs r
            JOIN sessions s ON s.session_id=r.session_id
            JOIN live_calls c ON c.session_id=r.session_id AND c.owner_id=s.owner_id AND c.agent_id=r.agent_id
            JOIN live_actions a ON a.call_id=c.call_id
            WHERE r.run_id=? AND r.request_id=? AND r.session_id=? AND s.owner_id=?
            AND c.context_generation=r.context_generation AND (a.run_id=r.run_id OR (
                a.run_id IS NULL AND a.status='accepted'
                AND c.call_id=json_extract(r.message_context_json, '$.live_voice.call_id')
                AND a.delegation_id=json_extract(r.message_context_json, '$.live_voice.delegation_id')
                AND EXISTS (SELECT 1 FROM live_delegations d WHERE d.call_id=c.call_id
                    AND d.delegation_id=a.delegation_id
                    AND d.call_epoch=json_extract(r.message_context_json, '$.live_voice.call_epoch')
                    AND d.proposal_digest=json_extract(r.message_context_json, '$.live_voice.proposal_digest')
                    AND d.proposal_version=json_extract(r.message_context_json, '$.live_voice.proposal_version')
                    AND d.decision IN ('admitting','admitted')))) LIMIT 1""",
            (run_id, request_id, session_id, owner),
        ).fetchone()
        if row is None:
            return
        duplicate = connection.execute(
            "SELECT 1 FROM run_events WHERE run_id=? AND kind='voice.live.action.tool_effect' "
            "AND json_extract(detail_json, '$.tool_call_id')=? LIMIT 1", (run_id, tool_call_id),
        ).fetchone()
        if duplicate:
            return
        store._append_event(connection, session_id=session_id, run_id=run_id,
            kind="voice.live.action.tool_effect", status="success", summary="Phone action effect observed",
            detail={"call_id": row["call_id"], "request_id": request_id,
                    "tool_call_id": tool_call_id, "workspace_dir": str(workspace_dir),
                    "effect_receipt": json.loads(json.dumps(dict(receipt)))})
