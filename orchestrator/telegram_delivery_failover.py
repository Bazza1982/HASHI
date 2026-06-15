from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from telegram.error import RetryAfter

DEFAULT_FAILOVER_AGENT = "lin_yueru"
DEFAULT_WARNING_REMINDER_SECONDS = 600
DEFAULT_WATCHER_POLL_SECONDS = 60

_HEALTH_STATE_LOCK = asyncio.Lock()


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone().isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def preview_preferences_path(runtime: Any) -> Path:
    return Path(runtime.workspace_dir) / "state" / "runtime_preferences.json"


def preview_override(runtime: Any) -> bool | None:
    path = preview_preferences_path(runtime)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    value = payload.get("answer_stream_preview")
    if isinstance(value, bool):
        return value
    return None


def effective_preview_enabled(runtime: Any) -> bool:
    override = preview_override(runtime)
    if override is not None:
        return override
    extra = getattr(getattr(runtime, "config", None), "extra", {}) or {}
    return bool(extra.get("answer_stream_preview", True))


def preview_status(runtime: Any) -> tuple[bool, str]:
    override = preview_override(runtime)
    if override is not None:
        return override, "persisted override"
    extra = getattr(getattr(runtime, "config", None), "extra", {}) or {}
    return bool(extra.get("answer_stream_preview", True)), "config default"


def set_preview_enabled(runtime: Any, enabled: bool) -> None:
    path = preview_preferences_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "answer_stream_preview": bool(enabled)}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def delivery_state_path(runtime_or_kernel: Any) -> Path:
    global_cfg = getattr(runtime_or_kernel, "global_config", None) or getattr(runtime_or_kernel, "global_cfg", None)
    root = Path(getattr(global_cfg, "project_root", "."))
    return root / "state" / "telegram_delivery_health.json"


def _load_health_state_sync(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "agents": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "agents": {}}
    if not isinstance(payload, dict):
        return {"version": 1, "agents": {}}
    payload.setdefault("version", 1)
    payload.setdefault("agents", {})
    if not isinstance(payload["agents"], dict):
        payload["agents"] = {}
    return payload


