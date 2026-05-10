from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from telegram import Update


async def cmd_hchat(runtime, update: Update, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    args = [a.strip() for a in (context.args or []) if a.strip()]
    if len(args) < 2:
        await runtime._reply_text(
            update,
            "<b>💬 Hchat — Ask this agent to compose &amp; send a message to another agent</b>\n\n"
            "Usage: <code>/hchat &lt;agent&gt; &lt;intent&gt;</code> — local instance only\n"
            "       <code>/hchat &lt;agent&gt;@&lt;INSTANCE&gt; &lt;intent&gt;</code> — cross-instance via HASHI1 exchange\n"
            "       <code>/hchat all &lt;intent&gt;</code> — broadcast to all local active agents (excludes temp)\n"
            "       <code>/hchat @&lt;group&gt; &lt;intent&gt;</code> — broadcast to a local group (use /group to manage)\n\n"
            "Example: <code>/hchat lily give her an update on what we've been doing</code>\n"
            "Example: <code>/hchat rika@HASHI2 ask her for the latest test result</code>\n"
            "Example: <code>/hchat hashiko@MSI tell her the route is fixed</code>\n"
            "Example: <code>/hchat arale 告诉她新功能已完成</code>\n"
            "Example: <code>/hchat all 告诉大家新功能上线了</code>\n"
            "Example: <code>/hchat @staff 告诉核心团队系统已重启</code>\n\n"
            "<i>Note: no @ means local only. Cross-instance targets must be written as agent@INSTANCE.</i>",
            parse_mode="HTML",
        )
        return
    target_name = args[0].lower()
    intent = " ".join(args[1:])

    broadcast_targets: list[str] | None = None
    broadcast_label: str = ""

    if target_name == "all":
        import json as _json

        try:
            _cfg = _json.loads(runtime.global_config.config_path.read_text(encoding="utf-8-sig"))
            broadcast_targets = [
                a["name"] for a in _cfg.get("agents", [])
                if a.get("is_active", True)
                and a["name"].lower() != "temp"
                and a["name"].lower() != runtime.name.lower()
            ]
        except Exception:
            broadcast_targets = []
        broadcast_label = "ALL active agents"

    elif target_name.startswith("@"):
        group_name = target_name[1:]
        directory = getattr(runtime, "agent_directory", None) or getattr(getattr(runtime, "orchestrator", None), "agent_directory", None)
        if directory is None:
            await runtime._reply_text(update, "❌ Agent directory unavailable for group resolution.")
            return
        if not directory.group_exists(group_name):
            await runtime._reply_text(update, f"❌ Group '{group_name}' not found. Use /group to list groups.")
            return
        broadcast_targets = directory.resolve_group(group_name, exclude_self=runtime.name)
        broadcast_label = f"group @{group_name}"

    if broadcast_targets is not None:
        if not broadcast_targets:
            await runtime._reply_text(update, f"❌ No agents found in {broadcast_label}.")
            return
        agent_list = ", ".join(broadcast_targets)
        send_cmds = "\n".join(
            f'   {sys.executable} {Path(__file__).resolve().parent.parent / "tools" / "hchat_send.py"} --to {a} --from {runtime.name} --text "<your composed message>"'
            for a in broadcast_targets
        )
        self_prompt = (
            f"[HCHAT BROADCAST] The user wants you to send a Hchat message to {broadcast_label}.\n\n"
            f"Target agents: {agent_list}\n"
            f"EXCLUDED: temp (always excluded from broadcasts), {runtime.name} (yourself)\n\n"
            f"Intent: {intent}\n\n"
            f"Instructions:\n"
            f"1. Think about what from our current conversation context is relevant to this intent.\n"
            f"2. Compose a complete, meaningful message FROM you ({runtime.name}). "
            f"Write it as yourself — the same message goes to all agents. Be concise.\n"
            f"3. Send the message to EACH agent by running these bash commands:\n"
            f"{send_cmds}\n"
            f"4. Report back to the user: what you sent, to whom, and how many succeeded.\n\n"
            f"Do NOT relay the user's words literally. Compose the message yourself.\n\n"
            f"IMPORTANT: When you later receive messages starting with '[hchat reply from ...]', "
            f"just report the reply content to the user. Do NOT send another hchat message back."
        )
        await runtime._reply_text(
            update,
            f"📢 Broadcasting Hchat to <b>{len(broadcast_targets)}</b> agents ({broadcast_label})...",
            parse_mode="HTML",
        )
    else:
        self_prompt = (
            f"[HCHAT TASK] The user wants you to send a Hchat message to agent \"{target_name}\".\n\n"
            f"Intent: {intent}\n\n"
            f"Instructions:\n"
            f"1. Think about what from our current conversation context is relevant to this intent.\n"
            f"2. Compose a complete, meaningful message FROM you ({runtime.name}) TO {target_name}. "
            f"Write it as yourself — introduce yourself if appropriate, include relevant context, be concise.\n"
            f"3. Send the message by running this bash command:\n"
            f"   {sys.executable} {Path(__file__).resolve().parent.parent / 'tools' / 'hchat_send.py'} --to {target_name} --from {runtime.name} --text \"<your composed message>\"\n"
            f"4. Report back to the user: what you sent and a brief summary of why.\n\n"
            f"Do NOT relay the user's words literally. Compose the message yourself.\n\n"
            f"IMPORTANT: When you later receive a message starting with '[hchat reply from ...]', "
            f"just report the reply content to the user. Do NOT send another hchat message back — "
            f"the conversation ends there."
        )
        await runtime._reply_text(update, f"💬 Composing Hchat message to <b>{target_name}</b>...", parse_mode="HTML")

    await runtime.enqueue_api_text(
        self_prompt,
        source="bridge:hchat",
        deliver_to_telegram=True,
    )
