from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

from orchestrator.command_registry import RuntimeCallback


async def cmd_group(runtime, update: Update, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    directory = getattr(runtime, "agent_directory", None)
    if directory is None:
        await runtime._reply_text(update, "❌ Agent directory unavailable.")
        return

    args = [a.strip() for a in (context.args or []) if a.strip()]

    if args and args[0].lower() == "new":
        if len(args) < 2:
            await runtime._reply_text(update, "Usage: <code>/group new &lt;name&gt;</code>", parse_mode="HTML")
            return
        name = args[1].lower()
        desc = " ".join(args[2:]) if len(args) > 2 else ""
        ok, msg = directory.create_group(name, desc)
        if ok:
            text, markup = runtime._group_detail_view(directory, name)
            await runtime._reply_text(update, f"✅ {msg}\n\n" + text, parse_mode="HTML", reply_markup=markup)
        else:
            await runtime._reply_text(update, f"❌ {msg}")
        return

    if args and args[0].lower() == "del":
        if len(args) < 2:
            await runtime._reply_text(update, "Usage: <code>/group del &lt;name&gt;</code>", parse_mode="HTML")
            return
        name = args[1].lower()
        rows = [[
            InlineKeyboardButton("✅ Confirm Delete", callback_data=f"group:delete_confirm:{name}"),
            InlineKeyboardButton("✕ Cancel", callback_data="group:back"),
        ]]
        await runtime._reply_text(
            update,
            f"⚠️ Delete group <b>{name}</b>?\nThis will NOT affect the agents themselves.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if args:
        name = args[0].lower()
        if not directory.group_exists(name):
            await runtime._reply_text(update, f"❌ Group '{name}' not found.")
            return
        text, markup = runtime._group_detail_view(directory, name)
        await runtime._reply_text(update, text, parse_mode="HTML", reply_markup=markup)
        return

    text, markup = runtime._group_list_view(directory)
    await runtime._reply_text(update, text, parse_mode="HTML", reply_markup=markup)


async def callback_group(runtime, update: Update, context: Any) -> None:
    query = update.callback_query
    if not runtime._is_authorized_user(query.from_user.id):
        return
    directory = getattr(runtime, "agent_directory", None)
    if directory is None:
        await query.answer("Agent directory unavailable", show_alert=True)
        return

    data = query.data or ""
    parts = data.split(":", 3)
    action = parts[1] if len(parts) > 1 else ""
    group_name = parts[2] if len(parts) > 2 else ""
    extra = parts[3] if len(parts) > 3 else ""

    await query.answer()

    if action == "back":
        text, markup = runtime._group_list_view(directory)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        return

    if action == "view":
        text, markup = runtime._group_detail_view(directory, group_name)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        return

    if action == "new":
        await query.edit_message_text(
            "To create a new group, send:\n<code>/group new &lt;name&gt; [description]</code>",
            parse_mode="HTML",
        )
        return

    if action == "delete":
        rows = [[
            InlineKeyboardButton("✅ Confirm Delete", callback_data=f"group:delete_confirm:{group_name}"),
            InlineKeyboardButton("✕ Cancel", callback_data=f"group:view:{group_name}"),
        ]]
        await query.edit_message_text(
            f"⚠️ Delete group <b>{group_name}</b>?\nThis will NOT affect the agents themselves.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if action == "delete_confirm":
        ok, msg = directory.delete_group(group_name)
        if ok:
            text, markup = runtime._group_list_view(directory)
            await query.edit_message_text(f"🗑 {msg}\n\n" + text, parse_mode="HTML", reply_markup=markup)
        else:
            await query.edit_message_text(f"❌ {msg}")
        return

    if action == "add":
        groups = directory.list_groups()
        current = groups.get(group_name, {}).get("members", [])
        all_agents = list(directory._agent_rows.keys())
        available = [name for name in all_agents if name not in current]
        if not available:
            await query.edit_message_text(f"All active agents are already in <b>{group_name}</b>.", parse_mode="HTML")
            return
        rows = []
        for agent in available:
            row = directory.get_agent_row(agent)
            emoji = row.get("emoji", "🤖") if row else "🤖"
            rows.append([InlineKeyboardButton(f"{emoji} {agent}", callback_data=f"group:add_confirm:{group_name}:{agent}")])
        rows.append([InlineKeyboardButton("✕ Cancel", callback_data=f"group:view:{group_name}")])
        await query.edit_message_text(
            f"➕ Add to <b>{group_name}</b>\nSelect agents to add:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if action == "add_confirm":
        agent_name = extra
        ok, msg = directory.group_add_member(group_name, agent_name)
        text, markup = runtime._group_detail_view(directory, group_name)
        prefix = "✅ " if ok else "❌ "
        await query.edit_message_text(prefix + msg + "\n\n" + text, parse_mode="HTML", reply_markup=markup)
        return

    if action == "remove":
        groups = directory.list_groups()
        current = groups.get(group_name, {}).get("members", [])
        if not current:
            await query.edit_message_text(f"Group <b>{group_name}</b> is empty.", parse_mode="HTML")
            return
        rows = []
        for agent in current:
            row = directory.get_agent_row(agent)
            emoji = row.get("emoji", "🤖") if row else "🤖"
            rows.append([InlineKeyboardButton(f"{emoji} {agent}", callback_data=f"group:remove_confirm:{group_name}:{agent}")])
        rows.append([InlineKeyboardButton("✕ Cancel", callback_data=f"group:view:{group_name}")])
        await query.edit_message_text(
            f"➖ Remove from <b>{group_name}</b>\nSelect agents to remove:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    if action == "remove_confirm":
        agent_name = extra
        ok, msg = directory.group_remove_member(group_name, agent_name)
        text, markup = runtime._group_detail_view(directory, group_name)
        prefix = "✅ " if ok else "❌ "
        await query.edit_message_text(prefix + msg + "\n\n" + text, parse_mode="HTML", reply_markup=markup)
        return

    if action == "rename":
        await query.edit_message_text(
            f"To rename group <b>{group_name}</b>, send:\n<code>/group rename {group_name} &lt;new_name&gt;</code>",
            parse_mode="HTML",
        )
        return

    if action in ("start", "stop", "reboot"):
        orchestrator = getattr(runtime, "orchestrator", None)
        members = directory.resolve_group(group_name, exclude_self=runtime.name)
        if not members:
            await query.edit_message_text(f"Group <b>{group_name}</b> has no members to act on.", parse_mode="HTML")
            return

        lines = [f"<b>{'▶ Starting' if action == 'start' else '⏹ Stopping' if action == 'stop' else '🔄 Rebooting'} group {group_name}</b> ({len(members)} agents)\n"]

        if action == "reboot" and orchestrator:
            for name in members:
                all_names = orchestrator.configured_agent_names()
                if name in all_names:
                    num = all_names.index(name) + 1
                    orchestrator.request_restart(mode="number", agent_name=runtime.name, agent_number=num)
                    lines.append(f"  🔄 {name} — reboot queued")
                else:
                    lines.append(f"  ⚠️ {name} — not found")
        elif action == "start" and orchestrator:
            for name in members:
                ok, msg = await orchestrator.start_agent(name)
                lines.append(f"  {'✅' if ok else '❌'} {name} — {msg}")
        elif action == "stop" and orchestrator:
            for name in members:
                child_runtime = directory.get_runtime(name)
                if child_runtime and hasattr(child_runtime, "backend_manager") and child_runtime.backend_manager.current_backend:
                    await child_runtime.backend_manager.current_backend.shutdown()
                    lines.append(f"  ⏹ {name} — stopped")
                else:
                    lines.append(f"  ⚠️ {name} — not running or unavailable")
        else:
            lines.append("⚠️ Orchestrator unavailable.")

        await query.edit_message_text("\n".join(lines), parse_mode="HTML")
        return

    if action == "broadcast":
        members = directory.resolve_group(group_name, exclude_self=runtime.name)
        if not members:
            await query.edit_message_text(f"Group <b>{group_name}</b> has no members to broadcast to.", parse_mode="HTML")
            return
        await query.edit_message_text(
            f"📢 Broadcast to group <b>{group_name}</b> ({len(members)} agents)\n\n"
            f"Use: <code>/hchat @{group_name} &lt;your intent&gt;</code>",
            parse_mode="HTML",
        )
        return


CALLBACKS = [
    RuntimeCallback(pattern=r"^group:", callback=callback_group),
]
