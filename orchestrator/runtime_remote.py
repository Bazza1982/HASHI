from __future__ import annotations

import asyncio
import html
import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import remote_lifecycle, runtime_pending, ui_language
from orchestrator.agent_move.coordinator import (
    cancel_outbound_move,
    confirm_outbound_move,
    get_outbound_move,
    prepare_outbound_move,
    preview_outbound_move,
)
from orchestrator.agent_move.package import AgentMoveError
from orchestrator.agent_move.source_guard import source_move_guard_state
from orchestrator.command_ui import back_label, card_title, status_label

_TELEGRAM_CALLBACK_DATA_BYTES = 64
_MOVE_CALLBACK_CONTEXT_LIMIT = 128


def _move_callback_data(runtime: Any, raw: str) -> str:
    """Keep long Agent IDs out of Telegram's 64-byte callback field."""

    if len(raw.encode("utf-8")) <= _TELEGRAM_CALLBACK_DATA_BYTES:
        return raw
    contexts = getattr(runtime, "_move_callback_contexts", None)
    if not isinstance(contexts, dict):
        contexts = {}
        setattr(runtime, "_move_callback_contexts", contexts)
    token = secrets.token_hex(8)
    contexts[token] = raw
    while len(contexts) > _MOVE_CALLBACK_CONTEXT_LIMIT:
        contexts.pop(next(iter(contexts)))
    return f"move:ref:{token}"


def _resolve_move_callback_data(runtime: Any, raw: str) -> str | None:
    if not raw.startswith("move:ref:"):
        return raw
    token = raw.removeprefix("move:ref:")
    contexts = getattr(runtime, "_move_callback_contexts", None)
    if not isinstance(contexts, dict):
        return None
    value = contexts.get(token)
    return str(value) if value else None


def _find_agent_runtime(runtime: Any, agent_id: str) -> Any | None:
    return next(
        (
            candidate
            for candidate in getattr(
                getattr(runtime, "orchestrator", None), "runtimes", []
            )
            if getattr(candidate, "name", None) == agent_id
        ),
        None,
    )


def _move_recovery_markup(package_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    ui_language.tr("remote.move.retry_recovery"),
                    callback_data=f"move:commit:{package_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    ui_language.tr("remote.move.reconcile_cancel"),
                    callback_data=f"move:abort:{package_id}",
                )
            ],
        ]
    )


class InstanceConfigurationError(ValueError):
    """The instance connection directory cannot be read safely."""


def instance_root(runtime: Any) -> Path:
    """Resolve mutable migration state from the instance, never its code artifact."""
    config = runtime.global_config
    if home := getattr(config, "bridge_home", None):
        return Path(home)
    if config_path := getattr(config, "config_path", None):
        return Path(config_path).parent
    return Path(config.project_root)


def load_instances(
    candidates: list[Path] | None = None, *, project_root: Path | str | None = None,
) -> dict:
    """Read live instance configuration independently of the code generation."""
    if candidates is None:
        if project_root is None:
            raise InstanceConfigurationError("Instance root is unavailable")
        candidates = [
            Path(project_root) / "instances.json",
            Path.home() / ".hashi" / "instances.json",
        ]
    for path in candidates:
        try:
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            raise InstanceConfigurationError("Cannot read instance configuration") from exc
        if not isinstance(data, dict) or not isinstance(data.get("instances", {}), dict):
            raise InstanceConfigurationError("Invalid instance directory")
        instances = data.get("instances", {})
        if any(not isinstance(entry, dict) for entry in instances.values()):
            raise InstanceConfigurationError("Invalid instance entry")
        return instances
    return {}


class MoveDiscoveryError(ValueError):
    """A localized failure to obtain the local Remote peer directory."""

    def __init__(self, message_key: str) -> None:
        super().__init__(message_key)
        self.message_key = message_key


