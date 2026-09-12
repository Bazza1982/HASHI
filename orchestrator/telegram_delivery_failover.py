from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta
from math import ceil
from pathlib import Path
from typing import Any
from uuid import uuid4

from telegram.error import RetryAfter

from orchestrator import telegram_stream_policy
from orchestrator.process_resources import async_path_lock
from orchestrator.telegram_delivery_errors import (
    TelegramDeliveryError,
    classify_telegram_delivery_error,
)
from orchestrator.telegram_delivery_state import (
    DeliveryStateError,
    mutate_state,
    owned_record,
    owner_for_runtime,
    quarantine_record,
    read_state,
    record_owner,
    retire_agent_state,
    telegram_bot_fingerprint,
)

DEFAULT_FAILOVER_AGENT = "lin_yueru"
DEFAULT_WARNING_REMINDER_SECONDS = 600
DEFAULT_WATCHER_POLL_SECONDS = 60
MAX_RECOVERY_ATTEMPTS = 3
RECOVERY_ATTEMPT_LEASE_SECONDS = 300

logger = logging.getLogger("BridgeU.TelegramDeliveryFailover")


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
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_now().tzinfo)
        return parsed
    except Exception:
        return None


def _record_has_active_block(
    record: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether Telegram's authoritative RetryAfter window is active.

    ``recovery_due`` means a recovery notice remains pending.  It must not
    suppress ordinary Telegram traffic once the server-provided delay ends.
    """

    if str(record.get("status") or "") != "blocked":
        return False
    blocked_until = _parse_iso(record.get("blocked_until"))
    if blocked_until is None:
        # Preserve fail-closed behaviour for malformed legacy incidents that
        # do not provide a safe release deadline.
        return True
    current = now or _now()
    if blocked_until.tzinfo is None:
        blocked_until = blocked_until.replace(tzinfo=current.tzinfo)
    return current < blocked_until.astimezone(current.tzinfo)


def retry_after_seconds(exc: Any) -> int:
    """Normalize RetryAfter values without rounding a fractional wait down."""

    value = getattr(exc, "retry_after", 0) or 0
    if isinstance(value, timedelta):
        return max(0, ceil(value.total_seconds()))
    try:
        return max(0, ceil(float(value)))
    except (OverflowError, TypeError, ValueError):
        return 0


def _pending_recovery_chat_keys(record: dict[str, Any]) -> list[str]:
    now = _now()
    pending: list[str] = []
    per_chat = record.get("per_chat") or {}
    if not isinstance(per_chat, dict):
        return pending
    for chat_key, chat_state in per_chat.items():
        if not isinstance(chat_state, dict):
            continue
        if chat_state.get("recovery_notice_sent_at") or chat_state.get(
            "recovery_stopped_at"
        ):
            continue
        next_at = _parse_iso(chat_state.get("next_recovery_at"))
        if next_at is not None and next_at > now:
            continue
        claim_id = chat_state.get("recovery_attempt_id")
        if claim_id:
            started = _parse_iso(chat_state.get("recovery_attempt_started_at"))
            if started is None or (
                now - started
            ).total_seconds() < RECOVERY_ATTEMPT_LEASE_SECONDS:
                continue
        pending.append(str(chat_key))
    return pending


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
    try:
        return read_state(path)
    except DeliveryStateError as exc:
        logger.error("Telegram delivery state unavailable: %s", exc)
        return {"version": 2, "agents": {}, "quarantine": [], "read_error": str(exc)}


def load_health_state(runtime_or_kernel: Any) -> dict[str, Any]:
    return _load_health_state_sync(delivery_state_path(runtime_or_kernel))


def retire_agent_delivery_state(
    project_root: str | Path,
    agent_name: str,
    *,
    lifecycle_id: object = None,
    reason: str,
) -> dict[str, Any] | None:
    """Narrow lifecycle hook used by delete/recreate administration."""

    return retire_agent_state(
        project_root,
        agent_name,
        lifecycle_id=lifecycle_id,
        reason=reason,
    )


def runtime_token_key(runtime: Any) -> str:
    token_key = getattr(getattr(runtime, "config", None), "telegram_token_key", None) or getattr(runtime, "name", "unknown")
    return f"telegram:{token_key}"


def _agent_record(state: dict[str, Any], runtime: Any) -> dict[str, Any]:
    owner = owner_for_runtime(runtime)
    if owner is None:
        raise DeliveryStateError(
            f"Telegram delivery owner is incomplete for {getattr(runtime, 'name', 'unknown')}"
        )
    agents = state.setdefault("agents", {})
    agent_name = getattr(runtime, "name", "unknown")
    existing = agents.get(agent_name)
    if isinstance(existing, dict) and record_owner(existing) != owner:
        quarantine_record(
            state,
            agent_name,
            reason=(
                "legacy_record_has_no_owner"
                if record_owner(existing) is None
                else "runtime_owner_mismatch"
            ),
            expected_owner=owner,
        )
        existing = None
    record = existing if isinstance(existing, dict) else {}
    agents[agent_name] = record
    record["owner"] = owner
    record.setdefault("token_key", runtime_token_key(runtime))
    record.setdefault("status", "healthy")
    record.setdefault("per_chat", {})
    return record


def _owned_record_or_quarantine(
    state: dict[str, Any], runtime: Any
) -> tuple[dict[str, Any] | None, bool]:
    agent_name = getattr(runtime, "name", "unknown")
    owner = owner_for_runtime(runtime)
    record = (state.get("agents") or {}).get(agent_name)
    if not isinstance(record, dict):
        return None, False
    if owner is not None and record_owner(record) == owner:
        return record, False
    reason = (
        "runtime_owner_incomplete"
        if owner is None
        else (
            "legacy_record_has_no_owner"
            if record_owner(record) is None
            else "runtime_owner_mismatch"
        )
    )
    quarantine_record(
        state,
        agent_name,
        reason=reason,
        expected_owner=owner,
    )
    logger.warning(
        "Telegram delivery state quarantined agent=%s reason=%s incident_id=%s",
        agent_name,
        reason,
        record.get("incident_id"),
    )
    return None, True


def get_blocked_record(runtime: Any) -> dict[str, Any] | None:
    state = load_health_state(runtime)
    record = owned_record(state, getattr(runtime, "name", "unknown"), owner_for_runtime(runtime))
    if not record:
        return None
    if _record_has_active_block(record):
        return record
    return None


def is_delivery_blocked(runtime: Any) -> bool:
    return get_blocked_record(runtime) is not None


def delivery_status_summary(runtime: Any) -> dict[str, Any] | None:
    state = load_health_state(runtime)
    agent_name = getattr(runtime, "name", "unknown")
    runtime_owner = owner_for_runtime(runtime)
    record = owned_record(state, agent_name, runtime_owner)
    if record:
        status = str(record.get("status") or "healthy")
        if _record_has_active_block(record) or status in {
            "recovery_due",
            "recovery_stopped",
        }:
            return {
                "blocked_until": record.get("blocked_until"),
                "status": status,
                "active_failover_agent": record.get("active_failover_agent"),
                "incident_id": record.get("incident_id"),
                "reason": record.get("last_recovery_error"),
            }
        return None

    # A quarantined stale record is not a delivery block, but exposing one
    # concise status prevents operators from mistaking isolation for delivery.
    for entry in reversed(state.get("quarantine") or []):
        if not isinstance(entry, dict) or entry.get("agent_name") != agent_name:
            continue
        expected_owner = entry.get("expected_owner")
        if expected_owner and expected_owner != runtime_owner:
            continue
        return {
            "status": "quarantined",
            "incident_id": entry.get("incident_id"),
            "reason": entry.get("reason"),
        }
    return None


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


def _eligible_failover_runtime(source_runtime: Any, candidate: Any, *, blocked_bots: set[str]) -> bool:
    if candidate is source_runtime:
        return False
    if getattr(candidate, "name", None) == getattr(source_runtime, "name", None):
        return False
    if not getattr(candidate, "telegram_connected", False):
        return False
    if str(getattr(candidate, "token", "") or "") == "WORKBENCH_ONLY_NO_TOKEN":
        return False
    candidate_owner = owner_for_runtime(candidate)
    source_owner = owner_for_runtime(source_runtime)
    candidate_bot = (candidate_owner or {}).get("telegram_bot_fingerprint")
    source_bot = (source_owner or {}).get("telegram_bot_fingerprint")
    if not candidate_bot:
        return False
    if candidate_bot in blocked_bots:
        return False
    if candidate_bot == source_bot:
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
    blocked_bots = {
        str((record_owner(record) or {}).get("telegram_bot_fingerprint") or "")
        for record in (state.get("agents") or {}).values()
        if _record_has_active_block(record)
    }
    candidates = _runtime_candidates(source_runtime)
    if preferred_name:
        for candidate in candidates:
            if candidate.name == preferred_name and candidate.name not in exclude_names:
                if _eligible_failover_runtime(source_runtime, candidate, blocked_bots=blocked_bots):
                    return candidate
    for candidate in candidates:
        if candidate.name in exclude_names:
            continue
        if _eligible_failover_runtime(source_runtime, candidate, blocked_bots=blocked_bots):
            return candidate
    return None


def _warn_text(
    *,
    instance_id: str,
    source_agent: str,
    request_id: str | None,
    retry_after_s: int | None,
    blocked_until: str | None,
    response_path: Path | None,
    failover_agent: str | None,
    status: str = "blocked",
) -> str:
    recovery_due = status == "recovery_due"
    lines = [
        f"Delivery warning from {instance_id}:",
        "",
        (
            f"{source_agent} generated a response, but Telegram delivery recovery "
            "is still pending after the earlier flood limit expired."
            if recovery_due
            else f"{source_agent} generated a response, but Telegram delivery is flood-limited."
        ),
    ]
    if request_id:
        lines.append("")
        lines.append(f"Request: {request_id}")
    if retry_after_s is not None and not recovery_due:
        lines.append(f"Retry after: {retry_after_s}s")
    if blocked_until and not recovery_due:
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
    try:
        sender = getattr(runtime, "_send_text", None)
        if callable(sender):
            result = await sender(
                chat_id,
                text,
                _delivery_mode="failover_notice",
                _raise_delivery_error=True,
            )
            if result is False or result is None:
                raise TelegramDeliveryError(
                    "delivery_not_confirmed",
                    retryable=True,
                    permanent=False,
                    reason="telegram_send_returned_no_receipt",
                )
            return
        await runtime.app.bot.send_message(chat_id=chat_id, text=text)
    except Exception as exc:
        raise classify_telegram_delivery_error(exc) from exc


async def send_runtime_notice(
    kernel, *, source_agent, chat_id, thread_id=None, render_text
):
    """Send an operational notice without depending on any Agent Worker.

    Keep the original destination across fallback. Only this instance's known
    bot credentials are considered; no model, poller or Agent is started.
    """
    from telegram import Bot

    workers = getattr(kernel, "function_workers", None)
    ingresses = getattr(workers, "_telegram_ingress", {})
    runtime_map = kernel._runtime_map()
    raw = kernel._load_raw_config() if hasattr(kernel, "_load_raw_config") else {}
    configs = {str(a["name"]): a for a in raw.get("agents", [])}
    preferred = (raw.get("global", {}).get("telegram_delivery_failover") or {}).get(
        "default_agent"
    )
    names = list(
        dict.fromkeys(
            [source_agent, *([preferred] if preferred else []), *ingresses, *configs]
        )
    )
    health = load_health_state(kernel)

    def token_for(name):
        return str(
            getattr(ingresses.get(name), "token", "")
            or (getattr(kernel, "secrets", {}) or {}).get(
                configs.get(name, {}).get("telegram_token_key"), ""
            )
        )

    instance_id = str(getattr(kernel.global_cfg, "instance_id", "") or "").upper()

    def configured_owner(name):
        row = configs.get(name, {})
        lifecycle_id = str(row.get("agent_lifecycle_id") or "").strip().casefold()
        fingerprint = telegram_bot_fingerprint(token_for(name))
        if not instance_id or not lifecycle_id or not fingerprint:
            return None
        return {
            "instance_id": instance_id,
            "agent_lifecycle_id": lifecycle_id,
            "telegram_bot_fingerprint": fingerprint,
        }

    blocked = {
        name
        for name, record in health.get("agents", {}).items()
        if _record_has_active_block(record)
        and record_owner(record) == configured_owner(name)
    }
    blocked_bots = {
        str((record_owner(record) or {}).get("telegram_bot_fingerprint") or "")
        for name, record in health.get("agents", {}).items()
        if name in blocked
    }

    tried_tokens = {
        token_for(name)
        for name in names
        if name in blocked
        or str((configured_owner(name) or {}).get("telegram_bot_fingerprint") or "")
        in blocked_bots
    }
    retry_delay = 5
    try:
        async with asyncio.timeout(15):
            for name in names:
                if name in blocked:
                    continue
                token = token_for(name)
                if (
                    not token
                    or token in tried_tokens
                    or token == "WORKBENCH_ONLY_NO_TOKEN"
                ):
                    continue
                tried_tokens.add(token)
                handle = runtime_map.get(name)
                display = handle.get_display_name() if handle else name
                try:
                    async with asyncio.timeout(5):
                        async with Bot(token) as bot:
                            message = await bot.send_message(
                                chat_id=chat_id,
                                message_thread_id=thread_id,
                                text=render_text(name, display),
                                parse_mode="HTML",
                            )
                    return {
                        "sent": True,
                        "sender": name,
                        "message_id": message.message_id,
                    }
                except RetryAfter as exc:
                    retry_delay = max(retry_delay, retry_after_seconds(exc))
                except Exception as exc:
                    # Raw transport errors may contain the bot's request URL.
                    logger.warning(
                        "Runtime notice via %s failed (%s)", name, type(exc).__name__
                    )
    except TimeoutError:
        pass
    return {"sent": False, "retry_after": retry_delay}


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
    async with async_path_lock(delivery_state_path(source_runtime)):
        path = delivery_state_path(source_runtime)

        def mutation(state):
            record, changed = _owned_record_or_quarantine(state, source_runtime)
            if not record or not _record_has_active_block(record):
                return None, changed
            entry = record.setdefault("per_chat", {}).setdefault(str(chat_id), {})
            now = _now()
            last_warned_at = _parse_iso(entry.get("last_warned_at"))
            if last_warned_at is not None and (
                now - last_warned_at
            ).total_seconds() < warning_reminder_seconds(source_runtime):
                return None, changed
            preferred_name = str(
                record.get("active_failover_agent")
                or configured_default_failover_agent(source_runtime)
            )
            chosen = _select_failover_runtime_from_state(
                source_runtime,
                state,
                preferred_name=preferred_name,
                exclude_names=exclude_names,
            )
            if chosen is None:
                record["failover_failed"] = True
                record["last_failover_error"] = "no_eligible_failover_runtime"
                return None, True
            warning_text = _warn_text(
                instance_id=str(
                    getattr(
                        getattr(source_runtime, "global_config", None),
                        "instance_id",
                        None,
                    )
                    or "HASHI"
                ),
                source_agent=source_runtime.name,
                request_id=request_id,
                retry_after_s=record.get("retry_after_s"),
                blocked_until=record.get("blocked_until"),
                response_path=response_path,
                failover_agent=chosen.name,
                status=str(record.get("status") or "blocked"),
            )
            return (chosen, warning_text), changed

        return mutate_state(path, mutation)


async def _record_warning_result(
    source_runtime: Any,
    *,
    chat_id: int,
    request_id: str | None,
    failover_agent: str | None,
    success: bool,
    error: Exception | None = None,
) -> None:
    async with async_path_lock(delivery_state_path(source_runtime)):
        path = delivery_state_path(source_runtime)

        def mutation(state):
            record, changed = _owned_record_or_quarantine(state, source_runtime)
            if not record:
                return None, changed
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
                classified = classify_telegram_delivery_error(
                    error or RuntimeError("failover warning failed")
                )
                record["failover_failed"] = True
                record["last_failover_error"] = classified.code
            return None, True

        mutate_state(path, mutation)


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
    except TelegramDeliveryError as delivery_error:
        if delivery_error.code != "retry_after":
            await _record_warning_result(
                source_runtime,
                chat_id=chat_id,
                request_id=request_id,
                failover_agent=chosen.name,
                success=False,
                error=delivery_error,
            )
            return
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
    async with async_path_lock(delivery_state_path(runtime)):
        path = delivery_state_path(runtime)

        def mutation(state):
            record, changed = _owned_record_or_quarantine(state, runtime)
            if not record:
                return {"blocked": False}, changed
            if not _record_has_active_block(record):
                if str(record.get("status") or "") == "blocked":
                    record["status"] = (
                        "recovery_due"
                        if _pending_recovery_chat_keys(record)
                        else "healthy"
                    )
                    record["delivery_restored_at"] = _iso(_now())
                    record["active_failover_agent"] = None
                    record["failover_failed"] = False
                    if record["status"] == "healthy":
                        record.pop("recovery_failed", None)
                        record.pop("last_recovery_error", None)
                    logger.info(
                        "Telegram delivery wait elapsed; normal delivery restored "
                        "agent=%s incident_id=%s blocked_until=%s",
                        getattr(runtime, "name", "unknown"),
                        record.get("incident_id"),
                        record.get("blocked_until"),
                    )
                    return {"blocked": False}, True
                return {"blocked": False}, changed
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
                    entry = record.setdefault("per_chat", {}).setdefault(
                        str(chat_id), {}
                    )
                    requests = entry.setdefault("undelivered_request_ids", [])
                    if request_id and request_id not in requests:
                        requests.append(request_id)
            return {
                "blocked": True,
                "record": dict(record),
                "response_path": response_path,
            }, True

        result = mutate_state(path, mutation)
    if result.get("blocked"):
        await _maybe_send_warning(
            runtime,
            chat_id=chat_id,
            record=result["record"],
            request_id=request_id,
            response_path=result["response_path"],
        )
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
    retry_after_s = retry_after_seconds(exc)
    now = _now()
    blocked_until_dt = now + timedelta(seconds=max(retry_after_s, 1))
    runtime_name = getattr(runtime, "name", "unknown")
    incident_id = f"tg-{runtime_name}-{now.strftime('%Y%m%dT%H%M%S%f')}"
    async with async_path_lock(delivery_state_path(runtime)):
        path = delivery_state_path(runtime)

        def mutation(state):
            current_incident_id = incident_id
            record = _agent_record(state, runtime)
            continuing_incident = _record_has_active_block(record, now=now)
            if continuing_incident and record.get("incident_id"):
                current_incident_id = str(record.get("incident_id"))
            else:
                for entry in (record.get("per_chat") or {}).values():
                    for key in (
                        "first_warned_at",
                        "last_warned_at",
                        "last_warning_request_id",
                        "recovery_notice_sent_at",
                        "recovery_stopped_at",
                        "recovery_stop_reason",
                        "recovery_attempt_id",
                        "recovery_attempt_started_at",
                        "recovery_attempts",
                        "next_recovery_at",
                    ):
                        entry.pop(key, None)
                record["active_failover_agent"] = None
                record["failover_failed"] = False
                record.pop("last_failover_error", None)
            record["status"] = "blocked"
            record["token_key"] = runtime_token_key(runtime)
            record["blocked_until"] = _iso(blocked_until_dt)
            record["retry_after_s"] = retry_after_s
            record["incident_id"] = current_incident_id
            record["last_incident_at"] = _iso(now)
            record["last_request_id"] = request_id
            record.pop("delivery_restored_at", None)
            record.pop("recovery_failed", None)
            record.pop("last_recovery_error", None)
            response_path = None
            if text:
                response_path = persist_undelivered_response(
                    runtime,
                    request_id=request_id,
                    chat_id=chat_id,
                    text=text,
                    purpose=purpose,
                    incident_id=current_incident_id,
                    retry_after_s=retry_after_s,
                    blocked_until=record["blocked_until"],
                    failover_agent=record.get("active_failover_agent"),
                )
            if chat_id is not None:
                entry = record.setdefault("per_chat", {}).setdefault(str(chat_id), {})
                requests = entry.setdefault("undelivered_request_ids", [])
                if request_id and request_id not in requests:
                    requests.append(request_id)
            return (dict(record), response_path), True

        saved_record, response_path = mutate_state(path, mutation)
    await _maybe_send_warning(
        runtime,
        chat_id=chat_id,
        record=saved_record,
        request_id=request_id,
        response_path=response_path,
    )
    return saved_record


async def delivery_health_watcher(kernel: Any) -> None:
    while True:
        try:
            await _tick_recovery(kernel)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Telegram delivery recovery watcher tick failed")
        await asyncio.sleep(max(5, watcher_poll_seconds(kernel)))


async def _tick_recovery(kernel: Any) -> None:
    notices: list[tuple[str, Any, int, str | None, dict[str, str], str]] = []
    async with async_path_lock(delivery_state_path(kernel)):
        path = delivery_state_path(kernel)
        runtimes = {getattr(rt, "name", None): rt for rt in getattr(kernel, "runtimes", [])}

        def claim_mutation(state):
            changed = False
            now = _now()
            for agent_name, record in list((state.get("agents") or {}).items()):
                if not isinstance(record, dict):
                    quarantine_record(
                        state,
                        agent_name,
                        reason="malformed_agent_delivery_record",
                    )
                    changed = True
                    continue
                owner = record_owner(record)
                if owner is None:
                    quarantine_record(
                        state,
                        agent_name,
                        reason="legacy_record_has_no_owner",
                    )
                    logger.warning(
                        "Telegram delivery state quarantined agent=%s "
                        "reason=legacy_record_has_no_owner incident_id=%s",
                        agent_name,
                        record.get("incident_id"),
                    )
                    changed = True
                    continue
                runtime = runtimes.get(agent_name)
                if runtime is None:
                    continue
                runtime_owner = owner_for_runtime(runtime)
                if runtime_owner != owner:
                    quarantine_record(
                        state,
                        agent_name,
                        reason="runtime_owner_mismatch",
                        expected_owner=runtime_owner,
                    )
                    logger.warning(
                        "Telegram delivery state quarantined agent=%s "
                        "reason=runtime_owner_mismatch incident_id=%s",
                        agent_name,
                        record.get("incident_id"),
                    )
                    changed = True
                    continue
                if not isinstance(record.get("per_chat"), dict):
                    quarantine_record(
                        state,
                        agent_name,
                        reason="malformed_per_chat_delivery_state",
                        expected_owner=runtime_owner,
                    )
                    logger.warning(
                        "Telegram delivery state quarantined agent=%s "
                        "reason=malformed_per_chat_delivery_state incident_id=%s",
                        agent_name,
                        record.get("incident_id"),
                    )
                    changed = True
                    continue
                status = str(record.get("status") or "")
                if status not in {"blocked", "recovery_due"}:
                    continue
                if status == "blocked" and _record_has_active_block(record, now=now):
                    continue
                if not getattr(runtime, "telegram_connected", False):
                    continue
                if status == "blocked":
                    record["status"] = "recovery_due"
                    logger.info(
                        "Telegram delivery block expired; recovery due agent=%s",
                        agent_name,
                    )
                    changed = True
                for chat_key, chat_state in (record.get("per_chat") or {}).items():
                    if not isinstance(chat_state, dict):
                        continue
                    if chat_state.get("recovery_notice_sent_at") or chat_state.get(
                        "recovery_stopped_at"
                    ):
                        continue
                    claim_id = chat_state.get("recovery_attempt_id")
                    if claim_id:
                        started = _parse_iso(chat_state.get("recovery_attempt_started_at"))
                        if started is not None and (
                            now - started
                        ).total_seconds() < RECOVERY_ATTEMPT_LEASE_SECONDS:
                            continue
                        # The prior process may have sent before losing its
                        # receipt. Do not risk a duplicate recovery notice.
                        chat_state["recovery_stopped_at"] = _iso(now)
                        chat_state["recovery_stop_reason"] = (
                            "prior_attempt_outcome_unknown"
                        )
                        chat_state.pop("recovery_attempt_id", None)
                        chat_state.pop("recovery_attempt_started_at", None)
                        changed = True
                        continue
                    next_at = _parse_iso(chat_state.get("next_recovery_at"))
                    if next_at is not None and next_at > now:
                        continue
                    try:
                        chat_id = int(chat_key)
                        if chat_id == 0:
                            raise ValueError("zero chat ID")
                    except (TypeError, ValueError):
                        chat_state["recovery_stopped_at"] = _iso(now)
                        chat_state["recovery_stop_reason"] = "invalid_chat_id"
                        chat_state["last_recovery_error_code"] = "invalid_chat_id"
                        record["recovery_failed"] = True
                        record["last_recovery_error"] = "invalid_chat_id"
                        logger.warning(
                            "Telegram delivery recovery stopped agent=%s "
                            "chat_id=invalid reason=invalid_chat_id",
                            agent_name,
                        )
                        changed = True
                        continue
                    attempt_id = uuid4().hex
                    chat_state["recovery_attempt_id"] = attempt_id
                    chat_state["recovery_attempt_started_at"] = _iso(now)
                    notices.append(
                        (
                            agent_name,
                            runtime,
                            chat_id,
                            record.get("incident_id"),
                            dict(owner),
                            attempt_id,
                        )
                    )
                    changed = True
                status_before_normalize = str(record.get("status") or "")
                _normalize_recovery_status(record)
                if str(record.get("status") or "") != status_before_normalize:
                    changed = True
            return None, changed

        mutate_state(path, claim_mutation)
    if not notices:
        return
    results: list[tuple[str, int, str | None, dict[str, str], str, str, TelegramDeliveryError | None]] = []
    for agent_name, runtime, chat_id, incident_id, owner, attempt_id in notices:
        try:
            await _send_direct(runtime, chat_id=chat_id, text=_recovery_text(agent_name))
            results.append(
                (agent_name, chat_id, incident_id, owner, attempt_id, "sent", None)
            )
        except TelegramDeliveryError as exc:
            results.append(
                (agent_name, chat_id, incident_id, owner, attempt_id, "error", exc)
            )
        except Exception as exc:
            results.append(
                (
                    agent_name,
                    chat_id,
                    incident_id,
                    owner,
                    attempt_id,
                    "error",
                    classify_telegram_delivery_error(exc),
                )
            )
    async with async_path_lock(delivery_state_path(kernel)):
        path = delivery_state_path(kernel)

        def result_mutation(state):
            changed = False
            for (
                agent_name,
                chat_id,
                incident_id,
                owner,
                attempt_id,
                status,
                error,
            ) in results:
                record = owned_record(state, agent_name, owner)
                if not record or record.get("incident_id") != incident_id:
                    logger.info(
                        "Ignoring stale Telegram recovery result agent=%s "
                        "incident_id=%s attempt_id=%s",
                        agent_name,
                        incident_id,
                        attempt_id,
                    )
                    continue
                chat_state = (record.get("per_chat") or {}).get(str(chat_id))
                if not isinstance(chat_state, dict) or (
                    chat_state.get("recovery_attempt_id") != attempt_id
                ):
                    logger.info(
                        "Ignoring superseded Telegram recovery result agent=%s "
                        "incident_id=%s attempt_id=%s",
                        agent_name,
                        incident_id,
                        attempt_id,
                    )
                    continue
                chat_state.pop("recovery_attempt_id", None)
                chat_state.pop("recovery_attempt_started_at", None)
                now = _now()
                if status == "sent":
                    chat_state["recovery_notice_sent_at"] = _iso(now)
                    chat_state.pop("next_recovery_at", None)
                    logger.info(
                        "Telegram delivery recovery notice sent agent=%s chat_id=%s",
                        agent_name,
                        chat_id,
                    )
                elif error is not None and error.code == "retry_after":
                    wait = max(int(error.retry_after_s or 0), 1)
                    record["status"] = "blocked"
                    record["retry_after_s"] = wait
                    record["blocked_until"] = _iso(now + timedelta(seconds=wait))
                    chat_state["last_recovery_error_code"] = error.code
                    logger.warning(
                        "Telegram delivery recovery rate-limited agent=%s "
                        "chat_id=%s retry_after_s=%s",
                        agent_name,
                        chat_id,
                        wait,
                    )
                elif error is not None and error.permanent:
                    chat_state["recovery_stopped_at"] = _iso(now)
                    chat_state["recovery_stop_reason"] = error.code
                    chat_state["last_recovery_error_code"] = error.code
                    record["recovery_failed"] = True
                    record["last_recovery_error"] = error.code
                    logger.warning(
                        "Telegram delivery recovery stopped agent=%s chat_id=%s "
                        "reason=%s",
                        agent_name,
                        chat_id,
                        error.code,
                    )
                else:
                    attempts = int(chat_state.get("recovery_attempts") or 0) + 1
                    chat_state["recovery_attempts"] = attempts
                    code = error.code if error is not None else "transient_unknown"
                    chat_state["last_recovery_error_code"] = code
                    record["recovery_failed"] = True
                    record["last_recovery_error"] = code
                    if attempts >= MAX_RECOVERY_ATTEMPTS:
                        chat_state["recovery_stopped_at"] = _iso(now)
                        chat_state["recovery_stop_reason"] = (
                            "transient_retry_exhausted"
                        )
                        logger.warning(
                            "Telegram delivery recovery stopped agent=%s "
                            "chat_id=%s reason=transient_retry_exhausted",
                            agent_name,
                            chat_id,
                        )
                    else:
                        delay = min(300, 5 * (2 ** (attempts - 1)))
                        chat_state["next_recovery_at"] = _iso(
                            now + timedelta(seconds=delay)
                        )
                        logger.warning(
                            "Telegram delivery recovery deferred agent=%s "
                            "chat_id=%s reason=%s attempt=%s",
                            agent_name,
                            chat_id,
                            code,
                            attempts,
                        )
                if str(record.get("status") or "") != "blocked":
                    _normalize_recovery_status(record)
                changed = True
            return None, changed

        mutate_state(path, result_mutation)


def _normalize_recovery_status(record: dict[str, Any]) -> None:
    chats = [
        value
        for value in (record.get("per_chat") or {}).values()
        if isinstance(value, dict)
    ]
    if any(
        not value.get("recovery_notice_sent_at")
        and not value.get("recovery_stopped_at")
        for value in chats
    ):
        record["status"] = "recovery_due"
        return
    if any(value.get("recovery_stopped_at") for value in chats):
        record["status"] = "recovery_stopped"
        record["active_failover_agent"] = None
        return
    record["status"] = "healthy"
    record["active_failover_agent"] = None
    record["failover_failed"] = False
    record.pop("recovery_failed", None)
    record.pop("last_recovery_error", None)
