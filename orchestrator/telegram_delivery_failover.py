from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from telegram.error import RetryAfter

from orchestrator import telegram_stream_policy

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
    return telegram_stream_policy.preferences_path(runtime)


def preview_override(runtime: Any) -> bool | None:
    payload = telegram_stream_policy.load_preferences(runtime)
    stream = payload.get("telegram_stream")
    value = stream.get("preview") if isinstance(stream, dict) else None
    if not isinstance(value, bool):
        value = payload.get("answer_stream_preview")
    if isinstance(value, bool):
        return value
    return None


def effective_preview_enabled(runtime: Any) -> bool:
    return telegram_stream_policy.get_policy(runtime).preview_enabled


def preview_status(runtime: Any) -> tuple[bool, str]:
    policy = telegram_stream_policy.get_policy(runtime)
    if not policy.enabled:
        return False, f"stream disabled ({policy.source})"
    if not policy.placeholder_enabled:
        return False, "placeholder disabled"
    return policy.preview_enabled, policy.component_sources["preview"]


def set_preview_enabled(runtime: Any, enabled: bool) -> None:
    telegram_stream_policy.set_policy_value(runtime, "preview", enabled)


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
    return _select_failover_runtime_from_state(
        source_runtime,
        load_health_state(source_runtime),
        preferred_name=preferred_name,
        exclude_names=exclude_names,
    )


def _select_failover_runtime_from_state(
    source_runtime: Any,
    state: dict[str, Any],
    *,
    preferred_name: str | None = None,
    exclude_names: set[str] | None = None,
) -> Any | None:
    exclude_names = exclude_names or set()
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


async def _prepare_warning(
    source_runtime: Any,
    *,
    chat_id: int | None,
    request_id: str | None,
    response_path: Path | None,
    exclude_names: set[str] | None = None,
) -> tuple[Any, str] | None:
    if chat_id is None:
        return None
    async with _HEALTH_STATE_LOCK:
        path = delivery_state_path(source_runtime)
        state = _load_health_state_sync(path)
        _agent_name, record = _find_record_by_token(state, runtime_token_key(source_runtime))
        if not record or str(record.get("status") or "") not in {"blocked", "recovery_due"}:
            return None
        chat_key = str(chat_id)
        per_chat = record.setdefault("per_chat", {})
        entry = per_chat.setdefault(chat_key, {})
        now = _now()
        last_warned_at = _parse_iso(entry.get("last_warned_at"))
        if last_warned_at is not None:
            if (now - last_warned_at).total_seconds() < warning_reminder_seconds(source_runtime):
                return None
        preferred_name = str(record.get("active_failover_agent") or configured_default_failover_agent(source_runtime))
        chosen = _select_failover_runtime_from_state(
            source_runtime,
            state,
            preferred_name=preferred_name,
            exclude_names=exclude_names,
        )
        if chosen is None:
            record["failover_failed"] = True
            record["last_failover_error"] = "no eligible failover runtime"
            _save_health_state_sync(path, state)
            return None
        warning_text = _warn_text(
            source_agent=source_runtime.name,
            request_id=request_id,
            retry_after_s=record.get("retry_after_s"),
            blocked_until=record.get("blocked_until"),
            response_path=response_path,
            failover_agent=chosen.name,
        )
        return chosen, warning_text


async def _record_warning_result(
    source_runtime: Any,
    *,
    chat_id: int,
    request_id: str | None,
    failover_agent: str | None,
    success: bool,
    error: Exception | None = None,
) -> None:
    async with _HEALTH_STATE_LOCK:
        path = delivery_state_path(source_runtime)
        state = _load_health_state_sync(path)
        _agent_name, record = _find_record_by_token(state, runtime_token_key(source_runtime))
        if not record:
            return
        entry = record.setdefault("per_chat", {}).setdefault(str(chat_id), {})
        if success:
            now = _now()
            record["active_failover_agent"] = failover_agent
            record["failover_failed"] = False
            record.pop("last_failover_error", None)
            entry.setdefault("first_warned_at", _iso(now))
            entry["last_warned_at"] = _iso(now)
            entry["last_warning_request_id"] = request_id
        else:
            record["failover_failed"] = True
            if error is not None:
                record["last_failover_error"] = f"{type(error).__name__}: {error}"
        _save_health_state_sync(path, state)


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
    prepared = await _prepare_warning(
        source_runtime,
        chat_id=chat_id,
        request_id=request_id,
        response_path=response_path,
    )
    if prepared is None:
        return
    chosen, warning_text = prepared
    try:
        await _send_direct(chosen, chat_id=chat_id, text=warning_text)
        await _record_warning_result(
            source_runtime,
            chat_id=chat_id,
            request_id=request_id,
            failover_agent=chosen.name,
            success=True,
        )
    except RetryAfter:
        alternate_prepared = await _prepare_warning(
            source_runtime,
            chat_id=chat_id,
            request_id=request_id,
            response_path=response_path,
            exclude_names={chosen.name},
        )
        if alternate_prepared is None:
            await _record_warning_result(
                source_runtime,
                chat_id=chat_id,
                request_id=request_id,
                failover_agent=None,
                success=False,
                error=RuntimeError("failover runtime flood-limited and no alternate runtime available"),
            )
            return
        alternate, alternate_warning_text = alternate_prepared
        try:
            await _send_direct(alternate, chat_id=chat_id, text=alternate_warning_text)
            await _record_warning_result(
                source_runtime,
                chat_id=chat_id,
                request_id=request_id,
                failover_agent=alternate.name,
                success=True,
            )
        except Exception as exc:
            await _record_warning_result(
                source_runtime,
                chat_id=chat_id,
                request_id=request_id,
                failover_agent=alternate.name,
                success=False,
                error=exc,
            )
    except Exception as exc:
        await _record_warning_result(
            source_runtime,
            chat_id=chat_id,
            request_id=request_id,
            failover_agent=chosen.name,
            success=False,
            error=exc,
        )


