from __future__ import annotations

import json
import asyncio
import html
import subprocess
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

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
        await runtime._reply_text(update, "No agents found in this instance.")
        return

    rows = [[InlineKeyboardButton(f"🤖 {name}", callback_data=f"move:agent:{name}")] for name in agent_names]
    markup = InlineKeyboardMarkup(rows)
    await runtime._reply_text(update, "<b>Move Agent</b> — select agent to move:", parse_mode="HTML", reply_markup=markup)


async def move_show_target_picker(runtime: Any, update: Any, agent_id: str, instances: dict) -> None:
    """Step 2: pick target instance."""
    rows = []
    for name, inst in instances.items():
        label = inst.get("display_name", name)
        rows.append([InlineKeyboardButton(f"📦 {label}", callback_data=f"move:target:{agent_id}:{name}")])
    markup = InlineKeyboardMarkup(rows)
    await runtime._reply_text(
        update,
        f"<b>Move <code>{agent_id}</code></b> — select target instance:",
        parse_mode="HTML",
        reply_markup=markup,
    )


async def move_show_options(runtime: Any, update: Any, agent_id: str, target: str) -> None:
    """Step 3: show move options."""
    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔒 Move + Encrypt", callback_data=f"move:exec:{agent_id}:{target}:enc"),
            InlineKeyboardButton("📋 Move Plain", callback_data=f"move:exec:{agent_id}:{target}:plain"),
        ],
        [
            InlineKeyboardButton("📂 Copy (keep source)", callback_data=f"move:exec:{agent_id}:{target}:keep"),
            InlineKeyboardButton("🔄 Sync memories", callback_data=f"move:exec:{agent_id}:{target}:sync"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="move:cancel")],
    ])
    await update.callback_query.edit_message_text(
        f"<b>Move <code>{agent_id}</code> → {target}</b>\n\nChoose move mode:",
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

    await runtime._send_text(chat_id, f"⏳ Moving <code>{agent_id}</code> → <b>{target}</b>…", parse_mode="HTML")

    project_root = Path(__file__).parent.parent
    script = project_root / "scripts" / "move_agent.py"
    if not script.exists():
        await runtime._send_text(chat_id, "Error: move_agent.py not found.")
        return

    cmd = [
        "python",
        str(script),
        agent_id,
        target,
        "--source-instance",
        "hashi2",
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
            f"{status} <b>Migration result:</b>\n<pre>{output}</pre>",
            parse_mode="HTML",
        )
    except Exception as exc:
        await runtime._send_text(chat_id, f"Error running migration: {exc}")


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
        await query.edit_message_text("Move cancelled.")
        return

    if action == "agent" and len(parts) >= 3:
        agent_id = parts[2]
        instances = runtime._load_instances()
        rows = []
        for name, inst in instances.items():
            label = inst.get("display_name", name)
            rows.append([InlineKeyboardButton(f"📦 {label}", callback_data=f"move:target:{agent_id}:{name}")])
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data="move:cancel")])
        markup = InlineKeyboardMarkup(rows)
        await query.edit_message_text(
            f"<b>Move <code>{agent_id}</code></b> — select target:",
            parse_mode="HTML",
            reply_markup=markup,
        )
        return

    if action == "target" and len(parts) >= 4:
        agent_id = parts[2]
        target = parts[3]
        markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📋 Move (plain)", callback_data=f"move:exec:{agent_id}:{target}:plain"),
                InlineKeyboardButton("📂 Copy (keep source)", callback_data=f"move:exec:{agent_id}:{target}:keep"),
            ],
            [
                InlineKeyboardButton("🔄 Sync memories back", callback_data=f"move:exec:{agent_id}:{target}:sync"),
                InlineKeyboardButton("🔍 Dry run preview", callback_data=f"move:exec:{agent_id}:{target}:dry"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="move:cancel")],
        ])
        await query.edit_message_text(
            f"<b>Move <code>{agent_id}</code> → {target}</b>\n\nChoose mode:",
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
    alive = runtime._remote_process is not None and runtime._remote_process.returncode is None

    if arg == "status" or not arg:
        health, health_url = await runtime._fetch_remote_json("/health")
        status, _status_url = await runtime._fetch_remote_json("/protocol/status")
        if not health:
            if alive:
                await runtime._reply_text(
                    update,
                    "🟡 Hashi Remote process is running, but the API did not respond.\n"
                    f"PID: {runtime._remote_process.pid}\n"
                    f"Expected port: {cfg['port']}  ·  TLS: {'on' if cfg['use_tls'] else 'off'}",
                )
            else:
                await runtime._reply_text(update, "⚪ Hashi Remote is not running. Use /remote on to start.")
            return
        instance = health.get("instance") or {}
        peers = list((health.get("peers") or []))
        lines = [
            "🟢 <b>Hashi Remote Status</b>",
            f"Instance: <code>{instance.get('instance_id') or runtime.global_config.project_root.name.upper()}</code>",
            f"API: <code>{health_url}</code>",
            f"Port: <code>{cfg['port']}</code>  ·  TLS: <code>{'on' if cfg['use_tls'] else 'off'}</code>",
            f"Discovery: <code>{cfg['backend']}</code>",
            f"Process: <code>{'running' if alive else 'external/unknown'}</code>" + (f" (PID {runtime._remote_process.pid})" if alive else ""),
            f"Peers: <code>{len(peers)}</code>",
        ]
        if status:
            inflight = int(status.get("inflight_count") or 0)
            lines.append(f"Inflight: <code>{inflight}</code>")
            lines.append(
                "Auth: <code>%s</code>  ·  shared token: <code>%s</code>  ·  lan mode: <code>%s</code>"
                % (
                    html.escape(str(status.get("protocol_auth_mode") or health.get("protocol_auth_mode") or "unknown")),
                    "yes" if (status.get("shared_token_configured") or health.get("shared_token_configured")) else "no",
                    "on" if (status.get("lan_mode") if "lan_mode" in status else health.get("lan_mode")) else "off",
                )
            )
            if not (status.get("shared_token_configured") or health.get("shared_token_configured")):
                lines.append("Mode: <code>discovery-only</code> — trusted protocol messaging is unavailable.")
        await runtime._reply_text(update, "\n".join(lines), parse_mode="HTML")
        return

    if arg == "list":
        data, _url = await runtime._fetch_remote_json("/peers")
        peers = list((data or {}).get("peers") or [])
        if not peers:
            if data and data.get("trusted_view") is False:
                await runtime._reply_text(update, "⚪ Peer detail is not available from this view. Use local loopback or trusted auth.")
            else:
                await runtime._reply_text(update, "⚪ No remote peers are currently visible.")
            return
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
        lines = [
            "📡 <b>Remote Instances</b>",
            f"online: <code>{counts['online']}</code>  ·  attention: <code>{counts['attention']}</code>  ·  offline: <code>{counts['offline']}</code>",
            f"refreshed: <code>{datetime.now().strftime('%H:%M:%S')}</code>",
            "",
        ]
        for idx, peer in enumerate(peers):
            lines.extend(runtime._render_remote_peer_block(peer))
            if idx != len(peers) - 1:
                lines.append("")
        await runtime._reply_text(update, "\n".join(lines), parse_mode="HTML")
        return

    if arg == "off":
        if runtime._remote_process is None or runtime._remote_process.returncode is not None:
            await runtime._reply_text(update, "⚪ Hashi Remote is not running.")
            return
        runtime._remote_process.terminate()
        try:
            await asyncio.wait_for(runtime._remote_process.wait(), timeout=5)
        except asyncio.TimeoutError:
            runtime._remote_process.kill()
        runtime._remote_process = None
        await runtime._reply_text(update, "🔴 Hashi Remote stopped.")
        return

    if arg == "on":
        if alive:
            await runtime._reply_text(update, "🟢 Already running (PID %d)." % runtime._remote_process.pid)
            return

        root = cfg["root"]
        venv_python = root / ".venv" / "bin" / "python3"
        if not venv_python.exists():
            venv_python = root / ".venv" / "Scripts" / "python.exe"
        if not venv_python.exists():
            await runtime._reply_text(
                update,
                f"🔴 Hashi Remote could not start.\nMissing interpreter: <code>{html.escape(str(venv_python))}</code>",
                parse_mode="HTML",
            )
            return

        cmd = [str(venv_python), "-m", "remote"]
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
            f"🟢 Hashi Remote started (PID {runtime._remote_process.pid})\n"
            f"   Port {cfg['port']} · TLS {'on' if cfg['use_tls'] else 'off'} · discovery {cfg['backend']}\n"
            f"   API <code>{html.escape(detail)}</code>\n"
            "   Use /remote off to stop.",
            parse_mode="HTML",
        )
        return

    await runtime._reply_text(update, "Usage: /remote [on|off|status|list]")


