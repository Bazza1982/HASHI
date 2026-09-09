"""Localized /version command backed by one structured fact payload."""

from __future__ import annotations

import asyncio
import html
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from orchestrator import runtime_remote, ui_language
from orchestrator.command_ui import DIVIDER, card_title
from orchestrator.version_info import collect_version_payload
from orchestrator.version_remote import (
    VersionQueryError,
    load_version_cache,
    query_remote_version,
    resolve_instance_entry,
    save_version_cache,
)
from remote.security.shared_token import load_shared_token


def _tr(key: str, locale: str, **values: Any) -> str:
    return ui_language.tr(key, locale=locale, **values)


def _short(value: Any, length: int = 12) -> str:
    text = str(value or "")
    if text.startswith("sha256:"):
        return f"sha256:{text[7:7 + length]}…"
    return text[:length] if text else ""


def _format_time_portable(value: Any, locale: str) -> str:
    """Format Windows safely (``%-I`` is unsupported there)."""

    raw = str(value or "").strip()
    if not raw:
        return _tr("version.unavailable", locale)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return _tr("version.unavailable", locale)
    zone = parsed.tzname() or parsed.strftime("%z")
    if locale == "zh-CN":
        return f"{parsed:%Y-%m-%d %H:%M} {zone}"
    hour = parsed.strftime("%I").lstrip("0") or "0"
    return f"{parsed.day} {parsed:%b %Y}, {hour}:{parsed:%M %p} {zone}"


def _running_ref(payload: Mapping[str, Any], locale: str, *, full: bool = False) -> str:
    running = payload.get("running") or {}
    commit = str(running.get("commit") or "")
    if not commit:
        return _tr("version.legacy_generation", locale)
    ref = str(
        running.get("branch")
        or running.get("tag")
        or _tr("version.detached", locale)
    )
    return f"{ref}@{commit if full else _short(commit)}"


def _source_ref(payload: Mapping[str, Any], locale: str) -> str:
    source = payload.get("source") or {}
    if not source.get("available"):
        return _tr("version.packaged_source", locale)
    commit = _short(source.get("commit")) or _tr("version.unavailable", locale)
    branch = source.get("branch") or source.get("tag") or _tr("version.detached", locale)
    cleanliness = _tr(
        "version.source.dirty" if source.get("dirty") else "version.source.clean",
        locale,
    )
    if source.get("dirty") and source.get("change_count"):
        cleanliness += f" ({int(source['change_count'])})"
    return f"{branch}@{commit} · {cleanliness}"


def _state_icon(code: str) -> str:
    if code in {"running_matches_source", "packaged_release"}:
        return "✅"
    if code == "mixed_generations":
        return "🟠"
    return "⚠️"


def _state_text(code: str, locale: str) -> str:
    key = f"version.state.{code}"
    translated = _tr(key, locale)
    return translated if translated != key else _tr("version.state.provenance_unavailable", locale)


