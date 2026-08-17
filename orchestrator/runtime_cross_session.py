from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping

from orchestrator import runtime_retry, runtime_turn_context


STATE_VERSION = 2
STATE_FILENAME = "cross_session_receipts.json"
MAX_RECEIPTS = 64
MAX_CONTEXT_RECEIPTS = 6
MAX_STORED_PROMPT_CHARS = 20_000
MAX_STORED_RESPONSE_CHARS = 24_000
MAX_CONTEXT_PROMPT_CHARS = 5_000
MAX_CONTEXT_RESPONSE_CHARS = 8_000

HER_SESSION_SCOPE_ISOLATED = "isolated_per_run"
HER_SESSION_SCOPE_ISOLATED_RESUME = "isolated_resume"

_DIRECT_REPLY_SOURCES = frozenset(
    {"telegram", "text", "voice", "voice_transcript"}
)
_SKIP_CONTEXT_SOURCES = frozenset(
    {
        "startup",
        "system",
        "session_reset",
        runtime_retry.RETRY_HANDOFF_SOURCE,
    }
)
_INCOMPLETE_STOP_REASONS = frozenset(
    {"budget_exhausted", "max_iterations", "no_final_text"}
)
_REPLY_INVITATION_RE = re.compile(
    r"\b(?:reply|respond|choose|select)\b|"
    r"回复|回覆|选择|選擇|回字母|返信|選ん|選択",
    re.IGNORECASE,
)
_CHOICE_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?([A-Z])(?:\*\*)?\s*(?:[—–:\-.)]|$)",
    re.MULTILINE,
)
_CHOICE_TABLE_RE = re.compile(r"^\s*\|\s*([A-Z])\s*\|", re.MULTILINE)
_CHOICE_REPLY_RE = re.compile(
    r"(?:comment|choose|select|选|選|评论|評論)?\s*"
    r"([A-Z](?:\s*[,，/、&+]\s*[A-Z])*)",
    re.IGNORECASE,
)
_QUESTION_END_RE = re.compile(r"[?？](?:[^\w]|_)*\Z")
_SHORT_ANSWER_RE = re.compile(
    r"(?:yes|no|ok|okay|sure|do it|go ahead|"
    r"是|否|好|好的|可以|不|不要|继续|繼續|はい|いいえ)",
    re.IGNORECASE,
)


def _value(candidate: Any, key: str, default: Any = None) -> Any:
    if isinstance(candidate, Mapping):
        return candidate.get(key, default)
    return getattr(candidate, key, default)


def _workspace_dir(runtime: Any) -> Path | None:
    value = getattr(runtime, "workspace_dir", None)
    if value is None:
        value = getattr(getattr(runtime, "config", None), "workspace_dir", None)
    if value is None:
        return None
    try:
        return Path(value)
    except TypeError:
        return None


def receipt_state_path(runtime: Any) -> Path | None:
    workspace = _workspace_dir(runtime)
    return workspace / "state" / STATE_FILENAME if workspace is not None else None


def _empty_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "next_sequence": 1, "receipts": []}


def _read_state(runtime: Any) -> dict[str, Any]:
    path = receipt_state_path(runtime)
    if path is None or not path.exists():
        return _empty_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty_state()
    if not isinstance(payload, dict) or not isinstance(payload.get("receipts"), list):
        return _empty_state()
    try:
        next_sequence = max(1, int(payload.get("next_sequence") or 1))
    except (TypeError, ValueError):
        next_sequence = 1
    return {
        "version": STATE_VERSION,
        "next_sequence": next_sequence,
        "receipts": [item for item in payload["receipts"] if isinstance(item, dict)],
    }


