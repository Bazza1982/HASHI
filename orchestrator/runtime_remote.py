from __future__ import annotations

import json
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