async def load_move_instances(runtime: Any) -> dict[str, dict[str, Any]]:
    """Derive migration routes from Remote's trusted live peer view."""
    data, _url = await runtime._fetch_remote_json("/peers")
    if not isinstance(data, dict) or data.get("ok") is False:
        raise MoveDiscoveryError("move.remote_unavailable")
    if data.get("trusted_view") is False:
        raise MoveDiscoveryError("move.remote_untrusted")
    peers = data.get("peers")
    if not isinstance(peers, list):
        raise MoveDiscoveryError("move.remote_unavailable")
    current_instance = str(runtime.global_config.instance_id or "").strip().upper()
    instances = {}
    for peer in peers:
        if not isinstance(peer, dict):
            continue
        instance_id = str(peer.get("instance_id") or "").strip().upper()
        if not instance_id or instance_id in {current_instance, "UNKNOWN"}:
            continue
        props = peer.get("properties") or {}
        host = str(peer.get("resolved_route_host") or peer.get("host") or "").strip()
        try:
            port = int(peer.get("resolved_route_port") or peer.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        rank, _label, _state = runtime._remote_peer_presence(peer)
        instances[instance_id.lower()] = {
            **peer,
            "instance_id": instance_id,
            "host": host,
            "remote_port": port,
            "environment_kind": props.get("environment_kind") or peer.get("platform"),
            "active": rank == 0 and bool(host) and 0 < port <= 65535,
        }
    return instances


def _move_target_error(target: str, instances: dict) -> str | None:
    entry = next(
        (value for key, value in instances.items()
         if str(key).casefold() == str(target).casefold()
         or str(value.get("instance_id") or "").casefold() == str(target).casefold()),
        None,
    )
    if entry is None:
        key = "remote.move.unknown_target"
    elif entry.get("active") is False:
        key = "remote.move.target_unavailable"
    elif "capabilities" in entry and "agent_move_receive_v1" not in (entry.get("capabilities") or []):
        key = "remote.move.target_unsupported"
    else:
        return None
    return ui_language.tr(key, target=html.escape(str(target)))


async def move_show_agent_picker(runtime: Any, update: Any, instances: dict) -> None:
    """Step 1: pick which agent to move from the current instance."""
    root = instance_root(runtime)
    try:
        with open(root / "agents.json", encoding="utf-8-sig") as f:
            data = json.load(f)
        agents = data if isinstance(data, list) else data.get("agents", [])
        agent_names = [
            ag.get("name") or ag.get("id")
            for ag in agents
            if (ag.get("name") or ag.get("id"))
            and ag.get("is_active", True) is not False
            and ag.get("transfer_state") != "moved_out_pending_reboot"
        ]
    except Exception:
        agent_names = []

    if not agent_names:
        await runtime._reply_text(update, ui_language.tr("remote.move.no_agents"))
        return

    rows = [
        [
            InlineKeyboardButton(
                f"🤖 {name}",
                callback_data=_move_callback_data(runtime, f"move:agent:{name}"),
            )
        ]
        for name in agent_names
    ]
    markup = InlineKeyboardMarkup(rows)
    instance_id = str(
        getattr(getattr(runtime, "global_config", None), "instance_id", None) or "HASHI"
    )
    await runtime._reply_text(
        update,
        f"{card_title('📦', 'Move agent')}\n\n"
        f"<b>{html.escape(ui_language.tr('common.current'))}</b> · "
        f"{ui_language.tr('remote.move.current_source', instance=f'<code>{html.escape(instance_id)}</code>')}\n\n"
        f"{ui_language.tr('remote.move.select_agent')}",
        parse_mode="HTML",
        reply_markup=markup,
    )


async def move_show_target_picker(runtime: Any, update: Any, agent_id: str, instances: dict) -> None:
    """Step 2: pick target instance."""
    rows = []
    current_instance = str(
        getattr(getattr(runtime, "global_config", None), "instance_id", None) or "HASHI"
    ).upper()
    for name, inst in instances.items():
        instance_id = str(inst.get("instance_id") or name).upper()
        if instance_id == current_instance or inst.get("active") is False:
            continue
        label = inst.get("display_name", name)
        rows.append(
            [
                InlineKeyboardButton(
                    f"📦 {label}",
                    callback_data=_move_callback_data(
                        runtime,
                        f"move:target:{agent_id}:{name}",
                    ),
                )
            ]
        )
    if not rows:
        await runtime._reply_text(update, ui_language.tr("remote.move.no_targets"))
        return
    markup = InlineKeyboardMarkup(rows)
    await runtime._reply_text(
        update,
        f"{card_title('📦', 'Move agent')}\n\n"
        f"<b>{html.escape(ui_language.tr('common.agent'))}</b> · <code>{html.escape(agent_id)}</code>\n\n"
        f"{ui_language.tr('remote.move.select_target')}",
        parse_mode="HTML",
        reply_markup=markup,
    )


async def move_show_options(runtime: Any, update: Any, agent_id: str, target: str) -> None:
    """Step 3: show move options."""
    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                ui_language.tr("remote.move.button.safe_move"),
                callback_data=_move_callback_data(
                    runtime, f"move:exec:{agent_id}:{target}:move"
                ),
            ),
            InlineKeyboardButton(
                ui_language.tr("remote.move.button.copy"),
                callback_data=_move_callback_data(
                    runtime, f"move:exec:{agent_id}:{target}:keep"
                ),
            ),
        ],
        [
            InlineKeyboardButton(
                ui_language.tr("remote.move.button.preview"),
                callback_data=_move_callback_data(
                    runtime, f"move:exec:{agent_id}:{target}:dry"
                ),
            ),
        ],
        [InlineKeyboardButton(ui_language.tr("remote.move.button.keep"), callback_data="move:cancel")],
    ])
    await update.callback_query.edit_message_text(
        f"{card_title('📦', 'Move agent')}\n\n"
        f"<b>{html.escape(ui_language.tr('common.agent'))}</b> · <code>{html.escape(agent_id)}</code>\n"
        f"<b>{html.escape(ui_language.tr('common.target'))}</b> · <code>{html.escape(target)}</code>\n\n"
        f"{ui_language.tr('remote.move.choose_safe')}",
        parse_mode="HTML",
        reply_markup=markup,
    )