async def handle_blocked_send(
    runtime: Any,
    *,
    chat_id: int | None,
    request_id: str | None,
    purpose: str,
    text: str | None = None,
) -> bool:
    response_path = None
    blocked_record = None
    async with _HEALTH_STATE_LOCK:
        path = delivery_state_path(runtime)
        state = _load_health_state_sync(path)
        _agent_name, record = _find_record_by_token(state, runtime_token_key(runtime))
        if not record or str(record.get("status") or "") not in {"blocked", "recovery_due"}:
            return False
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
        blocked_record = dict(record)
        _save_health_state_sync(path, state)
    if blocked_record is not None:
        await _maybe_send_warning(runtime, chat_id=chat_id, record=blocked_record, request_id=request_id, response_path=response_path)
        return True
    return False


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
    response_path = None
    saved_record: dict[str, Any]
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
        saved_record = dict(record)
        _save_health_state_sync(path, state)
    await _maybe_send_warning(runtime, chat_id=chat_id, record=saved_record, request_id=request_id, response_path=response_path)
    return saved_record


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
    notices: list[tuple[str, Any, int]] = []
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
            for chat_key, chat_state in (record.get("per_chat") or {}).items():
                if chat_state.get("recovery_notice_sent_at"):
                    continue
                notices.append((agent_name, runtime, int(chat_key)))
            if not record.get("per_chat"):
                record["status"] = "healthy"
                record["active_failover_agent"] = None
                record["failover_failed"] = False
            changed = True
        if changed:
            _save_health_state_sync(path, state)
    if not notices:
        return
    results: list[tuple[str, int, str, int | None, Exception | None]] = []
    for agent_name, runtime, chat_id in notices:
        try:
            await _send_direct(runtime, chat_id=chat_id, text=_recovery_text(agent_name))
            results.append((agent_name, chat_id, "sent", None, None))
        except RetryAfter as exc:
            retry_after_s = int(getattr(exc, "retry_after", 0) or 0)
            results.append((agent_name, chat_id, "retry_after", retry_after_s, exc))
        except Exception as exc:
            results.append((agent_name, chat_id, "error", None, exc))
    async with _HEALTH_STATE_LOCK:
        path = delivery_state_path(kernel)
        state = _load_health_state_sync(path)
        changed = False
        for agent_name, chat_id, status, retry_after_s, error in results:
            record = (state.get("agents") or {}).get(agent_name)
            if not record:
                continue
            chat_state = record.setdefault("per_chat", {}).setdefault(str(chat_id), {})
            if status == "sent":
                chat_state["recovery_notice_sent_at"] = _iso(_now())
            elif status == "retry_after":
                record["status"] = "blocked"
                record["retry_after_s"] = retry_after_s
                record["blocked_until"] = _iso(_now() + timedelta(seconds=max(int(retry_after_s or 0), 1)))
                changed = True
                continue
            else:
                record["status"] = "recovery_due"
                record["recovery_failed"] = True
                record["last_recovery_error"] = f"{type(error).__name__}: {error}"
                changed = True
                continue
            pending = [
                key
                for key, value in (record.get("per_chat") or {}).items()
                if not value.get("recovery_notice_sent_at")
            ]
            if not pending:
                record["status"] = "healthy"
                record["active_failover_agent"] = None
                record["failover_failed"] = False
                record.pop("recovery_failed", None)
                record.pop("last_recovery_error", None)
            changed = True
        if changed:
            _save_health_state_sync(path, state)