def render_version_card(
    payload: Mapping[str, Any],
    *,
    locale: str,
    full: bool = False,
) -> str:
    instance = payload.get("instance") or {}
    running = payload.get("running") or {}
    functions = running.get("functions") or {}
    worker = running.get("worker") or {}
    source = payload.get("source") or {}
    state = payload.get("state") or {}
    compatibility = payload.get("compatibility") or {}
    environment = str(instance.get("environment") or "unknown").upper()
    display_name = str(instance.get("display_name") or "")
    instance_value = str(instance.get("id") or "HASHI")
    if display_name and display_name.casefold() != instance_value.casefold():
        instance_value += f" · {display_name}"
    instance_value += f" · {environment}"
    worker_generation = worker.get("generation_id")
    if running.get("mixed_workers"):
        worker_generation = _tr("version.mixed", locale)
    lines = [
        card_title("🏷️", "HASHI version", locale=locale),
        "",
        f"<b>{html.escape(_tr('version.product', locale))}</b> · <code>{html.escape(str((payload.get('product') or {}).get('version') or 'unknown'))}</code>",
        f"<b>{html.escape(_tr('version.instance', locale))}</b> · {html.escape(instance_value)}",
        f"<b>{html.escape(_tr('version.running', locale))}</b> · <code>{html.escape(_running_ref(payload, locale))}</code>",
        f"<b>{html.escape(_tr('version.commit_time', locale))}</b> · {html.escape(_format_time_portable(running.get('commit_time'), locale))}",
        f"<b>Functions</b> · <code>{html.escape(_short(functions.get('generation_id')) or _tr('version.unavailable', locale))}</code>",
        f"<b>Worker</b> · <code>{html.escape(_short(worker_generation) or _tr('version.unavailable', locale))}</code>",
        f"<b>{html.escape(_tr('version.adopted', locale))}</b> · {html.escape(_format_time_portable(running.get('adopted_at'), locale))}",
        f"<b>{html.escape(_tr('version.source', locale))}</b> · <code>{html.escape(_source_ref(payload, locale))}</code>",
        f"<b>{html.escape(_tr('version.status', locale))}</b> · {_state_icon(str(state.get('code') or ''))} {html.escape(_state_text(str(state.get('code') or ''), locale))}",
    ]
    if not full:
        return "\n".join(lines)

    remote = running.get("remote") or {}
    lines.extend(
        [
            "",
            f"<b>{html.escape(_tr('version.details', locale))}</b>",
            f"{html.escape(_tr('version.full_commit', locale))} · <code>{html.escape(str(running.get('commit') or _tr('version.legacy_generation', locale)))}</code>",
            f"Build ID · <code>{html.escape(str(running.get('build_id') or _tr('version.unavailable', locale)))}</code>",
            f"{html.escape(_tr('version.build_time', locale))} · {html.escape(_format_time_portable(running.get('build_time'), locale))}",
            f"{html.escape(_tr('version.release_channel', locale))} · <code>{html.escape(str(running.get('release_channel') or _tr('version.unavailable', locale)))}</code>",
            f"Core API · <code>{html.escape(str(compatibility.get('core_api') or _tr('version.unavailable', locale)))}</code> · Functions API · <code>{html.escape(str(compatibility.get('function_api') or _tr('version.unavailable', locale)))}</code>",
            f"Worker protocol · <code>{html.escape(str(compatibility.get('worker_protocol') or _tr('version.unavailable', locale)))}</code> · Generation schema · <code>{html.escape(str(compatibility.get('generation_schema') or _tr('version.unavailable', locale)))}</code>",
            f"Python · <code>{html.escape(str(compatibility.get('python') or instance.get('python') or 'unknown'))}</code>",
            f"Platform ABI · <code>{html.escape(str(compatibility.get('platform_abi') or _tr('version.unavailable', locale)))}</code>",
            f"Shared Functions · PID <code>{html.escape(str(functions.get('pid') or _tr('version.unavailable', locale)))}</code> · <code>{html.escape(str(functions.get('generation_id') or _tr('version.unavailable', locale)))}</code>",
            f"Current Worker · PID <code>{html.escape(str(worker.get('pid') or _tr('version.unavailable', locale)))}</code> · <code>{html.escape(str(worker.get('generation_id') or _tr('version.unavailable', locale)))}</code>",
            f"Remote · {html.escape(str(remote.get('status') or 'offline'))} · PID <code>{html.escape(str(remote.get('pid') or _tr('version.unavailable', locale)))}</code> · <code>{html.escape(str(remote.get('generation_id') or _tr('version.unavailable', locale)))}</code>",
            f"{html.escape(_tr('version.remote_started', locale))} · {html.escape(_format_time_portable(remote.get('started_at'), locale))}",
            f"{html.escape(_tr('version.source_branch', locale))} · <code>{html.escape(str(source.get('branch') or _tr('version.packaged_branch', locale)))}</code>",
        ]
    )
    return "\n".join(lines)