async def do_move(
    runtime: Any,
    update: Any,
    agent_id: str,
    target: str,
    instances: dict,
    *,
    keep_source: bool = False,
    sync: bool = False,
    dry_run: bool = False,
) -> None:
    chat_id = update.effective_chat.id

    if sync:
        await runtime._send_text(
            chat_id,
            ui_language.tr("remote.move.sync_retired"),
            parse_mode="HTML",
        )
        return

    delayed = await runtime_pending.delayed_count(runtime, agent_name=agent_id)
    if delayed:
        await runtime._send_text(
            chat_id,
            f"Move is blocked while <code>{html.escape(agent_id)}</code> has "
            f"<code>{delayed}</code> delayed message(s). Use /recall from that agent first.",
            parse_mode="HTML",
        )
        return

    target_error = _move_target_error(target, instances)
    if target_error:
        await runtime._send_text(chat_id, target_error, parse_mode="HTML")
        return

    selected_runtime = _find_agent_runtime(runtime, agent_id)
    busy_check = getattr(selected_runtime, "_backend_busy", None)
    if callable(busy_check) and busy_check():
        await runtime._send_text(
            chat_id,
            ui_language.tr("remote.move.agent_busy", agent=html.escape(agent_id)),
            parse_mode="HTML",
        )
        return

    operation = "preview" if dry_run else "prepare"
    await runtime._send_text(
        chat_id,
        ui_language.tr(
            f"remote.move.{operation}_started",
            agent=html.escape(agent_id),
            target=html.escape(target),
        ),
        parse_mode="HTML",
    )

    global_config = getattr(runtime, "global_config", None)
    project_root = instance_root(runtime)
    source_instance = str(getattr(global_config, "instance_id", None) or "HASHI")
    try:
        if dry_run:
            result = await asyncio.to_thread(
                preview_outbound_move,
                project_root,
                instances,
                agent_id,
                target,
                source_instance=source_instance,
            )
            await runtime._send_text(
                chat_id,
                _render_move_preview(result),
                parse_mode="HTML",
            )
            return

        result = await asyncio.to_thread(
            prepare_outbound_move,
            project_root,
            instances,
            agent_id,
            target,
            source_instance=source_instance,
            keep_source=keep_source,
        )
        package_id = str(result["package_id"])
        confirmation_key = "remote.move.confirm_copy" if keep_source else "remote.move.confirm_move"
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        ui_language.tr(confirmation_key),
                        callback_data=f"move:commit:{package_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        ui_language.tr("remote.move.cancel_prepared"),
                        callback_data=f"move:abort:{package_id}",
                    )
                ],
            ]
        )
        await runtime._send_text(
            chat_id,
            _render_move_prepared(result),
            parse_mode="HTML",
            reply_markup=markup,
        )
    except (AgentMoveError, OSError) as exc:
        await runtime._send_text(
            chat_id,
            ui_language.tr("remote.move.failed", error=html.escape(str(exc))),
            parse_mode="HTML",
        )


