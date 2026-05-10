from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator.command_registry import RuntimeCallback


async def cmd_move(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    instances = runtime._load_instances()
    if not instances:
        await runtime._reply_text(update, "⚠️ No instances.json found. Create one at the project root.")
        return

    args = context.args or []

    if args and args[0].lower() == "list":
        lines = ["<b>Known HASHI Instances:</b>"]
        for name, inst in instances.items():
            root = inst.get("root") or "(auto)"
            lines.append(f"  • <code>{name}</code> — {inst.get('display_name', '')}  <i>{root}</i>")
        await runtime._reply_text(update, "\n".join(lines), parse_mode="HTML")
        return

    if len(args) >= 2:
        agent_id = args[0]
        target = args[1]
        keep_source = "--keep-source" in args
        sync = "--sync" in args
        dry_run = "--dry-run" in args
        await runtime._do_move(
            update,
            agent_id,
            target,
            instances,
            keep_source=keep_source,
            sync=sync,
            dry_run=dry_run,
        )
        return

    if len(args) == 1:
        await runtime._move_show_target_picker(update, args[0], instances)
        return

    await runtime._move_show_agent_picker(update, instances)


async def callback_move(runtime: Any, update: Any, context: Any) -> None:
    query = update.callback_query
    if not runtime._is_authorized_user(query.from_user.id):
        await query.answer()
        return
    await query.answer()

    data = query.data or ""
    parts = data.split(":")

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

    if action == "exec" and len(parts) >= 5:
        agent_id = parts[2]
        target = parts[3]
        mode = parts[4]

        keep = mode == "keep"
        sync = mode == "sync"
        dry = mode == "dry"
        instances = runtime._load_instances()
        await runtime._do_move(update, agent_id, target, instances, keep_source=keep, sync=sync, dry_run=dry)


CALLBACKS = [
    RuntimeCallback(pattern=r"^move:", callback=callback_move),
]
