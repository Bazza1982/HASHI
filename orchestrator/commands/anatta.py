from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator.command_registry import RuntimeCallback, RuntimeCommand
from orchestrator.command_ui import card_title, refresh_label, selected_label
from tools.anatta_diagnostics import build_report


ANATTA_OBSERVER_FACTORY = "orchestrator.anatta.post_turn_observer:build_post_turn_observer"
ANATTA_MODES = {"off", "shadow", "on"}
USAGE = (
    "Usage: /anatta [status|full|off|shadow|on]\n"
    "status/full inspect Anatta. off/shadow/on update this workspace's Anatta mode."
)


def _is_authorized(runtime: Any, update: Any) -> bool:
    checker = getattr(runtime, "_is_authorized_user", None)
    user_id = getattr(getattr(update, "effective_user", None), "id", None)
    if callable(checker):
        return bool(checker(user_id))
    authorized_id = getattr(getattr(runtime, "global_config", None), "authorized_id", None)
    return authorized_id is None or user_id == authorized_id


async def anatta_command(runtime: Any, update: Any, context: Any) -> None:
    args = [str(arg).strip().lower() for arg in (getattr(context, "args", []) or []) if str(arg).strip()]
    if args and args[0] in {"help", "-h", "--help"}:
        await _send(runtime, update, USAGE)
        return
    if args and args[0] not in {"status", "full", *ANATTA_MODES}:
        await _send(runtime, update, USAGE)
        return

    workspace = Path(getattr(runtime, "workspace_dir"))
    if args and args[0] in ANATTA_MODES:
        mode = args[0]
        notice = _apply_mode(runtime, workspace, mode)
        report = build_report(workspace, full=False)
        await _send(
            runtime,
            update,
            _status_text(workspace, report, notice=notice),
            reply_markup=_keyboard(workspace),
        )
        return

    full = bool(args and args[0] == "full")
    report = build_report(workspace, full=full)
    await _send(
        runtime,
        update,
        _status_text(workspace, report, full=full),
        reply_markup=_keyboard(workspace),
    )


def _current_mode(workspace: Path) -> str:
    return str(_load_json_object(workspace / "anatta_config.json").get("mode") or "off")


def _status_text(
    workspace: Path,
    report: str,
    *,
    notice: str | None = None,
    full: bool = False,
) -> str:
    lines = [
        card_title("🪷", "Anatta diagnostics"),
        "",
        f"<b>Current</b> · <code>{html.escape(_current_mode(workspace).upper())}</code>",
        f"<b>View</b> · <code>{'FULL' if full else 'SUMMARY'}</code>",
        "<b>Changes</b> · mode changes persist in this workspace",
    ]
    if notice:
        lines.extend(["", f"✅ {html.escape(notice)}"])
    lines.extend(["", f"<pre>{html.escape(report)}</pre>"])
    return "\n".join(lines)


def _keyboard(workspace: Path) -> InlineKeyboardMarkup:
    current = _current_mode(workspace)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(selected_label("Off", current == "off"), callback_data="anatta:off"),
                InlineKeyboardButton(selected_label("Shadow", current == "shadow"), callback_data="anatta:shadow"),
                InlineKeyboardButton(selected_label("On", current == "on"), callback_data="anatta:on"),
            ],
            [
                InlineKeyboardButton("Full report", callback_data="anatta:full"),
                InlineKeyboardButton(refresh_label(), callback_data="anatta:status"),
            ],
        ]
    )


def _apply_mode(runtime: Any, workspace: Path, mode: str) -> str:
    changed = _set_anatta_mode(workspace, mode)
    reloaded = _reload_observers(runtime)
    observer_line = "observer ensured" if changed["observer_ensured"] else "observer unchanged"
    reload_line = "observers reloaded" if reloaded else "observer reload unavailable"
    return f"Mode set to {mode}: {observer_line}; {reload_line}."


async def anatta_callback(runtime: Any, update: Any, context: Any) -> None:
    query = update.callback_query
    if not _is_authorized(runtime, update):
        await query.answer()
        return
    action = (query.data or "").split(":", 1)[1] if ":" in (query.data or "") else "status"
    workspace = Path(getattr(runtime, "workspace_dir"))
    notice = None
    if action in ANATTA_MODES:
        notice = _apply_mode(runtime, workspace, action)
    full = action == "full"
    report = build_report(workspace, full=full)
    await query.edit_message_text(
        _status_text(workspace, report, notice=notice, full=full),
        parse_mode="HTML",
        reply_markup=_keyboard(workspace),
    )
    await query.answer()


def _set_anatta_mode(workspace: Path, mode: str) -> dict[str, bool]:
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = workspace / "anatta_config.json"
    config = _load_json_object(config_path)
    config["mode"] = mode
    _write_json_object(config_path, config)

    observer_ensured = False
    if mode in {"shadow", "on"}:
        observer_ensured = _ensure_anatta_observer(workspace / "post_turn_observers.json")
    return {"observer_ensured": observer_ensured}


def _ensure_anatta_observer(path: Path) -> bool:
    config = _load_json_object(path)
    raw_observers = config.get("observers", [])
    observers = raw_observers if isinstance(raw_observers, list) else []
    changed = raw_observers is not observers

    found = False
    normalized: list[Any] = []
    for item in observers:
        if isinstance(item, str):
            if item == ANATTA_OBSERVER_FACTORY:
                found = True
            normalized.append(item)
            continue
        if isinstance(item, dict):
            copied = dict(item)
            if str(copied.get("factory", "")).strip() == ANATTA_OBSERVER_FACTORY:
                found = True
                if copied.get("enabled") is False:
                    copied["enabled"] = True
                    changed = True
            normalized.append(copied)
    if not found:
        normalized.append({"factory": ANATTA_OBSERVER_FACTORY, "enabled": True})
        changed = True

    config["observers"] = normalized
    if changed or not path.exists():
        _write_json_object(path, config)
    return changed or not path.exists()


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_json_object(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _reload_observers(runtime: Any) -> bool:
    reload = getattr(runtime, "reload_post_turn_observers", None)
    if not callable(reload):
        return False
    reload()
    return True


async def _send(runtime: Any, update: Any, text: str, *, reply_markup=None) -> None:
    if hasattr(runtime, "_reply_text"):
        await runtime._reply_text(update, text, parse_mode="HTML", reply_markup=reply_markup)
        return
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is not None and hasattr(runtime, "send_long_message"):
        await runtime.send_long_message(
            chat_id,
            text,
            request_id="anatta-command",
            purpose="command",
        )
        return
    message = getattr(update, "message", None)
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)


COMMANDS = [
    RuntimeCommand(
        name="anatta",
        description="Read-only Anatta diagnostics",
        callback=anatta_command,
    )
]

CALLBACKS = [RuntimeCallback(pattern=r"^anatta:", callback=anatta_callback)]