def _render_move_preview(result: dict[str, Any]) -> str:
    cross_platform = result.get("source_environment") != result.get("target_environment")
    lines = [
        card_title("🔎", "Agent move preview"),
        "",
        f"<b>{html.escape(ui_language.tr('common.agent'))}</b> · <code>{html.escape(str(result.get('agent_id')))}</code>",
        f"<b>{html.escape(ui_language.tr('common.target'))}</b> · <code>{html.escape(str(result.get('target_instance')))}</code>",
        f"<b>{html.escape(ui_language.tr('remote.move.package_size'))}</b> · <code>{int(result.get('package_bytes') or 0):,} B</code>",
        f"<b>{html.escape(ui_language.tr('remote.move.workspace_files'))}</b> · <code>{int(result.get('workspace_files') or 0)}</code>",
        f"<b>{html.escape(ui_language.tr('remote.move.excluded_paths'))}</b> · <code>{int(result.get('excluded_count') or 0)}</code>",
        f"<b>{html.escape(ui_language.tr('remote.move.schedules'))}</b> · <code>{int(result.get('schedule_count') or 0)}</code>",
        f"<b>{html.escape(ui_language.tr('remote.move.platform'))}</b> · <code>{html.escape(str(result.get('source_environment')))} → {html.escape(str(result.get('target_environment')))}</code>",
        "",
        ui_language.tr("remote.move.preview_clean"),
    ]
    if cross_platform:
        lines.append(ui_language.tr("remote.move.cross_platform"))
    lines.extend(_render_move_review_notes(result))
    return "\n".join(lines)


def _render_move_prepared(result: dict[str, Any]) -> str:
    credentials = result.get("credential_status") or {}
    missing = list(credentials.get("missing_keys") or [])
    lines = [
        card_title("🛡️", "Agent move prepared"),
        "",
        f"<b>{html.escape(ui_language.tr('common.agent'))}</b> · <code>{html.escape(str(result.get('agent_id')))}</code>",
        f"<b>{html.escape(ui_language.tr('common.target'))}</b> · <code>{html.escape(str(result.get('target_instance')))}</code>",
        f"<b>{html.escape(ui_language.tr('remote.move.package_id'))}</b> · <code>{html.escape(str(result.get('package_id')))}</code>",
        f"<b>{html.escape(ui_language.tr('remote.move.workspace_files'))}</b> · <code>{int(result.get('workspace_files') or 0)}</code>",
        f"<b>{html.escape(ui_language.tr('remote.move.excluded_paths'))}</b> · <code>{int(result.get('excluded_count') or 0)}</code>",
        f"<b>{html.escape(ui_language.tr('remote.move.schedules'))}</b> · <code>{int(result.get('schedule_count') or 0)}</code>",
        "",
        ui_language.tr("remote.move.prepared_safe"),
    ]
    if missing:
        lines.append(
            ui_language.tr(
                "remote.move.credentials_missing",
                keys=html.escape(", ".join(str(item) for item in missing)),
            )
        )
    lines.extend(_render_move_review_notes(result))
    return "\n".join(lines)