async def cmd_oll(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    from browser_gateway.service_control import start as start_oll_service, status as oll_status, stop as stop_oll_service

    arg = (context.args[0].lower() if context.args else "").strip()
    root = runtime.global_config.project_root

    if arg == "on":
        state = start_oll_service(root)
        await runtime._reply_text(
            update,
            "🟢 OLL Browser Gateway started.\n"
            f"PID: {state.get('pid') or 'unknown'}\n"
            f"Base URL: {state.get('base_url')}\n"
            f"Log: {state.get('log_file')}",
        )
        return

    if arg == "off":
        was_running = oll_status(root)
        state = stop_oll_service(root)
        if was_running.get("running"):
            await runtime._reply_text(update, "🔴 OLL Browser Gateway stopped.")
        else:
            await runtime._reply_text(update, "⚪ OLL Browser Gateway is not running.")
        return

    if arg == "status" or not arg:
        state = oll_status(root)
        if state.get("running"):
            await runtime._reply_text(
                update,
                "🟢 OLL Browser Gateway is running.\n"
                f"PID: {state.get('pid')}\n"
                f"Base URL: {state.get('base_url')}\n"
                f"Log: {state.get('log_file')}\n"
                f"State DB: {state.get('state_db')}",
            )
        else:
            await runtime._reply_text(
                update,
                "⚪ OLL Browser Gateway is not running.\n"
                f"Base URL: {state.get('base_url')}\n"
                "Use /oll on to start.",
            )
        return

    await runtime._reply_text(update, "Usage: /oll [on|off|status]")
