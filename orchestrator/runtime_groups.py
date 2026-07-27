from __future__ import annotations

import html
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator.command_ui import BACK_LABEL, card_title, confirm_card


def group_detail_view(directory: Any, group_name: str) -> tuple[str, InlineKeyboardMarkup]:
    groups = directory.list_groups()
    group = groups.get(group_name, {})
    description = group.get("description", "")
    members = group.get("members", [])
    is_dynamic = members == "@active"

    if is_dynamic:
        resolved = directory.resolve_group(group_name)
        member_display = (
            "🔄 <i>Dynamic — all active agents</i>\n  " + ", ".join(resolved)
            if resolved
            else "🔄 <i>Dynamic — (none running)</i>"
        )
    elif members:
        rows = []
        for member in members:
            row = directory.get_agent_row(member)
            emoji = row.get("emoji", "🤖") if row else "🤖"
            display = row.get("display_name", member) if row else member
            rows.append(f"{emoji} {display}")
        member_display = "  " + "  ·  ".join(rows)
    else:
        member_display = "  <i>(empty)</i>"

    text = (
        f"{card_title('📦', 'Agent group')}\n\n"
        f"<b>Current</b> · <code>{html.escape(group_name)}</code>\n"
        f"<b>Description</b> · {html.escape(str(description or 'None'))}\n\n"
        f"<b>MEMBERS</b> · {len(directory.resolve_group(group_name))}\n"
        f"{member_display}\n"
    )
    if is_dynamic:
        buttons = [[InlineKeyboardButton(BACK_LABEL, callback_data="group:back")]]
    else:
        buttons = [
            [
                InlineKeyboardButton("＋ Add", callback_data=f"group:add:{group_name}"),
                InlineKeyboardButton("－ Remove", callback_data=f"group:remove:{group_name}"),
                InlineKeyboardButton("✏️ Rename", callback_data=f"group:rename:{group_name}"),
            ],
            [
                InlineKeyboardButton("🗑 Delete", callback_data=f"group:delete:{group_name}"),
                InlineKeyboardButton(BACK_LABEL, callback_data="group:back"),
            ],
            [
                InlineKeyboardButton("Start all", callback_data=f"group:start:{group_name}"),
                InlineKeyboardButton("Stop all", callback_data=f"group:stop:{group_name}"),
                InlineKeyboardButton("Reboot all", callback_data=f"group:reboot:{group_name}"),
            ],
            [InlineKeyboardButton("💬 Broadcast", callback_data=f"group:broadcast:{group_name}")],
        ]
    return text, InlineKeyboardMarkup(buttons)