def _render_move_review_notes(result: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    retained = result.get("retained_identity")
    if isinstance(retained, dict):
        storage_path = str(retained.get("storage_path") or "").strip()
        key = (
            "remote.move.retained_identity_stored"
            if storage_path
            else "remote.move.retained_identity_preview"
        )
        lines.append(
            ui_language.tr(
                key,
                sha256=html.escape(str(retained.get("sha256") or "unknown")),
                path=html.escape(storage_path or "target transaction storage"),
            )
        )
    rebind = list(result.get("target_rebind_required") or [])
    if rebind:
        lines.append(
            ui_language.tr(
                "remote.move.target_rebind",
                items=html.escape(", ".join(str(item) for item in rebind)),
            )
        )
    warnings = [str(item) for item in result.get("warnings") or [] if str(item)]
    if warnings:
        lines.append(ui_language.tr("remote.move.review_notes"))
        lines.extend(f"  • {html.escape(item)}" for item in warnings[:8])
    return lines


def _render_move_complete(result: dict[str, Any]) -> str:
    if result.get("status") == "copied_inactive":
        body = (
            f"{card_title('✅', 'Agent copied inactive')}\n\n"
            f"{ui_language.tr('remote.move.copy_complete', agent=html.escape(str(result.get('agent_id'))), target=html.escape(str(result.get('target_instance'))))}"
        )
        return "\n".join([body, *_render_move_review_notes(result)])
    order = list(result.get("reboot_order") or [])
    source = html.escape(str(order[0] if order else result.get("source_instance") or "source"))
    target = html.escape(str(order[1] if len(order) > 1 else result.get("target_instance") or "target"))
    body = (
        f"{card_title('✅', 'Agent move committed')}\n\n"
        f"{ui_language.tr('remote.move.move_complete', agent=html.escape(str(result.get('agent_id'))), target=html.escape(str(result.get('target_instance'))))}\n\n"
        f"{ui_language.tr('remote.move.reboot_order', source=source, target=target, agent=html.escape(str(result.get('agent_id'))))}"
    )
    return "\n".join([body, *_render_move_review_notes(result)])


def render_remote_peer_lines(
    runtime: Any,
    peers: list[dict[str, Any]],
    *,
    include_refreshed_at: bool = True,
    include_title: bool = True,
) -> list[str]:
    peers = sorted(
        peers,
        key=lambda peer: (
            runtime._remote_peer_presence(peer)[0],
            str(peer.get("instance_id") or ""),
        ),
    )
    counts = {"online": 0, "attention": 0, "offline": 0}
    for peer in peers:
        rank, _presence, _state = runtime._remote_peer_presence(peer)
        if rank == 0:
            counts["online"] += 1
        elif rank in {1, 2}:
            counts["attention"] += 1
        else:
            counts["offline"] += 1
    online_count = f"<code>{counts['online']}</code>"
    lines = [card_title("📡", "Remote instances"), ""] if include_title else []
    lines.extend(
        [
            f"<b>{html.escape(ui_language.tr('common.current'))}</b> · "
            f"{ui_language.tr('remote.peers.online', count=online_count)}",
            f"<b>{html.escape(ui_language.tr('remote.peers.attention'))}</b> · <code>{counts['attention']}</code>",
            f"<b>{html.escape(ui_language.tr('remote.peers.offline'))}</b> · <code>{counts['offline']}</code>",
        ]
    )
    if include_refreshed_at:
        lines.append(
            f"<b>{html.escape(ui_language.tr('remote.peers.refreshed'))}</b> · "
            f"<code>{datetime.now().strftime('%H:%M:%S')}</code>"
        )
    lines.append("")
    if not peers:
        lines.append(ui_language.tr("remote.peers.none"))
    for idx, peer in enumerate(peers):
        lines.extend(runtime._render_remote_peer_block(peer))
        if idx != len(peers) - 1:
            lines.append("")
    return lines


async def handle_move_callback(runtime: Any, update: Any, context: Any) -> None:
    try:
        await _handle_move_callback(runtime, update, context)
    except MoveDiscoveryError as exc:
        data = _resolve_move_callback_data(runtime, update.callback_query.data or "") or ""
        parts = data.split(":")
        markup = (
            _move_recovery_markup(parts[2])
            if len(parts) == 3 and parts[1] in {"commit", "abort"} else None
        )
        await update.callback_query.edit_message_text(
            ui_language.tr(exc.message_key), reply_markup=markup,
        )


async def _handle_move_callback(runtime: Any, update: Any, context: Any) -> None:
    """Handle move: callback queries."""
    query = update.callback_query
    if not runtime._is_authorized_user(query.from_user.id):
        await query.answer()
        return
    await query.answer()

    data = _resolve_move_callback_data(runtime, query.data or "")
    if data is None:
        await query.edit_message_text(ui_language.tr("remote.move.selection_expired"))
        return
    parts = data.split(":", 3)

    if len(parts) < 2:
        return

    action = parts[1] if len(parts) > 1 else ""

    if action == "cancel":
        await query.edit_message_text(ui_language.tr("remote.move.cancelled"))
        return

    if action == "agent" and len(parts) >= 3:
        agent_id = parts[2]
        instances = await load_move_instances(runtime)
        rows = []
        current_instance = str(
            getattr(getattr(runtime, "global_config", None), "instance_id", None) or "HASHI"
        ).upper()
        for name, inst in instances.items():
            if str(inst.get("instance_id") or name).upper() == current_instance or inst.get("active") is False:
                continue
            label = inst.get("display_name", name)
            rows.append(
                [
                    InlineKeyboardButton(
                        f"📦 {label}",
                        callback_data=_move_callback_data(
                            runtime,
                            f"move:target:{agent_id}:{name}",
                        ),
                    )
                ]
            )
        if not rows:
            await query.edit_message_text(ui_language.tr("remote.move.no_targets"))
            return
        rows.append([InlineKeyboardButton(back_label(), callback_data="move:cancel")])
        markup = InlineKeyboardMarkup(rows)
        await query.edit_message_text(
            f"{card_title('📦', 'Move agent')}\n\n"
            f"<b>{html.escape(ui_language.tr('common.agent'))}</b> · <code>{html.escape(agent_id)}</code>\n\n"
            f"{ui_language.tr('remote.move.select_target')}",
            parse_mode="HTML",
            reply_markup=markup,
        )
        return

    if action == "target" and len(parts) >= 4:
        agent_id = parts[2]
        target = parts[3]
        instances = await load_move_instances(runtime)
        target_error = _move_target_error(target, instances)
        if target_error:
            await query.edit_message_text(target_error, parse_mode="HTML")
            return
        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    ui_language.tr("remote.move.button.safe_move"),
                    callback_data=_move_callback_data(
                        runtime, f"move:exec:{agent_id}:{target}:move"
                    ),
                ),
                InlineKeyboardButton(
                    ui_language.tr("remote.move.button.copy"),
                    callback_data=_move_callback_data(
                        runtime, f"move:exec:{agent_id}:{target}:keep"
                    ),
                ),
            ],
            [
                InlineKeyboardButton(
                    ui_language.tr("remote.move.button.preview"),
                    callback_data=_move_callback_data(
                        runtime, f"move:exec:{agent_id}:{target}:dry"
                    ),
                ),
            ],
            [InlineKeyboardButton(ui_language.tr("remote.move.button.keep"), callback_data="move:cancel")],
        ])
        await query.edit_message_text(
            f"{card_title('📦', 'Move agent')}\n\n"
            f"<b>{html.escape(ui_language.tr('common.agent'))}</b> · <code>{html.escape(agent_id)}</code>\n"
            f"<b>{html.escape(ui_language.tr('common.target'))}</b> · <code>{html.escape(target)}</code>\n\n"
            f"{ui_language.tr('remote.move.choose_safe')}",
            parse_mode="HTML",
            reply_markup=markup,
        )
        return

    if action == "exec" and len(parts) >= 4:
        agent_id = parts[2]
        target_mode = parts[3].split(":", 1)
        target = target_mode[0]
        mode = target_mode[1] if len(target_mode) > 1 else "plain"

        keep = mode == "keep"
        sync = mode == "sync"
        dry = mode == "dry"
        instances = await load_move_instances(runtime)
        await runtime._do_move(update, agent_id, target, instances, keep_source=keep, sync=sync, dry_run=dry)
        return

    if action in {"commit", "abort"} and len(parts) >= 3:
        package_id = parts[2]
        project_root = instance_root(runtime)
        instances = await load_move_instances(runtime)
        await query.edit_message_text(
            ui_language.tr(
                "remote.move.committing" if action == "commit" else "remote.move.cancelling"
            ),
            parse_mode="HTML",
        )
        selected_runtime = None
        try:
            outbound = await asyncio.to_thread(
                get_outbound_move,
                project_root,
                package_id,
            )
            agent_id = str(outbound.get("agent_id") or "")
            target_instance = str(outbound.get("target_instance") or "")
            selected_runtime = _find_agent_runtime(runtime, agent_id)
            if selected_runtime is not None:
                selected_runtime._agent_move_quiesced = True
                selected_runtime._agent_move_target_instance = target_instance

            if action == "commit":
                delayed = await runtime_pending.delayed_count(
                    runtime,
                    agent_name=agent_id,
                )
                busy_check = getattr(selected_runtime, "_backend_busy", None)
                if delayed or (callable(busy_check) and busy_check()):
                    if selected_runtime is not None:
                        selected_runtime._agent_move_quiesced = False
                    await query.edit_message_text(
                        ui_language.tr(
                            "remote.move.agent_busy",
                            agent=html.escape(agent_id),
                        ),
                        parse_mode="HTML",
                        reply_markup=_move_recovery_markup(package_id),
                    )
                    return
            if action == "commit":
                result = await asyncio.to_thread(
                    confirm_outbound_move,
                    project_root,
                    instances,
                    package_id,
                )
                await query.edit_message_text(
                    _render_move_complete(result),
                    parse_mode="HTML",
                )
                if (
                    selected_runtime is not None
                    and result.get("status") == "copied_inactive"
                ):
                    selected_runtime._agent_move_quiesced = False
            else:
                await asyncio.to_thread(
                    cancel_outbound_move,
                    project_root,
                    instances,
                    package_id,
                )
                await query.edit_message_text(
                    ui_language.tr("remote.move.prepared_cancelled"),
                    parse_mode="HTML",
                )
                if selected_runtime is not None:
                    selected_runtime._agent_move_quiesced = False
        except (AgentMoveError, OSError) as exc:
            if selected_runtime is not None and source_move_guard_state(
                project_root, str(getattr(selected_runtime, "name", "") or "")
            ) is None:
                selected_runtime._agent_move_quiesced = False
            await query.edit_message_text(
                f"{ui_language.tr('remote.move.failed', error=html.escape(str(exc)))}\n\n"
                f"{ui_language.tr('remote.move.recovery_hint')}",
                parse_mode="HTML",
                reply_markup=_move_recovery_markup(package_id),
            )