def _summary_block(
    payload: Mapping[str, Any],
    *,
    locale: str,
    current: bool = False,
) -> list[str]:
    instance = payload.get("instance") or {}
    running = payload.get("running") or {}
    state = payload.get("state") or {}
    suffix = f" · {_tr('version.this_instance', locale)}" if current else ""
    return [
        f"{_state_icon(str(state.get('code') or ''))} <b>{html.escape(str(instance.get('id') or 'HASHI'))}</b> · {html.escape(str(instance.get('environment') or 'unknown').upper())}{html.escape(suffix)}",
        f"{html.escape(_tr('version.product', locale))} · <code>{html.escape(str((payload.get('product') or {}).get('version') or 'unknown'))}</code>",
        f"{html.escape(_tr('version.running', locale))} · <code>{html.escape(_running_ref(payload, locale))}</code>",
        f"{html.escape(_tr('version.commit_time', locale))} · {html.escape(_format_time_portable(running.get('commit_time'), locale))}",
        f"{html.escape(_tr('version.adopted', locale))} · {html.escape(_format_time_portable(running.get('adopted_at'), locale))}",
        f"{html.escape(_tr('version.status', locale))} · {html.escape(_state_text(str(state.get('code') or ''), locale))}",
    ]


def render_version_all(
    rows: list[dict[str, Any]],
    *,
    locale: str,
    current_instance: str,
) -> str:
    lines = [card_title("🏷️", "HASHI version", locale=locale), ""]
    verified = 0
    for index, row in enumerate(rows):
        if index:
            lines.append("")
        status = str(row.get("verification") or "failed")
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else None
        instance_id = str(row.get("instance_id") or "HASHI")
        if status == "live" and payload is not None:
            verified += 1
            lines.extend(
                _summary_block(
                    payload,
                    locale=locale,
                    current=instance_id.upper() == current_instance.upper(),
                )
            )
        elif status == "stale" and payload is not None:
            running_ref = _running_ref(payload, locale)
            product = str((payload.get("product") or {}).get("version") or "unknown")
            lines.extend(
                [
                    f"⚪ <b>{html.escape(instance_id)}</b> · {html.escape(_tr('version.unreachable', locale))}",
                    f"{html.escape(_tr('version.last_verified', locale))} · {html.escape(_format_time_portable(row.get('verified_at'), locale))}",
                    f"{html.escape(_tr('version.cached_version', locale))} · <code>{html.escape(product)} · {html.escape(running_ref)}</code>",
                    f"{html.escape(_tr('version.status', locale))} · {html.escape(_tr('version.state.unverified_offline', locale))}",
                ]
            )
        else:
            lines.extend(
                [
                    f"❌ <b>{html.escape(instance_id)}</b> · {html.escape(_tr('version.unreachable', locale))}",
                    f"{html.escape(_tr('version.status', locale))} · {html.escape(_tr('version.state.query_failed', locale))}",
                ]
            )
    lines.extend(
        [
            "",
            DIVIDER,
            f"{html.escape(_tr('version.verified_count', locale))} · {verified}",
            _tr("version.handshake_note", locale),
        ]
    )
    return "\n".join(lines)


async def _send(runtime: Any, update: Any, text: str) -> None:
    await runtime.send_long_message(
        int(update.effective_chat.id),
        text,
        request_id="version-command",
        purpose="version-command",
        parse_mode="HTML",
    )


async def _trusted_instances(runtime: Any) -> dict[str, dict[str, Any]]:
    try:
        return await runtime_remote.load_move_instances(runtime)
    except runtime_remote.MoveDiscoveryError as exc:
        raise VersionQueryError("remote_unavailable", exc.message_key) from exc