def _write_state(runtime: Any, state: Mapping[str, Any]) -> bool:
    path = receipt_state_path(runtime)
    if path is None:
        return False
    try:
        receipts = list(state.get("receipts") or [])
        active = [item for item in receipts if bool(item.get("active"))]
        inactive = [item for item in receipts if not bool(item.get("active"))]
        inactive.sort(key=lambda item: int(item.get("last_sequence") or 0))
        inactive_limit = max(0, MAX_RECEIPTS - len(active))
        keep_inactive = inactive[-inactive_limit:] if inactive_limit else []
        kept = active + keep_inactive
        kept.sort(key=lambda item: int(item.get("last_sequence") or 0))
        payload = {
            "version": STATE_VERSION,
            "next_sequence": max(1, int(state.get("next_sequence") or 1)),
            "receipts": kept,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        path.chmod(0o600)
        return True
    except Exception as exc:
        logger = getattr(runtime, "logger", None)
        if logger is not None:
            logger.warning(f"Could not persist cross-session receipt state: {exc}")
        return False


def _bounded_text(value: Any, limit: int) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if len(text) <= limit:
        return text
    head = max(1, limit // 2)
    tail = max(1, limit - head - 38)
    return (
        text[:head].rstrip()
        + "\n…[cross-session text truncated]…\n"
        + text[-tail:].lstrip()
    )


def _chat_matches(receipt: Mapping[str, Any], chat_id: Any) -> bool:
    return str(receipt.get("chat_id")) == str(chat_id)


def _request_meta(runtime: Any, request_id: str) -> dict[str, Any]:
    registry = getattr(runtime, "_request_meta_by_id", None)
    if isinstance(registry, dict):
        candidate = registry.get(str(request_id or ""))
        if isinstance(candidate, dict):
            return candidate
    current = getattr(runtime, "current_request_meta", None)
    if isinstance(current, dict) and str(current.get("request_id") or "") == str(
        request_id or ""
    ):
        return current
    return {}


def _stream_metadata(response: Any) -> dict[str, Any]:
    value = _value(response, "stream_metadata", None)
    return dict(value) if isinstance(value, Mapping) else {}


def _completion_status(response: Any, error: str) -> tuple[str, str, str]:
    metadata = _stream_metadata(response)
    completion = str(metadata.get("claw_completion_status") or "").strip().lower()
    stop_reason = (
        str(
            metadata.get("claw_stop_reason")
            or _value(response, "stop_reason", "")
            or ""
        )
        .strip()
        .lower()
    )
    if error or not bool(_value(response, "is_success", not error)):
        return "failed", completion or "failed", stop_reason or "backend_error"
    if completion == "incomplete" or stop_reason in _INCOMPLETE_STOP_REASONS:
        return "incomplete", completion or "incomplete", stop_reason or "incomplete"
    return "completed", completion or "completed", stop_reason or "end_turn"


def _choice_labels(text: str) -> list[str]:
    if not _REPLY_INVITATION_RE.search(text):
        return []
    labels = {
        match.group(1).upper()
        for pattern in (_CHOICE_LINE_RE, _CHOICE_TABLE_RE)
        for match in pattern.finditer(text)
    }
    return sorted(labels)


def _pending_interaction(
    assistant_text: str,
    *,
    status: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any] | None:
    structured = metadata.get("pending_interaction")
    if not isinstance(structured, Mapping):
        ultra = metadata.get("her_ultra")
        structured = (
            ultra.get("pending_interaction") if isinstance(ultra, Mapping) else None
        )
    if isinstance(structured, Mapping):
        kind = str(structured.get("kind") or "").strip().lower()
        interaction_id = str(structured.get("interaction_id") or "").strip()
        question = _bounded_text(structured.get("question"), 4_000)
        raw_options = structured.get("options")
        options = (
            [_bounded_text(option, 1_000) for option in raw_options if str(option).strip()]
            if isinstance(raw_options, (list, tuple))
            else []
        )
        if kind == "choice":
            raw_labels = structured.get("labels")
            raw_labels = raw_labels if isinstance(raw_labels, (list, tuple)) else []
            labels = sorted(
                {
                    str(label).strip().upper()
                    for label in raw_labels
                    if str(label).strip()
                }
            )
            if labels:
                pending = {"kind": "choice", "labels": labels}
                if question:
                    pending["question"] = question
                if options:
                    pending["options"] = options
                if interaction_id:
                    pending["interaction_id"] = interaction_id
                return pending
        if kind == "continuation":
            pending = {
                "kind": "continuation",
                "token": str(structured.get("token") or "CONTINUE").upper(),
            }
            if interaction_id:
                pending["interaction_id"] = interaction_id
            return pending
        if kind in {"confirmation", "question"}:
            pending = {"kind": "question"}
            if question:
                pending["question"] = question
            if options:
                pending["options"] = options
            if interaction_id:
                pending["interaction_id"] = interaction_id
            return pending
    labels = _choice_labels(assistant_text)
    if labels:
        return {"kind": "choice", "labels": labels}
    recommendation = str(metadata.get("recommended_action") or "").strip().upper()
    if recommendation == "CONTINUE" or status == "incomplete":
        return {"kind": "continuation", "token": "CONTINUE"}
    if _QUESTION_END_RE.search(assistant_text.rstrip()):
        return {"kind": "question"}
    return None


def _current_model(runtime: Any) -> str:
    backend = getattr(
        getattr(runtime, "backend_manager", None), "current_backend", None
    )
    claw_model = getattr(backend, "_claw_model", None)
    if callable(claw_model):
        try:
            return str(claw_model() or "").strip()
        except Exception:
            pass
    getter = getattr(runtime, "get_current_model", None)
    if callable(getter):
        try:
            return str(getter() or "").strip()
        except Exception:
            pass
    return ""


def _next_sequence(state: dict[str, Any]) -> int:
    try:
        sequence = max(1, int(state.get("next_sequence") or 1))
    except (TypeError, ValueError):
        sequence = 1
    state["next_sequence"] = sequence + 1
    return sequence


def _bound_receipt_id(runtime: Any, item: Any) -> str:
    attached = _value(item, "_cross_session_receipt", None)
    if isinstance(attached, Mapping) and attached.get("receipt_id"):
        return str(attached["receipt_id"])
    meta = _request_meta(runtime, str(_value(item, "request_id", "") or ""))
    attached = meta.get("cross_session_receipt")
    if isinstance(attached, Mapping) and attached.get("receipt_id"):
        return str(attached["receipt_id"])
    return ""


def _should_record(runtime: Any, item: Any, response: Any) -> bool:
    source = str(_value(item, "source", "") or "").strip().lower()
    if source.startswith("scheduler") or _bound_receipt_id(runtime, item):
        return True
    metadata = _stream_metadata(response)
    scope = str(metadata.get("her_session_scope") or "").strip().lower()
    return scope in {HER_SESSION_SCOPE_ISOLATED, HER_SESSION_SCOPE_ISOLATED_RESUME}


def _supersede_active_receipts(
    receipts: list[dict[str, Any]],
    *,
    chat_id: Any,
    current: Mapping[str, Any] | None,
    now: float,
    resolved_by: str,
) -> bool:
    changed = False
    for receipt in receipts:
        if receipt is current:
            continue
        if _chat_matches(receipt, chat_id) and bool(receipt.get("active")):
            receipt["active"] = False
            receipt["resolved_at"] = now
            receipt["resolved_by"] = resolved_by
            changed = True
    return changed


def record_turn_result(
    runtime: Any,
    item: Any,
    *,
    assistant_text: str = "",
    response: Any = None,
    error: str = "",
    delivered: bool,
    completion_path: str,
) -> dict[str, Any] | None:
    """Persist a no-op receipt for a turn outside the primary backend session."""
    if delivered:
        runtime_turn_context.record_delivered_turn(
            runtime,
            item,
            assistant_text or error,
        )
    metadata = _stream_metadata(response)
    status, completion_status, stop_reason = _completion_status(response, error)
    pending = _pending_interaction(
        assistant_text,
        status=status,
        metadata=metadata,
    )
    if not (_should_record(runtime, item, response) or pending is not None):
        if not delivered or pending is None:
            return None
        state = _read_state(runtime)
        changed = _supersede_active_receipts(
            state["receipts"],
            chat_id=_value(item, "chat_id", None),
            current=None,
            now=time.time(),
            resolved_by=f"newer_primary_interaction:{_value(item, 'request_id', 'unknown')}",
        )
        if changed:
            _write_state(runtime, state)
        return None

    state = _read_state(runtime)
    receipts = state["receipts"]
    bound_id = _bound_receipt_id(runtime, item)
    receipt = next(
        (
            candidate
            for candidate in receipts
            if candidate.get("receipt_id") == bound_id
        ),
        None,
    )
    now = time.time()
    sequence = _next_sequence(state)
    session_id = str(metadata.get("her_session_id") or "").strip()
    session_scope = str(metadata.get("her_session_scope") or "").strip()
    model = str(metadata.get("her_model") or _current_model(runtime) or "").strip()
    backend = str(getattr(getattr(runtime, "config", None), "active_backend", "") or "")

    if receipt is None:
        receipt_id = (
            f"{getattr(runtime, 'name', 'agent')}:"
            f"{_value(item, 'request_id', 'request')}:{time.time_ns()}"
        )
        receipt = {
            "receipt_id": receipt_id,
            "request_id": str(_value(item, "request_id", "") or ""),
            "chat_id": _value(item, "chat_id", None),
            "source": str(_value(item, "source", "") or ""),
            "summary": _bounded_text(_value(item, "summary", ""), 1_000),
            "task_prompt": _bounded_text(
                _value(item, "prompt", ""), MAX_STORED_PROMPT_CHARS
            ),
            "created_at": now,
        }
        receipts.append(receipt)
    else:
        receipt["last_user_text"] = _bounded_text(
            _value(item, "prompt", ""), MAX_STORED_PROMPT_CHARS
        )

    if bound_id and (not delivered or status == "failed"):
        receipt["last_attempt"] = {
            "at": now,
            "status": status,
            "completion_status": completion_status,
            "stop_reason": stop_reason,
            "error": _bounded_text(error, 2_000),
            "delivered": bool(delivered),
        }
        receipt["last_sequence"] = sequence
        _write_state(runtime, state)
        return dict(receipt)

    receipt.update(
        {
            "last_sequence": sequence,
            "updated_at": now,
            "completion_path": str(completion_path or "foreground"),
            "backend": backend,
            "model": model,
            "session_id": session_id or str(receipt.get("session_id") or ""),
            "session_scope": session_scope or str(receipt.get("session_scope") or ""),
            "status": status,
            "completion_status": completion_status,
            "stop_reason": stop_reason,
            "assistant_text": _bounded_text(
                assistant_text or error, MAX_STORED_RESPONSE_CHARS
            ),
            "error": _bounded_text(error, 2_000),
            "delivered": bool(delivered),
            "pending_interaction": pending,
            "task_status": "awaiting_user" if pending else status,
            "task_checkpoint": (
                dict(metadata["task_checkpoint"])
                if isinstance(metadata.get("task_checkpoint"), Mapping)
                else None
            ),
            "execution_ledger": (
                dict(metadata["execution_ledger"])
                if isinstance(metadata.get("execution_ledger"), Mapping)
                else {"version": 1, "total_entries": 0, "entries": []}
            ),
            "next_action_after_answer": (
                "Resume the preserved task checkpoint after revalidating external state."
                if pending
                else ""
            ),
            "delivery_receipt": {
                "confirmed": bool(delivered),
                "request_id": str(_value(item, "request_id", "") or ""),
                "completion_path": str(completion_path or "foreground"),
                "recorded_at": now,
            },
            "active": bool(delivered and pending),
        }
    )
    if delivered and not bound_id:
        _supersede_active_receipts(
            receipts,
            chat_id=receipt.get("chat_id"),
            current=receipt,
            now=now,
            resolved_by=f"superseded_by:{receipt['receipt_id']}",
        )
    if receipt["active"]:
        receipt.pop("resolved_at", None)
        receipt.pop("resolved_by", None)
    elif bound_id and delivered:
        receipt["resolved_at"] = now
        receipt["resolved_by"] = str(_value(item, "request_id", "") or "reply")

    if not _write_state(runtime, state):
        return None
    return dict(receipt)


def _parse_choice_reply(text: str) -> list[str]:
    candidate = str(text or "").strip().strip(".?!。！？~～")
    match = _CHOICE_REPLY_RE.fullmatch(candidate)
    if not match:
        return []
    return re.findall(r"[A-Z]", match.group(1).upper())


def _short_answer(text: str) -> bool:
    candidate = str(text or "").strip().strip(".?!。！？~～")
    return bool(candidate and _SHORT_ANSWER_RE.fullmatch(candidate))


def _matching_receipt(runtime: Any, item: Any) -> tuple[dict[str, Any] | None, str]:
    source = str(_value(item, "source", "") or "").strip().lower()
    if bool(_value(item, "silent", False)) or source not in _DIRECT_REPLY_SOURCES:
        return None, ""
    text = str(_value(item, "prompt", "") or "")
    if not text.strip() or text.lstrip().startswith("/"):
        return None, ""
    continuation = runtime_retry.is_explicit_continuation(text)
    labels = _parse_choice_reply(text)
    short_answer = _short_answer(text)

    state = _read_state(runtime)
    candidates = [
        receipt
        for receipt in state["receipts"]
        if bool(receipt.get("active"))
        and bool(receipt.get("delivered"))
        and _chat_matches(receipt, _value(item, "chat_id", None))
        and isinstance(receipt.get("pending_interaction"), Mapping)
    ]
    candidates.sort(
        key=lambda receipt: int(receipt.get("last_sequence") or 0), reverse=True
    )
    for receipt in candidates:
        pending = receipt["pending_interaction"]
        kind = str(pending.get("kind") or "")
        if continuation and kind == "continuation":
            return receipt, "continuation"
        if labels and kind == "choice":
            offered = {str(label).upper() for label in pending.get("labels") or []}
            if set(labels).issubset(offered):
                return receipt, "choice"
        if short_answer and kind == "question":
            return receipt, "answer"
        if kind in {"choice", "question"}:
            return receipt, "answer"
    return None, ""


def capture_reply_target(runtime: Any, item: Any) -> dict[str, str] | None:
    """Freeze the eligible isolated reply target at user-message enqueue time."""
    if bool(_value(item, "_cross_session_target_captured", False)):
        attached = _value(item, "_cross_session_receipt", None)
        return dict(attached) if isinstance(attached, Mapping) else None

    receipt, reply_kind = _matching_receipt(runtime, item)
    try:
        setattr(item, "_cross_session_target_captured", True)
    except Exception:
        pass
    if receipt is None:
        return None

    binding = {
        "receipt_id": str(receipt.get("receipt_id") or ""),
        "request_id": str(receipt.get("request_id") or ""),
        "reply_kind": reply_kind,
        "session_id": str(receipt.get("session_id") or ""),
    }
    try:
        setattr(item, "_cross_session_receipt", binding)
        setattr(item, "_cross_session_receipt_snapshot", dict(receipt))
    except Exception:
        pass
    return binding


def _can_resume_exact_session(runtime: Any, receipt: Mapping[str, Any]) -> bool:
    if not _uses_her_backend(runtime):
        return False
    if not str(receipt.get("session_id") or "").strip():
        return False
    receipt_model = str(receipt.get("model") or "").strip()
    current_model = _current_model(runtime)
    return not receipt_model or not current_model or receipt_model == current_model


def _uses_her_backend(runtime: Any) -> bool:
    return (
        str(getattr(getattr(runtime, "config", None), "active_backend", "") or "")
        .strip()
        .lower()
        == "her"
    )


def prepare_reply_binding(runtime: Any, item: Any, effective_prompt: str) -> str:
    """Apply only the isolated reply target captured when the message arrived."""
    if not bool(_value(item, "_cross_session_target_captured", False)):
        capture_reply_target(runtime, item)
    binding = _value(item, "_cross_session_receipt", None)
    receipt = _value(item, "_cross_session_receipt_snapshot", None)
    if not isinstance(binding, Mapping) or not isinstance(receipt, Mapping):
        return effective_prompt
    binding = dict(binding)
    reply_kind = str(binding.get("reply_kind") or "")
    meta = _request_meta(runtime, str(_value(item, "request_id", "") or ""))
    if meta:
        meta["cross_session_receipt"] = binding
        if _uses_her_backend(runtime):
            meta["session_scope"] = HER_SESSION_SCOPE_ISOLATED
            if _can_resume_exact_session(runtime, receipt):
                meta["session_scope"] = HER_SESSION_SCOPE_ISOLATED_RESUME
                meta["resume_session_id"] = binding["session_id"]
    logger = getattr(runtime, "logger", None)
    if logger is not None:
        logger.info(
            f"Bound request {_value(item, 'request_id', '')} to cross-session "
            f"receipt {binding['receipt_id']} ({reply_kind}, "
            f"exact_resume={bool(meta.get('resume_session_id')) if meta else False})"
        )

    task_prompt = _bounded_text(receipt.get("task_prompt"), MAX_CONTEXT_PROMPT_CHARS)
    assistant_text = _bounded_text(
        receipt.get("assistant_text"), MAX_CONTEXT_RESPONSE_CHARS
    )
    return (
        "[HASHI cross-session reply binding — authoritative referent resolution]\n"
        "The runtime bound the current user reply to the newest delivered, "
        "unresolved turn shown below. Do not interpret it as a reply to older choices "
        "or questions in the primary session. The receipt is context only; perform only "
        "the action authorized by the current reply and the original task scope. Preserve "
        "verified progress and verify external state before repeating side effects.\n"
        f"Receipt ID: {binding['receipt_id']}\n"
        f"Reply kind: {reply_kind}\n"
        f"Original turn source: {receipt.get('source') or 'unknown'}\n"
        f"Original turn status: {receipt.get('status') or 'unknown'}\n"
        f"Task status: {receipt.get('task_status') or receipt.get('status') or 'unknown'}\n"
        "Preserved task checkpoint:\n"
        f"{json.dumps(receipt.get('task_checkpoint'), ensure_ascii=False)}\n\n"
        "Execution receipt index:\n"
        f"{json.dumps(receipt.get('execution_ledger'), ensure_ascii=False)}\n\n"
        f"Original task:\n{task_prompt}\n\n"
        f"Assistant message delivered to the user:\n{assistant_text}\n\n"
        "Current user reply:\n"
        f"{str(_value(item, 'prompt', '') or '').strip()}\n"
        "[End HASHI cross-session reply binding]"
    )


def context_section(runtime: Any, item: Any) -> list[tuple[str, str]]:
    """Inject recent isolated-turn receipts into fixed and flex user turns."""
    source = str(_value(item, "source", "") or "").strip().lower()
    if source.startswith("scheduler") or source in _SKIP_CONTEXT_SOURCES:
        return []
    state = _read_state(runtime)
    receipts = [
        receipt
        for receipt in state["receipts"]
        if _chat_matches(receipt, _value(item, "chat_id", None))
    ]
    if not receipts:
        return []
    receipts.sort(key=lambda receipt: int(receipt.get("last_sequence") or 0))
    active = [receipt for receipt in receipts if bool(receipt.get("active"))]
    recent = [receipt for receipt in receipts if receipt not in active]
    active = active[-MAX_CONTEXT_RECEIPTS:]
    recent_limit = max(0, MAX_CONTEXT_RECEIPTS - len(active))
    selected = active + (recent[-recent_limit:] if recent_limit else [])
    selected.sort(key=lambda receipt: int(receipt.get("last_sequence") or 0))

    parts = [
        "These records describe turns completed outside the primary backend session. "
        "They are read-only context and must not trigger work by themselves. If the "
        "current request is a reply, obey any explicit HASHI cross-session binding "
        "in the current request and do not resolve it against older primary-session choices."
    ]
    for receipt in selected:
        pending = receipt.get("pending_interaction")
        pending_text = json.dumps(pending, ensure_ascii=False) if pending else "none"
        parts.append(
            "\n".join(
                (
                    f"## Receipt {receipt.get('receipt_id')}",
                    f"source={receipt.get('source') or 'unknown'}; "
                    f"summary={receipt.get('summary') or 'none'}; "
                    f"status={receipt.get('status') or 'unknown'}; "
                    f"task_status={receipt.get('task_status') or receipt.get('status') or 'unknown'}; "
                    f"delivered={bool(receipt.get('delivered'))}; "
                    f"active={bool(receipt.get('active'))}",
                    f"pending_interaction={pending_text}",
                    "Task checkpoint:\n"
                    + json.dumps(
                        receipt.get("task_checkpoint"), ensure_ascii=False
                    ),
                    "Execution receipt index:\n"
                    + json.dumps(
                        receipt.get("execution_ledger"), ensure_ascii=False
                    ),
                    "Original task:\n"
                    + _bounded_text(
                        receipt.get("task_prompt"), MAX_CONTEXT_PROMPT_CHARS
                    ),
                    "Assistant result:\n"
                    + _bounded_text(
                        receipt.get("assistant_text"), MAX_CONTEXT_RESPONSE_CHARS
                    ),
                )
            )
        )
    return [("CROSS-SESSION TURN RECEIPTS", "\n\n".join(parts))]


def load_receipts(runtime: Any) -> list[dict[str, Any]]:
    """Return a defensive copy for diagnostics and tests."""
    return [dict(receipt) for receipt in _read_state(runtime)["receipts"]]