async def cmd_remote(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    arg = (context.args[0].lower() if context.args else "").strip()
    cfg = runtime._remote_config_snapshot()
    lifecycle = remote_lifecycle.load_settings(cfg["root"])
    disabled = remote_lifecycle.read_disabled_state(cfg["root"])
    alive = runtime._remote_process is not None and runtime._remote_process.returncode is None

    if arg == "status" or not arg:
        health, health_url = await runtime._fetch_remote_json("/health")
        status, _status_url = await runtime._fetch_remote_json("/protocol/status")
        if not health:
            if alive:
                await runtime._reply_text(
                    update,
                    f"{card_title('📡', 'Hashi remote')}\n\n"
                    f"<b>{html.escape(ui_language.tr('common.current'))}</b> · "
                    f"<code>{html.escape(ui_language.tr('remote.status.attention'))}</code>\n"
                    f"{ui_language.tr('remote.status.api_unresponsive')}\n\n"
                    f"<b>{html.escape(ui_language.tr('remote.status.pid'))}</b> · "
                    f"<code>{runtime._remote_process.pid}</code>\n"
                    f"<b>{html.escape(ui_language.tr('remote.status.port'))}</b> · <code>{cfg['port']}</code>\n"
                    f"<b>{html.escape(ui_language.tr('remote.status.tls'))}</b> · "
                    f"<code>{status_label(bool(cfg['use_tls']))}</code>",
                    parse_mode="HTML",
                )
            else:
                lines = [
                    card_title("📡", "Hashi remote"),
                    "",
                    f"<b>{html.escape(ui_language.tr('common.current'))}</b> · "
                    f"<code>{html.escape(ui_language.tr('common.off'))}</code>",
                    f"{html.escape(ui_language.tr('remote.status.lifecycle'))}: "
                    f"<code>{'enabled' if lifecycle.enabled else 'disabled_by_config'}</code>",
                    f"{html.escape(ui_language.tr('remote.status.supervisor'))}: "
                    f"<code>{'requested' if lifecycle.supervised else 'child_fallback'}</code>",
                    f"{html.escape(ui_language.tr('remote.status.disabled_state'))}: "
                    f"<code>{'yes' if disabled else 'no'}</code>",
                ]
                if disabled:
                    lines.append(
                        f"{html.escape(ui_language.tr('remote.status.reason'))}: "
                        f"<code>{html.escape(str(disabled.get('reason') or ui_language.tr('common.unknown')))}</code>"
                    )
                    lines.append(
                        f"{html.escape(ui_language.tr('remote.status.state_file'))}: "
                        f"<code>{html.escape(str(lifecycle.disabled_path))}</code>"
                    )
                lines.append(ui_language.tr("remote.status.start_help"))
                await runtime._reply_text(update, "\n".join(lines), parse_mode="HTML")
            return
        instance = health.get("instance") or {}
        peers = list((health.get("peers") or []))
        lines = [
            card_title("📡", "Hashi remote"),
            "",
            f"<b>{html.escape(ui_language.tr('common.current'))}</b> · "
            f"<code>{html.escape(ui_language.tr('common.on'))}</code>",
            f"<b>{html.escape(ui_language.tr('remote.status.instance'))}</b> · "
            f"<code>{html.escape(str(instance.get('instance_id') or runtime.global_config.project_root.name.upper()))}</code>",
            f"<b>API</b> · <code>{html.escape(str(health_url))}</code>",
        ]
        if disabled:
            lines.append(f"{ui_language.tr('remote.status.disabled')}: <code>disabled</code>")
            lines.append(
                f"{ui_language.tr('remote.status.disabled_reason')}: "
                f"<code>{html.escape(str(disabled.get('reason') or ui_language.tr('common.unknown')))}</code>"
            )
        if status:
            shared_token = bool(status.get("shared_token_configured") or health.get("shared_token_configured"))
            lan_mode = bool(status.get("lan_mode") if "lan_mode" in status else health.get("lan_mode"))
            if not shared_token:
                lines.append(
                    f"{ui_language.tr('remote.status.security')}: <code>discovery-only</code> — "
                    f"{ui_language.tr('remote.status.discovery_unavailable')}"
                )
            elif lan_mode:
                lines.append(
                    f"{ui_language.tr('remote.status.security')}: <code>token ok</code>  ·  "
                    f"{ui_language.tr('remote.status.lan_relaxed')}: <code>on</code>"
                )
            route_diagnostics = status.get("route_diagnostics") or {}
            conflicts = list(route_diagnostics.get("port_conflicts") or [])
            if conflicts:
                lines.append(
                    f"{ui_language.tr('remote.status.route_warnings')}: "
                    + ui_language.tr(
                        "remote.status.port_conflicts",
                        count=f"<code>{len(conflicts)}</code>",
                    )
                )
        if peers:
            lines.extend(
                [
                    "",
                    *render_remote_peer_lines(
                        runtime,
                        peers,
                        include_refreshed_at=False,
                        include_title=False,
                    ),
                ]
            )
        lines.extend(
            [
                "",
                ui_language.tr("remote.status.control_help"),
            ]
        )
        await runtime._reply_text(update, "\n".join(lines), parse_mode="HTML")
        return

    if arg == "list":
        data, _url = await runtime._fetch_remote_json("/peers")
        if data is None:
            await runtime._reply_text(
                update,
                ui_language.tr("remote.status.unavailable"),
            )
            return
        peers = list((data or {}).get("peers") or [])
        if not peers:
            if data and data.get("trusted_view") is False:
                await runtime._reply_text(update, ui_language.tr("remote.status.untrusted"))
            else:
                await runtime._reply_text(update, ui_language.tr("remote.status.none"))
            return
        await runtime._reply_text(
            update,
            "\n".join(render_remote_peer_lines(runtime, peers, include_refreshed_at=True)),
            parse_mode="HTML",
        )
        return

    if arg == "off":
        state_path = remote_lifecycle.write_disabled_state(cfg["root"])
        stopped = await remote_lifecycle.stop_remote(cfg["root"])
        if not stopped.get("ok"):
            await runtime._reply_text(
                update,
                ui_language.tr(
                    "remote.lifecycle.stop_failed",
                    state=html.escape(str(state_path)),
                    reason=html.escape(str(stopped.get("reason") or "unknown")),
                ),
                parse_mode="HTML",
            )
            return
        runtime._remote_process = None
        message_key = (
            "remote.lifecycle.disabled"
            if stopped.get("action") == "already_stopped"
            else "remote.lifecycle.stopped"
        )
        await runtime._reply_text(
            update,
            ui_language.tr(
                message_key,
                state=html.escape(str(state_path)),
            ),
            parse_mode="HTML",
        )
        return

    if arg == "on":
        remote_lifecycle.clear_disabled_state(cfg["root"])
        if alive:
            await runtime._reply_text(
                update,
                ui_language.tr(
                    "remote.lifecycle.already_running",
                    pid=runtime._remote_process.pid,
                ),
            )
            return
        try:
            started = await remote_lifecycle.ensure_remote_started(cfg["root"])
        except Exception as exc:
            started = {
                "ok": False,
                "reason": f"{type(exc).__name__}: {exc}",
            }
        if not started.get("ok"):
            runtime._remote_process = None
            await runtime._reply_text(
                update,
                ui_language.tr(
                    "remote.lifecycle.activation_failed",
                    reason=html.escape(str(started.get("reason") or "unknown")),
                ),
                parse_mode="HTML",
            )
            return
        process = started.get("process")
        if process is not None:
            runtime._remote_process = process
            cmd = remote_lifecycle.build_child_command(lifecycle)
            log_path = Path(started.get("log_path") or runtime._remote_start_log_path())
            ok, detail = await runtime._await_remote_start_health(
                process=process,
                cfg=cfg,
                cmd=cmd,
                log_path=log_path,
            )
            if not ok:
                runtime._remote_process = None
                await runtime._reply_text(update, detail, parse_mode="HTML")
                return
        else:
            port = int(started.get("port") or cfg["port"])
            host = str(started.get("health_host") or "127.0.0.1")
            scheme = "https" if cfg["use_tls"] else "http"
            detail = f"{scheme}://{host}:{port}/health"
        action = str(started.get("action") or "started")
        mode = (
            "supervisor"
            if action == "started_supervisor"
            else "bundled child"
            if action in {"started_child", "started_child_fallback"}
            else "existing service"
        )
        await runtime._reply_text(
            update,
            ui_language.tr(
                "remote.lifecycle.activated",
                mode=html.escape(mode),
                port=int(started.get("port") or cfg["port"]),
                tls=ui_language.tr(
                    "remote.lifecycle.on" if cfg["use_tls"] else "remote.lifecycle.off"
                ),
                discovery=html.escape(str(cfg["backend"])),
                api=html.escape(detail),
            ),
            parse_mode="HTML",
        )
        return

    await runtime._reply_text(
        update,
        f"{card_title('📡', 'Hashi remote')}\n\n"
        f"<b>{html.escape(ui_language.tr('common.current'))}</b> · "
        f"{html.escape(ui_language.tr('remote.invalid'))}\n\n"
        f"{ui_language.tr('remote.invalid_help')}",
        parse_mode="HTML",
    )