async def command(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    locale = ui_language.preferred_locale(runtime, update)
    args = [str(item).strip() for item in (context.args or ()) if str(item).strip()]
    local_payload = await asyncio.to_thread(collect_version_payload, runtime)
    if not args:
        await _send(runtime, update, render_version_card(local_payload, locale=locale))
        return
    if len(args) != 1:
        await runtime._reply_text(update, _tr("version.usage", locale))
        return
    action = args[0]
    if action.casefold() == "full":
        await _send(
            runtime,
            update,
            render_version_card(local_payload, locale=locale, full=True),
        )
        return

    try:
        instances = await _trusted_instances(runtime)
    except VersionQueryError:
        await runtime._reply_text(update, _tr("version.remote_unavailable", locale))
        return
    local_id = str((local_payload.get("instance") or {}).get("id") or "HASHI").upper()
    local_display = str((local_payload.get("instance") or {}).get("display_name") or local_id)
    resolvable = dict(instances)
    resolvable[local_id.casefold()] = {
        "instance_id": local_id,
        "display_name": local_display,
        "_local": True,
    }
    root = runtime_remote.instance_root(runtime)
    token = load_shared_token(root)
    cache = load_version_cache(root)

    if action.casefold() != "all":
        try:
            target, entry = resolve_instance_entry(resolvable, action)
        except VersionQueryError as exc:
            await runtime._reply_text(
                update,
                _tr(f"version.error.{exc.code}", locale, instance=action),
            )
            return
        if entry.get("_local"):
            await _send(runtime, update, render_version_card(local_payload, locale=locale))
            return
        if entry.get("active") is False:
            cached = cache.get(target)
            rows = [
                {
                    "instance_id": target,
                    "verification": "stale" if cached else "failed",
                    "payload": (cached or {}).get("payload") if cached else None,
                    "verified_at": (cached or {}).get("verified_at") if cached else None,
                }
            ]
            await _send(
                runtime,
                update,
                render_version_all(rows, locale=locale, current_instance=local_id),
            )
            return
        try:
            payload = await asyncio.to_thread(
                query_remote_version,
                entry,
                target_instance=target,
                source_instance=local_id,
                shared_token=token or "",
            )
        except VersionQueryError:
            cached = cache.get(target)
            rows = [
                {
                    "instance_id": target,
                    "verification": "stale" if cached else "failed",
                    "payload": (cached or {}).get("payload") if cached else None,
                    "verified_at": (cached or {}).get("verified_at") if cached else None,
                }
            ]
            await _send(
                runtime,
                update,
                render_version_all(rows, locale=locale, current_instance=local_id),
            )
            return
        cache[target] = {
            "verified_at": payload.get("collected_at"),
            "payload": payload,
        }
        await asyncio.to_thread(save_version_cache, root, cache)
        await _send(runtime, update, render_version_card(payload, locale=locale))
        return

    rows: list[dict[str, Any]] = [
        {"instance_id": local_id, "verification": "live", "payload": local_payload}
    ]

    async def query_one(target: str, entry: dict[str, Any]) -> dict[str, Any]:
        if entry.get("active") is False:
            cached = cache.get(target)
            return {
                "instance_id": target,
                "verification": "stale" if cached else "failed",
                "payload": (cached or {}).get("payload") if cached else None,
                "verified_at": (cached or {}).get("verified_at") if cached else None,
            }
        try:
            payload = await asyncio.to_thread(
                query_remote_version,
                entry,
                target_instance=target,
                source_instance=local_id,
                shared_token=token or "",
            )
        except VersionQueryError:
            cached = cache.get(target)
            return {
                "instance_id": target,
                "verification": "stale" if cached else "failed",
                "payload": (cached or {}).get("payload") if cached else None,
                "verified_at": (cached or {}).get("verified_at") if cached else None,
            }
        cache[target] = {
            "verified_at": payload.get("collected_at"),
            "payload": payload,
        }
        return {"instance_id": target, "verification": "live", "payload": payload}

    tasks = [
        query_one(str(entry.get("instance_id") or key).upper(), dict(entry))
        for key, entry in instances.items()
    ]
    if tasks:
        rows.extend(await asyncio.gather(*tasks))
    rows.sort(key=lambda row: str(row.get("instance_id") or "").casefold())
    if cache:
        await asyncio.to_thread(save_version_cache, root, cache)
    await _send(
        runtime,
        update,
        render_version_all(rows, locale=locale, current_instance=local_id),
    )