def group_list_view(directory: Any) -> tuple[str, InlineKeyboardMarkup]:
    groups = directory.list_groups()
    if groups:
        lines = [
            card_title("📦", "Agent groups"),
            "",
            f"<b>Current</b> · <code>{len(groups)}</code> groups",
            "",
        ]
        for name, group in groups.items():
            members = group.get("members", [])
            is_dynamic = members == "@active"
            count = len(directory.resolve_group(name)) if is_dynamic else len(members)
            description = group.get("description", "")
            tag = " 🔄" if is_dynamic else ""
            lines.append(f"• <b>{name}</b>{tag}  ({count} agents) — {description}")
    else:
        lines = [
            card_title("📦", "Agent groups"),
            "",
            "<b>Current</b> · <code>0</code> groups",
            "",
            "No groups are defined yet.",
        ]
    buttons = [
        [InlineKeyboardButton(f"📦 {name}", callback_data=f"group:view:{name}")]
        for name in groups
    ]
    buttons.append([InlineKeyboardButton("New group", callback_data="group:new")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def cmd_group(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    directory = getattr(runtime, "agent_directory", None)
    if directory is None:
        await runtime._reply_text(update, "❌ Agent directory unavailable.")
        return
    args = [arg.strip() for arg in (context.args or []) if arg.strip()]

    if args and args[0].lower() == "new":
        if len(args) < 2:
            await runtime._reply_text(
                update,
                "Usage: <code>/group new &lt;name&gt;</code>",
                parse_mode="HTML",
            )
            return
        name = args[1].lower()
        description = " ".join(args[2:]) if len(args) > 2 else ""
        ok, message = directory.create_group(name, description)
        if ok:
            text, markup = group_detail_view(directory, name)
            await runtime._reply_text(
                update,
                f"✅ {message}\n\n{text}",
                parse_mode="HTML",
                reply_markup=markup,
            )
        else:
            await runtime._reply_text(update, f"❌ {message}")
        return

    if args and args[0].lower() == "del":
        if len(args) < 2:
            await runtime._reply_text(
                update,
                "Usage: <code>/group del &lt;name&gt;</code>",
                parse_mode="HTML",
            )
            return
        name = args[1].lower()
        buttons = [
            [InlineKeyboardButton(f"Delete {name}", callback_data=f"group:delete_confirm:{name}")],
            [InlineKeyboardButton("← Keep group", callback_data="group:back")],
        ]
        await runtime._reply_text(
            update,
            confirm_card(
                "⚠️",
                "Delete group",
                target=f"<code>{html.escape(name)}</code>",
                consequence="This deletes only the group definition. The agents themselves are unchanged.",
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if args:
        name = args[0].lower()
        if not directory.group_exists(name):
            await runtime._reply_text(update, f"❌ Group '{name}' not found.")
            return
        text, markup = group_detail_view(directory, name)
        await runtime._reply_text(update, text, parse_mode="HTML", reply_markup=markup)
        return

    text, markup = group_list_view(directory)
    await runtime._reply_text(update, text, parse_mode="HTML", reply_markup=markup)


async def callback_group(runtime: Any, update: Any, context: Any) -> None:
    query = update.callback_query
    if not runtime._is_authorized_user(query.from_user.id):
        return
    directory = getattr(runtime, "agent_directory", None)
    if directory is None:
        await query.answer("Agent directory unavailable", show_alert=True)
        return

    parts = (query.data or "").split(":", 3)
    action = parts[1] if len(parts) > 1 else ""
    group_name = parts[2] if len(parts) > 2 else ""
    extra = parts[3] if len(parts) > 3 else ""
    await query.answer()

    if action == "back":
        text, markup = group_list_view(directory)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        return
    if action == "view":
        text, markup = group_detail_view(directory, group_name)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        return
    if action == "new":
        await query.edit_message_text(
            "To create a new group, send:\n<code>/group new &lt;name&gt; [description]</code>",
            parse_mode="HTML",
        )
        return
    if action == "delete":
        buttons = [
            [
                InlineKeyboardButton(
                    f"Delete {group_name}",
                    callback_data=f"group:delete_confirm:{group_name}",
                )
            ],
            [InlineKeyboardButton("← Keep group", callback_data=f"group:view:{group_name}")],
        ]
        await query.edit_message_text(
            confirm_card(
                "⚠️",
                "Delete group",
                target=f"<code>{html.escape(group_name)}</code>",
                consequence="This deletes only the group definition. The agents themselves are unchanged.",
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return
    if action == "delete_confirm":
        ok, message = directory.delete_group(group_name)
        if ok:
            text, markup = group_list_view(directory)
            await query.edit_message_text(
                f"🗑 {message}\n\n{text}",
                parse_mode="HTML",
                reply_markup=markup,
            )
        else:
            await query.edit_message_text(f"❌ {message}")
        return
    if action == "add":
        current = directory.list_groups().get(group_name, {}).get("members", [])
        available = [name for name in directory._agent_rows if name not in current]
        if not available:
            await query.edit_message_text(
                f"All active agents are already in <b>{group_name}</b>.",
                parse_mode="HTML",
            )
            return
        buttons = []
        for agent in available:
            row = directory.get_agent_row(agent)
            emoji = row.get("emoji", "🤖") if row else "🤖"
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"{emoji} {agent}",
                        callback_data=f"group:add_confirm:{group_name}:{agent}",
                    )
                ]
            )
        buttons.append([InlineKeyboardButton(BACK_LABEL, callback_data=f"group:view:{group_name}")])
        await query.edit_message_text(
            f"➕ Add to <b>{group_name}</b>\nSelect agents to add:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return
    if action == "add_confirm":
        ok, message = directory.group_add_member(group_name, extra)
        text, markup = group_detail_view(directory, group_name)
        await query.edit_message_text(
            ("✅ " if ok else "❌ ") + message + "\n\n" + text,
            parse_mode="HTML",
            reply_markup=markup,
        )
        return
    if action == "remove":
        current = directory.list_groups().get(group_name, {}).get("members", [])
        if not current:
            await query.edit_message_text(
                f"Group <b>{group_name}</b> is empty.",
                parse_mode="HTML",
            )
            return
        buttons = []
        for agent in current:
            row = directory.get_agent_row(agent)
            emoji = row.get("emoji", "🤖") if row else "🤖"
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"{emoji} {agent}",
                        callback_data=f"group:remove_confirm:{group_name}:{agent}",
                    )
                ]
            )
        buttons.append([InlineKeyboardButton(BACK_LABEL, callback_data=f"group:view:{group_name}")])
        await query.edit_message_text(
            f"➖ Remove from <b>{group_name}</b>\nSelect agents to remove:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return
    if action == "remove_confirm":
        ok, message = directory.group_remove_member(group_name, extra)
        text, markup = group_detail_view(directory, group_name)
        await query.edit_message_text(
            ("✅ " if ok else "❌ ") + message + "\n\n" + text,
            parse_mode="HTML",
            reply_markup=markup,
        )
        return
    if action == "rename":
        await query.edit_message_text(
            f"To rename group <b>{group_name}</b>, send:\n"
            f"<code>/group rename {group_name} &lt;new_name&gt;</code>",
            parse_mode="HTML",
        )
        return
    if action in {"start", "stop", "reboot"}:
        orchestrator = getattr(runtime, "orchestrator", None)
        members = directory.resolve_group(group_name, exclude_self=runtime.name)
        if not members:
            await query.edit_message_text(
                f"Group <b>{group_name}</b> has no members to act on.",
                parse_mode="HTML",
            )
            return
        action_label = {
            "start": "▶ Starting",
            "stop": "⏹ Stopping",
            "reboot": "🔄 Rebooting",
        }[action]
        lines = [f"<b>{action_label} group {group_name}</b> ({len(members)} agents)\n"]
        if action == "reboot" and orchestrator:
            all_names = orchestrator.configured_agent_names()
            for name in members:
                if name in all_names:
                    orchestrator.request_restart(
                        mode="number",
                        agent_name=runtime.name,
                        agent_number=all_names.index(name) + 1,
                    )
                    lines.append(f"  🔄 {name} — reboot queued")
                else:
                    lines.append(f"  ⚠️ {name} — not found")
        elif action == "start" and orchestrator:
            for name in members:
                ok, message = await orchestrator.start_agent(name)
                lines.append(f"  {'✅' if ok else '❌'} {name} — {message}")
        elif action == "stop" and orchestrator:
            for name in members:
                member_runtime = directory.get_runtime(name)
                manager = getattr(member_runtime, "backend_manager", None)
                backend = getattr(manager, "current_backend", None)
                if backend is not None:
                    await backend.shutdown()
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
            await query.edit_message_text(
                f"Group <b>{group_name}</b> has no members to broadcast to.",
                parse_mode="HTML",
            )
            return
        await query.edit_message_text(
            f"📢 Broadcast to group <b>{group_name}</b> ({len(members)} agents)\n\n"
            f"Use: <code>/hchat @{group_name} &lt;your intent&gt;</code>",
            parse_mode="HTML",
        )
