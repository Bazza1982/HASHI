from __future__ import annotations

import json
import asyncio
import subprocess
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