def _save_health_state_sync(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_health_state(runtime_or_kernel: Any) -> dict[str, Any]:
    return _load_health_state_sync(delivery_state_path(runtime_or_kernel))


def runtime_token_key(runtime: Any) -> str:
    token_key = getattr(getattr(runtime, "config", None), "telegram_token_key", None) or getattr(runtime, "name", "unknown")
    return f"telegram:{token_key}"


def _agent_record(state: dict[str, Any], runtime: Any) -> dict[str, Any]:
    agents = state.setdefault("agents", {})
    record = agents.setdefault(getattr(runtime, "name", "unknown"), {})
    record.setdefault("token_key", runtime_token_key(runtime))
    record.setdefault("status", "healthy")
    record.setdefault("per_chat", {})
    return record


def _find_record_by_token(state: dict[str, Any], token_key: str) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    for agent_name, record in (state.get("agents") or {}).items():
        if str(record.get("token_key") or "") == token_key:
            return agent_name, record
    return None, None


def get_blocked_record(runtime: Any) -> dict[str, Any] | None:
    state = load_health_state(runtime)
    _agent_name, record = _find_record_by_token(state, runtime_token_key(runtime))
    if not record:
        return None
    if str(record.get("status")) in {"blocked", "recovery_due"}:
        return record
    return None


def is_delivery_blocked(runtime: Any) -> bool:
    return get_blocked_record(runtime) is not None


def delivery_status_summary(runtime: Any) -> dict[str, Any] | None:
    state = load_health_state(runtime)
    _agent_name, record = _find_record_by_token(state, runtime_token_key(runtime))
    if not record:
        return None
    if str(record.get("status")) not in {"blocked", "recovery_due"}:
        return None
    return {
        "blocked_until": record.get("blocked_until"),
        "status": record.get("status"),
        "active_failover_agent": record.get("active_failover_agent"),
        "incident_id": record.get("incident_id"),
    }


def warning_reminder_seconds(runtime: Any) -> int:
    extra = ((getattr(getattr(runtime, "orchestrator", None), "raw_config", None) or {}).get("global") or {}).get(
        "telegram_delivery_failover",
        {},
    )
    return int(extra.get("warning_reminder_seconds", DEFAULT_WARNING_REMINDER_SECONDS))


def watcher_poll_seconds(kernel: Any) -> int:
    extra = ((getattr(kernel, "raw_config", None) or {}).get("global") or {}).get("telegram_delivery_failover", {})
    return int(extra.get("watcher_poll_seconds", DEFAULT_WATCHER_POLL_SECONDS))


def configured_default_failover_agent(runtime: Any) -> str:
    raw_cfg = getattr(getattr(runtime, "orchestrator", None), "raw_config", None) or {}
    global_cfg = (raw_cfg.get("global") or {}).get("telegram_delivery_failover", {})
    return str(global_cfg.get("default_agent") or DEFAULT_FAILOVER_AGENT)


def _undelivered_dir(runtime: Any) -> Path:
    return Path(runtime.workspace_dir) / "undelivered"


def persist_undelivered_response(
    runtime: Any,
    *,
    request_id: str | None,
    chat_id: int | None,
    text: str,
    purpose: str,
    incident_id: str | None,
    retry_after_s: int | None,
    blocked_until: str | None,
    failover_agent: str | None,
) -> Path | None:
    if not text:
        return None
    req = str(request_id or f"undelivered-{int(_now().timestamp())}")
    folder = _undelivered_dir(runtime)
    folder.mkdir(parents=True, exist_ok=True)
    md_path = folder / f"{req}.md"
    json_path = folder / f"{req}.json"
    md_path.write_text(text, encoding="utf-8")
    payload = {
        "request_id": req,
        "chat_id": chat_id,
        "source_agent": getattr(runtime, "name", None),
        "incident_id": incident_id,
        "delivery_purpose": purpose,
        "backend_completed_at": _iso(_now()),
        "retry_after_s": retry_after_s,
        "blocked_until": blocked_until,
        "failover_agent": failover_agent,
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "markdown_path": str(md_path),
    }
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return md_path


def _runtime_candidates(source_runtime: Any) -> list[Any]:
    orchestrator = getattr(source_runtime, "orchestrator", None)
    if orchestrator is None:
        return []
    return [
        rt
        for rt in getattr(orchestrator, "runtimes", [])
        if getattr(rt, "startup_success", False)
    ]


def _eligible_failover_runtime(source_runtime: Any, candidate: Any, *, blocked_tokens: set[str]) -> bool:
    if candidate is source_runtime:
        return False
    if getattr(candidate, "name", None) == getattr(source_runtime, "name", None):
        return False
    if not getattr(candidate, "telegram_connected", False):
        return False
    if str(getattr(candidate, "token", "") or "") == "WORKBENCH_ONLY_NO_TOKEN":
        return False
    if runtime_token_key(candidate) in blocked_tokens:
        return False
    if runtime_token_key(candidate) == runtime_token_key(source_runtime):
        return False
    return True


def _select_failover_runtime(source_runtime: Any, *, preferred_name: str | None = None, exclude_names: set[str] | None = None) -> Any | None:
    exclude_names = exclude_names or set()
    state = load_health_state(source_runtime)
    blocked_tokens = {
        str(record.get("token_key") or "")
        for record in (state.get("agents") or {}).values()
        if str(record.get("status") or "") in {"blocked", "recovery_due"}
    }
    candidates = _runtime_candidates(source_runtime)
    if preferred_name:
        for candidate in candidates:
            if candidate.name == preferred_name and candidate.name not in exclude_names:
                if _eligible_failover_runtime(source_runtime, candidate, blocked_tokens=blocked_tokens):
                    return candidate
    for candidate in candidates:
        if candidate.name in exclude_names:
            continue
        if _eligible_failover_runtime(source_runtime, candidate, blocked_tokens=blocked_tokens):
            return candidate
    return None


def _warn_text(
    *,
    source_agent: str,
    request_id: str | None,
    retry_after_s: int | None,
    blocked_until: str | None,
    response_path: Path | None,
    failover_agent: str | None,
) -> str:
    lines = [
        "Delivery warning from HASHI2:",
        "",
        f"{source_agent} generated a response, but Telegram delivery is flood-limited.",
    ]
    if request_id:
        lines.append("")
        lines.append(f"Request: {request_id}")
    if retry_after_s is not None:
        lines.append(f"Retry after: {retry_after_s}s")
    if blocked_until:
        lines.append(f"Blocked until: {blocked_until}")
    if response_path is not None:
        lines.append(f"Saved response: {response_path}")
    lines.extend(
        [
            "",
            f"Please use {failover_agent or DEFAULT_FAILOVER_AGENT} or Workbench until {source_agent}'s Telegram delivery recovers.",
        ]
    )
    return "\n".join(lines)


def _recovery_text(source_agent: str) -> str:
    return (
        "Delivery recovered:\n\n"
        f"{source_agent}'s Telegram delivery block has expired.\n"
        f"You can continue using {source_agent} normally."
    )


async def _send_direct(runtime: Any, *, chat_id: int, text: str) -> None:
    await runtime.app.bot.send_message(chat_id=chat_id, text=text)


async def _maybe_send_warning(
    source_runtime: Any,
    *,
    chat_id: int | None,
    record: dict[str, Any],
    request_id: str | None,
    response_path: Path | None,
) -> None:
    if chat_id is None:
        return
    chat_key = str(chat_id)
    per_chat = record.setdefault("per_chat", {})
    entry = per_chat.setdefault(chat_key, {})
    now = _now()
    last_warned_at = _parse_iso(entry.get("last_warned_at"))
    if last_warned_at is not None:
        if (now - last_warned_at).total_seconds() < warning_reminder_seconds(source_runtime):
            return
    preferred_name = str(record.get("active_failover_agent") or configured_default_failover_agent(source_runtime))
    chosen = _select_failover_runtime(source_runtime, preferred_name=preferred_name)
    if chosen is None:
        record["failover_failed"] = True
        return
    warning_text = _warn_text(
        source_agent=source_runtime.name,
        request_id=request_id,
        retry_after_s=record.get("retry_after_s"),
        blocked_until=record.get("blocked_until"),
        response_path=response_path,
        failover_agent=chosen.name,
    )
    try:
        await _send_direct(chosen, chat_id=chat_id, text=warning_text)
        record["active_failover_agent"] = chosen.name
        record["failover_failed"] = False
        entry.setdefault("first_warned_at", _iso(now))
        entry["last_warned_at"] = _iso(now)
        entry["last_warning_request_id"] = request_id
    except RetryAfter:
        alternate = _select_failover_runtime(
            source_runtime,
            exclude_names={chosen.name},
        )
        if alternate is None:
            record["failover_failed"] = True
            return
        try:
            await _send_direct(alternate, chat_id=chat_id, text=warning_text.replace(chosen.name, alternate.name))
            record["active_failover_agent"] = alternate.name
            record["failover_failed"] = False
            entry.setdefault("first_warned_at", _iso(now))
            entry["last_warned_at"] = _iso(now)
            entry["last_warning_request_id"] = request_id
        except RetryAfter:
            record["failover_failed"] = True


async def handle_blocked_send(
    runtime: Any,
    *,
    chat_id: int | None,
    request_id: str | None,
    purpose: str,
    text: str | None = None,
) -> bool:
    async with _HEALTH_STATE_LOCK:
        path = delivery_state_path(runtime)
        state = _load_health_state_sync(path)
        _agent_name, record = _find_record_by_token(state, runtime_token_key(runtime))
        if not record or str(record.get("status") or "") not in {"blocked", "recovery_due"}:
            return False
        response_path = None
        if text:
            response_path = persist_undelivered_response(
                runtime,
                request_id=request_id,
                chat_id=chat_id,
                text=text,
                purpose=purpose,
                incident_id=record.get("incident_id"),
                retry_after_s=record.get("retry_after_s"),
                blocked_until=record.get("blocked_until"),
                failover_agent=record.get("active_failover_agent"),
            )
            if chat_id is not None:
                per_chat = record.setdefault("per_chat", {}).setdefault(str(chat_id), {})
                requests = per_chat.setdefault("undelivered_request_ids", [])
                if request_id and request_id not in requests:
                    requests.append(request_id)
        await _maybe_send_warning(runtime, chat_id=chat_id, record=record, request_id=request_id, response_path=response_path)
        _save_health_state_sync(path, state)
        return True


async def handle_retry_after(
    runtime: Any,
    *,
    exc: RetryAfter,
    chat_id: int | None,
    request_id: str | None,
    purpose: str,
    text: str | None = None,
) -> dict[str, Any]:
    retry_after_s = int(getattr(exc, "retry_after", 0) or 0)
    blocked_until_dt = _now() + timedelta(seconds=max(retry_after_s, 1))
    runtime_name = getattr(runtime, "name", "unknown")
    incident_id = f"tg-{runtime_name}-{_now().strftime('%Y%m%dT%H%M%S')}"
    async with _HEALTH_STATE_LOCK:
        path = delivery_state_path(runtime)
        state = _load_health_state_sync(path)
        record = _agent_record(state, runtime)
        if record.get("incident_id"):
            incident_id = str(record.get("incident_id"))
        record["status"] = "blocked"
        record["token_key"] = runtime_token_key(runtime)
        record["blocked_until"] = _iso(blocked_until_dt)
        record["retry_after_s"] = retry_after_s
        record["incident_id"] = incident_id
        record["last_incident_at"] = _iso(_now())
        record["last_request_id"] = request_id
        response_path = None
        if text:
            response_path = persist_undelivered_response(
                runtime,
                request_id=request_id,
                chat_id=chat_id,
                text=text,
                purpose=purpose,
                incident_id=incident_id,
                retry_after_s=retry_after_s,
                blocked_until=record["blocked_until"],
                failover_agent=record.get("active_failover_agent"),
            )
        if chat_id is not None:
            per_chat = record.setdefault("per_chat", {}).setdefault(str(chat_id), {})
            requests = per_chat.setdefault("undelivered_request_ids", [])
            if request_id and request_id not in requests:
                requests.append(request_id)
        await _maybe_send_warning(runtime, chat_id=chat_id, record=record, request_id=request_id, response_path=response_path)
        _save_health_state_sync(path, state)
        return record


async def delivery_health_watcher(kernel: Any) -> None:
    while True:
        await asyncio.sleep(max(5, watcher_poll_seconds(kernel)))
        try:
            await _tick_recovery(kernel)
        except asyncio.CancelledError:
            raise
        except Exception:
            continue


async def _tick_recovery(kernel: Any) -> None:
    async with _HEALTH_STATE_LOCK:
        path = delivery_state_path(kernel)
        state = _load_health_state_sync(path)
        changed = False
        runtimes = {getattr(rt, "name", None): rt for rt in getattr(kernel, "runtimes", [])}
        for agent_name, record in (state.get("agents") or {}).items():
            status = str(record.get("status") or "")
            if status != "blocked":
                continue
            blocked_until = _parse_iso(record.get("blocked_until"))
            if blocked_until is None or _now() < blocked_until:
                continue
            runtime = runtimes.get(agent_name)
            if runtime is None or not getattr(runtime, "telegram_connected", False):
                continue
            record["status"] = "recovery_due"
            recovery_sent = True
            for chat_key, chat_state in (record.get("per_chat") or {}).items():
                if chat_state.get("recovery_notice_sent_at"):
                    continue
                try:
                    await _send_direct(runtime, chat_id=int(chat_key), text=_recovery_text(agent_name))
                    chat_state["recovery_notice_sent_at"] = _iso(_now())
                except RetryAfter as exc:
                    retry_after_s = int(getattr(exc, "retry_after", 0) or 0)
                    record["status"] = "blocked"
                    record["retry_after_s"] = retry_after_s
                    record["blocked_until"] = _iso(_now() + timedelta(seconds=max(retry_after_s, 1)))
                    recovery_sent = False
                    break
            if recovery_sent:
                record["status"] = "healthy"
                record["active_failover_agent"] = None
                record["failover_failed"] = False
                changed = True
            else:
                changed = True
        if changed:
            _save_health_state_sync(path, state)
