from __future__ import annotations

import json
import asyncio
import html
import subprocess
import sys
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from orchestrator.command_ui import back_label, card_title, status_label
from orchestrator import remote_lifecycle, runtime_pending, ui_language
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def load_instances(candidates: list[Path] | None = None) -> dict:
    """Load instances.json from the project root or ~/.hashi/instances.json."""
    if candidates is None:
        candidates = [
            Path(__file__).parent.parent / "instances.json",
            Path.home() / ".hashi" / "instances.json",
        ]
    for path in candidates:
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data.get("instances", {})
    return {}


async def move_show_agent_picker(runtime: Any, update: Any, instances: dict) -> None:
    """Step 1: pick which agent to move from the current instance."""
    root = getattr(getattr(runtime, "global_config", None), "project_root", None) or Path(__file__).parent.parent
    try:
        with open(Path(root) / "agents.json", encoding="utf-8") as f:
            data = json.load(f)
        agents = data if isinstance(data, list) else data.get("agents", [])
        agent_names = [ag.get("name") or ag.get("id", "?") for ag in agents if ag.get("name")]
    except Exception:
        agent_names = []

    if not agent_names:
        await runtime._reply_text(update, ui_language.tr("remote.move.no_agents"))
        return

    rows = [[InlineKeyboardButton(f"🤖 {name}", callback_data=f"move:agent:{name}")] for name in agent_names]
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
    for name, inst in instances.items():
        label = inst.get("display_name", name)
        rows.append([InlineKeyboardButton(f"📦 {label}", callback_data=f"move:target:{agent_id}:{name}")])
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
            InlineKeyboardButton(ui_language.tr("remote.move.button.encrypted"), callback_data=f"move:exec:{agent_id}:{target}:enc"),
            InlineKeyboardButton(ui_language.tr("remote.move.button.plain"), callback_data=f"move:exec:{agent_id}:{target}:plain"),
        ],
        [
            InlineKeyboardButton(ui_language.tr("remote.move.button.copy"), callback_data=f"move:exec:{agent_id}:{target}:keep"),
            InlineKeyboardButton(ui_language.tr("remote.move.button.sync"), callback_data=f"move:exec:{agent_id}:{target}:sync"),
        ],
        [InlineKeyboardButton(ui_language.tr("remote.move.button.keep"), callback_data="move:cancel")],
    ])
    await update.callback_query.edit_message_text(
        f"{card_title('📦', 'Move agent')}\n\n"
        f"<b>{html.escape(ui_language.tr('common.agent'))}</b> · <code>{html.escape(agent_id)}</code>\n"
        f"<b>{html.escape(ui_language.tr('common.target'))}</b> · <code>{html.escape(target)}</code>\n\n"
        f"{ui_language.tr('remote.move.choose_destructive')}",
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

    delayed = await runtime_pending.delayed_count(runtime, agent_name=agent_id)
    if delayed:
        await runtime._send_text(
            chat_id,
            f"Move is blocked while <code>{html.escape(agent_id)}</code> has "
            f"<code>{delayed}</code> delayed message(s). Use /recall from that agent first.",
            parse_mode="HTML",
        )
        return

    await runtime._send_text(chat_id, f"⏳ Moving <code>{agent_id}</code> → <b>{target}</b>…", parse_mode="HTML")

    global_config = getattr(runtime, "global_config", None)
    project_root = Path(
        getattr(global_config, "project_root", None) or Path(__file__).parent.parent
    )
    source_instance = str(getattr(global_config, "instance_id", None) or "HASHI")
    script = project_root / "scripts" / "move_agent.py"
    if not script.exists():
        await runtime._send_text(chat_id, "Error: move_agent.py not found.")
        return

    cmd = [
        sys.executable,
        str(script),
        agent_id,
        target,
        "--source-instance",
        source_instance,
    ]
    if keep_source:
        cmd.append("--keep-source")
    if sync:
        cmd.append("--sync")
    if dry_run:
        cmd.append("--dry-run")

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: subprocess.run(cmd, capture_output=True, text=True, cwd=str(project_root)),
        )
        output = (result.stdout + result.stderr).strip()
        if len(output) > 3000:
            output = output[:3000] + "\n…[truncated]"
        status = "✅" if result.returncode == 0 else "❌"
        await runtime._send_text(
            chat_id,
            f"{status} <b>{html.escape(ui_language.tr('remote.migration_result'))}：</b>\n<pre>{output}</pre>",
            parse_mode="HTML",
        )
    except Exception as exc:
        await runtime._send_text(chat_id, f"Error running migration: {exc}")


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
    """Handle move: callback queries."""
    query = update.callback_query
    if not runtime._is_authorized_user(query.from_user.id):
        await query.answer()
        return
    await query.answer()

    data = query.data or ""
    parts = data.split(":", 3)

    if len(parts) < 2:
        return

    action = parts[1] if len(parts) > 1 else ""

    if action == "cancel":
        await query.edit_message_text(ui_language.tr("remote.move.cancelled"))
        return

    if action == "agent" and len(parts) >= 3:
        agent_id = parts[2]
        instances = runtime._load_instances()
        rows = []
        for name, inst in instances.items():
            label = inst.get("display_name", name)
            rows.append([InlineKeyboardButton(f"📦 {label}", callback_data=f"move:target:{agent_id}:{name}")])
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
        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(ui_language.tr("remote.move.button.plain"), callback_data=f"move:exec:{agent_id}:{target}:plain"),
                InlineKeyboardButton(ui_language.tr("remote.move.button.copy"), callback_data=f"move:exec:{agent_id}:{target}:keep"),
            ],
            [
                InlineKeyboardButton(ui_language.tr("remote.move.button.sync"), callback_data=f"move:exec:{agent_id}:{target}:sync"),
                InlineKeyboardButton(ui_language.tr("remote.move.button.preview"), callback_data=f"move:exec:{agent_id}:{target}:dry"),
            ],
            [InlineKeyboardButton(ui_language.tr("remote.move.button.keep"), callback_data="move:cancel")],
        ])
        await query.edit_message_text(
            f"{card_title('📦', 'Move agent')}\n\n"
            f"<b>{html.escape(ui_language.tr('common.agent'))}</b> · <code>{html.escape(agent_id)}</code>\n"
            f"<b>{html.escape(ui_language.tr('common.target'))}</b> · <code>{html.escape(target)}</code>\n\n"
            f"{ui_language.tr('remote.move.choose_preview')}",
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
        instances = runtime._load_instances()
        await runtime._do_move(update, agent_id, target, instances, keep_source=keep, sync=sync, dry_run=dry)


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
        if runtime._remote_process is None or runtime._remote_process.returncode is not None:
            await runtime._reply_text(
                update,
                ui_language.tr(
                    "remote.lifecycle.disabled",
                    state=html.escape(str(state_path)),
                ),
                parse_mode="HTML",
            )
            return
        runtime._remote_process.terminate()
        try:
            await asyncio.wait_for(runtime._remote_process.wait(), timeout=5)
        except asyncio.TimeoutError:
            runtime._remote_process.kill()
        runtime._remote_process = None
        await runtime._reply_text(
            update,
            ui_language.tr(
                "remote.lifecycle.stopped",
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

        root = cfg["root"]
        venv_python = root / ".venv" / "bin" / "python3"
        if not venv_python.exists():
            venv_python = root / ".venv" / "Scripts" / "python.exe"
        if not venv_python.exists():
            await runtime._reply_text(
                update,
                ui_language.tr(
                    "remote.lifecycle.missing_interpreter",
                    path=html.escape(str(venv_python)),
                ),
                parse_mode="HTML",
            )
            return

        cmd = [str(venv_python), "-m", "remote", "--hashi-root", str(root)]
        cmd.extend(["--port", str(cfg["port"])])
        if not cfg["use_tls"]:
            cmd.append("--no-tls")
        if cfg["backend"] in {"lan", "tailscale", "both"}:
            cmd.extend(["--discovery", cfg["backend"]])
        log_path = runtime._remote_start_log_path()
        with suppress(Exception):
            log_path.unlink()
        log_handle = log_path.open("ab")
        try:
            runtime._remote_process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(root),
                stdout=log_handle,
                stderr=log_handle,
            )
        finally:
            log_handle.close()

        ok, detail = await runtime._await_remote_start_health(
            process=runtime._remote_process,
            cfg=cfg,
            cmd=cmd,
            log_path=log_path,
        )
        if not ok:
            runtime._remote_process = None
            await runtime._reply_text(update, detail, parse_mode="HTML")
            return
        await runtime._reply_text(
            update,
            ui_language.tr(
                "remote.lifecycle.started",
                pid=runtime._remote_process.pid,
                port=cfg["port"],
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
