from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

CALLBACK_DATA_LIMIT = 64
CALLBACK_TOKEN_TTL_SECONDS = 30 * 60
MAX_CALLBACK_TOKENS = 256


def _runtime_logger(runtime):
    return getattr(runtime, "logger", logging.getLogger(__name__))


def _callback_token_buckets(runtime):
    buckets = getattr(runtime, "_ui_callback_tokens", None)
    if buckets is None:
        buckets = {}
        runtime._ui_callback_tokens = buckets
    return buckets


def _callback_token_store(runtime, namespace: str) -> dict[str, dict]:
    return _callback_token_buckets(runtime).setdefault(namespace, {})


def _prune_callback_token_store(store: dict[str, dict], *, now: float, ttl_seconds: int) -> int:
    expired = [
        token
        for token, entry in list(store.items())
        if float(entry.get("expires_at", 0)) <= now
    ]
    for token in expired:
        store.pop(token, None)
    return len(expired)


def mint_callback_token(
    runtime,
    namespace: str,
    payload: dict,
    *,
    prefix: str,
    ttl_seconds: int = CALLBACK_TOKEN_TTL_SECONDS,
    max_entries: int = MAX_CALLBACK_TOKENS,
) -> str:
    logger = _runtime_logger(runtime)
    store = _callback_token_store(runtime, namespace)
    now = time.time()
    pruned = _prune_callback_token_store(store, now=now, ttl_seconds=ttl_seconds)
    if pruned:
        logger.info("Pruned %s expired %s callback token(s).", pruned, namespace)
    if len(store) >= max_entries:
        for token, _entry in sorted(store.items(), key=lambda item: float(item[1].get("created_at", 0)))[: len(store) - max_entries + 1]:
            store.pop(token, None)
        logger.info("Pruned %s callback token store to stay within %s entries.", namespace, max_entries)
    for _ in range(16):
        token = f"{prefix}{uuid.uuid4().hex[:6]}"
        if token not in store:
            store[token] = {
                "payload": dict(payload),
                "created_at": now,
                "expires_at": now + ttl_seconds,
            }
            logger.debug("Created %s callback token %s for %s", namespace, token, payload)
            return token
    raise RuntimeError(f"Could not allocate unique callback token for {namespace}")


def resolve_callback_token(runtime, namespace: str, token: str, *, now: float | None = None) -> dict | None:
    logger = _runtime_logger(runtime)
    store = _callback_token_store(runtime, namespace)
    current_time = time.time() if now is None else now
    _prune_callback_token_store(store, now=current_time, ttl_seconds=CALLBACK_TOKEN_TTL_SECONDS)
    entry = store.get(token)
    if not entry:
        logger.warning("Unknown or expired %s callback token: %s", namespace, token)
        return None
    if float(entry.get("expires_at", 0)) <= current_time:
        store.pop(token, None)
        logger.warning("Expired %s callback token: %s", namespace, token)
        return None
    return dict(entry.get("payload") or {})


def _build_jobs_with_buttons(runtime, agent_name: str, skill_manager, filter_agent: str | None = None):
    """Build combined jobs message text and inline keyboard with run/toggle buttons.

    filter_agent: if set, only show jobs whose 'agent' field matches. None means show all.
    Returns (text: str, markup: InlineKeyboardMarkup | None).
    """
    if skill_manager is None or not hasattr(skill_manager, "tasks_path"):
        return "No task scheduler configured.", None
    try:
        if not skill_manager.tasks_path.exists():
            data = {"heartbeats": [], "crons": []}
        else:
            data = json.loads(skill_manager.tasks_path.read_text(encoding="utf-8"))
    except Exception:
        return "Could not read tasks.json.", None

    if filter_agent:
        title = f"<b>📋 Jobs — {filter_agent}</b>"
    else:
        title = "<b>📋 Jobs — all agents</b>"
    lines = [title]
    buttons: list = []

    all_jobs: list[tuple[str, dict]] = []

    for h in data.get("heartbeats", []):
        if filter_agent and h.get("agent") != filter_agent:
            continue
        interval = h.get("interval_seconds", 0)
        if interval >= 3600:
            interval_s = f"every {interval // 3600}h"
        elif interval >= 60:
            interval_s = f"every {interval // 60}m"
        else:
            interval_s = f"every {interval}s"
        status = "✅" if h.get("enabled", False) else "❌"
        owner = h.get("agent", "?")
        lines.append(f"\n{status} ⏱ <code>{h['id']}</code> — {interval_s} [{owner}]")
        note = h.get("note", "")
        if note and note != h["id"]:
            lines.append(f"   {note}")
        all_jobs.append(("heartbeat", h))

    for c in data.get("crons", []):
        if filter_agent and c.get("agent") != filter_agent:
            continue
        enabled = c.get("enabled", False)
        schedule = c.get("schedule", "")
        parts = schedule.split() if schedule else []
        if len(parts) == 5:
            minute, hour, dom, month, dow = parts
            if dom == "*" and month == "*":
                if dow == "*":
                    if hour.startswith("*/"):
                        interval_h = hour[2:]
                        time_s = f"every {interval_h}h"
                    elif minute.startswith("*/"):
                        interval_m = minute[2:]
                        time_s = f"every {interval_m}m"
                    else:
                        try:
                            time_s = f"{int(hour):02d}:{int(minute):02d}"
                        except ValueError:
                            time_s = schedule
                else:
                    time_s = schedule
            else:
                time_s = schedule
        else:
            time_s = schedule or "??:??"
        freq_label = "every " if "every" in time_s else "daily "
        status = "✅" if enabled else "❌"
        owner = c.get("agent", "?")
        lines.append(f"\n{status} 📅 <code>{c['id']}</code> — {freq_label}{time_s} [{owner}]")
        note = c.get("note", "")
        if note and note != c["id"]:
            lines.append(f"   {note}")
        all_jobs.append(("cron", c))

    for kind, job in all_jobs:
        jid = job["id"]
        enabled = job.get("enabled", False)
        toggle_mode = "off" if enabled else "on"
        toggle_label = "ON" if enabled else "OFF"
        icon = "⏱" if kind == "heartbeat" else "📅"
        short_id = jid[:22]
        run_token = mint_callback_token(runtime, "skilljob_action", {"kind": kind, "task_id": jid, "action": "run"}, prefix="j")
        toggle_token = mint_callback_token(
            runtime,
            "skilljob_action",
            {"kind": kind, "task_id": jid, "action": "toggle", "value": toggle_mode},
            prefix="j",
        )
        transfer_token = mint_callback_token(
            runtime,
            "skilljob_action",
            {"kind": kind, "task_id": jid, "action": "transfer"},
            prefix="j",
        )
        delete_token = mint_callback_token(
            runtime,
            "skilljob_action",
            {"kind": kind, "task_id": jid, "action": "delete"},
            prefix="j",
        )
        buttons.append([InlineKeyboardButton(f"{icon} {short_id}", callback_data="noop")])
        buttons.append([
            InlineKeyboardButton("▶ Run", callback_data=f"skilljob:{kind}:key:{run_token}:run"),
            InlineKeyboardButton(toggle_label, callback_data=f"skilljob:{kind}:key:{toggle_token}:toggle"),
            InlineKeyboardButton("📤 Transfer", callback_data=f"skilljob:{kind}:key:{transfer_token}:transfer"),
            InlineKeyboardButton("🗑 Del", callback_data=f"skilljob:{kind}:key:{delete_token}:delete"),
        ])

    if not all_jobs:
        lines.append("\nNo jobs configured.")

    markup = InlineKeyboardMarkup(buttons) if buttons else None
    return "\n".join(lines), markup


def _build_jobs_text(agent_name: str, skill_manager) -> str:
    """Build a formatted jobs listing for a single agent."""
    if skill_manager is None or not hasattr(skill_manager, "tasks_path"):
        return "No task scheduler configured."
    try:
        if not skill_manager.tasks_path.exists():
            data = {"heartbeats": [], "crons": []}
        else:
            data = json.loads(skill_manager.tasks_path.read_text(encoding="utf-8"))
    except Exception:
        return "Could not read tasks.json."

    lines = [f"<b>Jobs for {agent_name}</b>", ""]
    found = False

    hbs = [h for h in data.get("heartbeats", []) if h.get("agent") == agent_name]
    if hbs:
        lines.append("<b>Heartbeats</b>")
        for h in hbs:
            enabled = "✓" if h.get("enabled") else "✗"
            interval = h.get("interval_seconds", 0)
            if interval >= 3600:
                interval_s = f"{interval // 3600}h"
            elif interval >= 60:
                interval_s = f"{interval // 60}m"
            else:
                interval_s = f"{interval}s"
            note = h.get("note", h.get("id", ""))
            action = h.get("action", "enqueue_prompt")
            lines.append(f"  {enabled} <code>{h['id']}</code>  every {interval_s}")
            if action != "enqueue_prompt":
                lines.append(f"      action: {action}")
            if note and note != h["id"]:
                lines.append(f"      {note}")
        lines.append("")
        found = True

    crons = [c for c in data.get("crons", []) if c.get("agent") == agent_name]
    if crons:
        lines.append("<b>Crons</b>")
        for c in crons:
            enabled = "✓" if c.get("enabled") else "✗"
            time_s = c.get("time", "??:??")
            action = c.get("action", "enqueue_prompt")
            note = c.get("note", c.get("id", ""))
            lines.append(f"  {enabled} <code>{c['id']}</code>  at {time_s}")
            if action != "enqueue_prompt":
                lines.append(f"      action: {action}")
            if note and note != c["id"]:
                lines.append(f"      {note}")
        lines.append("")
        found = True

    if not found:
        lines.append("No jobs configured for this agent.")

    return "\n".join(lines)


def build_job_transfer_keyboard(runtime, kind: str, task_id: str) -> InlineKeyboardMarkup:
    """Build inline keyboard for job transfer: same-instance agents + remote instances."""
    buttons = []

    orchestrator = getattr(runtime, "orchestrator", None)
    local_agents = []
    if orchestrator:
        for rt in getattr(orchestrator, "runtimes", []):
            name = getattr(rt, "name", "")
            if name and name != runtime.name:
                local_agents.append(name)

    if local_agents:
        buttons.append([InlineKeyboardButton("── This instance ──", callback_data="noop")])
        row = []
        for agent in sorted(local_agents):
            row.append(
                InlineKeyboardButton(
                    agent,
                    callback_data=runtime._job_transfer_callback(kind, task_id, agent),
                )
            )
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

    try:
        instances_path = runtime.global_config.project_root / "instances.json"
        if instances_path.exists():
            data = json.loads(instances_path.read_text(encoding="utf-8"))
            for inst_id, inst_info in data.get("instances", {}).items():
                if not inst_info.get("active", False):
                    continue
                display = inst_info.get("display_name", inst_id)
                platform = inst_info.get("platform", "")
                if platform == "portable":
                    continue
                if platform == "windows":
                    wsl_root = inst_info.get("wsl_root")
                    agents_path = Path(wsl_root) / "agents.json" if wsl_root else None
                else:
                    root = inst_info.get("root")
                    agents_path = Path(root) / "agents.json" if root else None

                if not agents_path or not agents_path.exists():
                    continue
                try:
                    adata = json.loads(agents_path.read_text(encoding="utf-8-sig"))
                    remote_agents = [a["name"] for a in adata.get("agents", []) if a.get("is_active", True)]
                except Exception:
                    continue

                if not remote_agents:
                    continue

                buttons.append([InlineKeyboardButton(f"── {display} ──", callback_data="noop")])
                row = []
                for agent in sorted(remote_agents):
                    cb = runtime._job_transfer_callback(kind, task_id, agent, instance_id=inst_id)
                    row.append(InlineKeyboardButton(f"{agent}", callback_data=cb))
                    if len(row) == 3:
                        buttons.append(row)
                        row = []
                if row:
                    buttons.append(row)
    except Exception as exc:
        logger = getattr(runtime, "logger", logging.getLogger(__name__))
        logger.warning("Failed to build remote agent transfer buttons: %s", exc)

    buttons.append([InlineKeyboardButton("✖ Cancel", callback_data="noop")])
    return InlineKeyboardMarkup(buttons)


def job_transfer_callback(
    runtime,
    kind: str,
    task_id: str,
    target_agent: str,
    *,
    instance_id: str | None = None,
    max_selections: int = 256,
) -> str:
    token = mint_callback_token(
        runtime,
        "skilljob_transfer",
        {
            "kind": kind,
            "task_id": task_id,
            "target_agent": target_agent,
            "instance_id": instance_id,
            "remote": instance_id is not None,
        },
        prefix="jtx",
        max_entries=max_selections,
    )
    legacy_store = getattr(runtime, "_job_transfer_selections", None)
    if legacy_store is None:
        legacy_store = {}
        runtime._job_transfer_selections = legacy_store
    legacy_store[token] = {
        "kind": kind,
        "task_id": task_id,
        "target_agent": target_agent,
        "instance_id": instance_id,
        "remote": instance_id is not None,
    }
    return f"skilljob:{kind}:xferkey:{token}:go"


async def handle_skill_job_callback(runtime, query, data: str) -> bool:
    if not data.startswith("skilljob:"):
        return False

    parts = data.split(":", 4)
    if len(parts) != 5:
        await query.answer("Malformed jobs callback.", show_alert=True)
        return True
    _, kind, action, task_id, value = parts
    if action == "key":
        _runtime_logger(runtime).debug("Handling tokenized jobs callback: %s", data)
        selection = resolve_callback_token(runtime, "skilljob_action", task_id)
        if not selection:
            await query.answer("This jobs action expired. Open /jobs again.", show_alert=True)
            return True
        if selection.get("kind") != kind or selection.get("action") != value:
            await query.answer("Invalid jobs action. Open /jobs again.", show_alert=True)
            return True
        task_id = selection["task_id"]
        action = selection["action"]
        value = str(selection.get("value", ""))
    if action == "toggle":
        ok, message = runtime.skill_manager.set_job_enabled(kind, task_id, enabled=(value == "on"))
        await query.answer(message, show_alert=not ok)
        await runtime._render_skill_jobs(query, kind)
        return True
    if action == "delete":
        ok, message = runtime.skill_manager.delete_job(kind, task_id)
        await query.answer(message, show_alert=not ok)
        await runtime._render_skill_jobs(query, kind)
        return True
    if action == "run":
        job = runtime.skill_manager.get_job(kind, task_id)
        if not job:
            await query.answer("Unknown job", show_alert=True)
            return True
        await query.answer("Running job now")
        await runtime._run_job_now(job)
        return True
    if action == "transfer":
        markup = runtime._build_job_transfer_keyboard(kind, task_id)
        job = runtime.skill_manager.get_job(kind, task_id)
        job_label = (job.get("note") or task_id) if job else task_id
        await query.edit_message_text(
            f"📤 <b>Transfer job</b>\n<code>{job_label[:60]}</code>\n\nSelect target agent:",
            parse_mode="HTML",
            reply_markup=markup,
        )
        await query.answer()
        return True
    if action == "xfer_to":
        target_agent = value
        job = runtime.skill_manager.get_job(kind, task_id)
        if not job:
            await query.answer("Job not found", show_alert=True)
            return True
        ok, message, _ = runtime.skill_manager.transfer_job(kind, task_id, target_agent)
        await query.answer(message, show_alert=not ok)
        if ok:
            await query.edit_message_text(
                f"✅ Job transferred to <b>{target_agent}</b> (disabled — review before enabling).",
                parse_mode="HTML",
            )
        return True
    if action == "xfer_remote":
        parts = value.split(":", 1)
        if len(parts) != 2:
            await query.answer("Invalid target", show_alert=True)
            return True
        target_agent, instance_id = parts
        job = runtime.skill_manager.get_job(kind, task_id)
        if not job:
            await query.answer("Job not found", show_alert=True)
            return True
        await query.answer("Sending to remote instance…")
        ok, msg = await runtime._transfer_job_remote(kind, job, target_agent, instance_id)
        if ok:
            runtime.skill_manager.set_job_enabled(kind, task_id, enabled=False)
            await query.edit_message_text(
                f"✅ Job transferred to <b>{target_agent}@{instance_id}</b> (original disabled).",
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text(f"❌ Transfer failed: {msg}")
        return True
    if action == "xferkey":
        selection = resolve_callback_token(runtime, "skilljob_transfer", task_id)
        if not selection:
            selection = getattr(runtime, "_job_transfer_selections", {}).get(task_id)
        if not selection:
            await query.answer("Transfer selection expired. Open /jobs and try again.", show_alert=True)
            return True
        target_kind = selection["kind"]
        target_task_id = selection["task_id"]
        target_agent = selection["target_agent"]
        job = runtime.skill_manager.get_job(target_kind, target_task_id)
        if not job:
            await query.answer("Job not found", show_alert=True)
            return True
        if selection.get("remote"):
            instance_id = selection["instance_id"]
            await query.answer("Sending to remote instance…")
            ok, msg = await runtime._transfer_job_remote(target_kind, job, target_agent, instance_id)
            if ok:
                runtime.skill_manager.set_job_enabled(target_kind, target_task_id, enabled=False)
                await query.edit_message_text(
                    f"✅ Job transferred to <b>{target_agent}@{instance_id}</b> (original disabled).",
                    parse_mode="HTML",
                )
            else:
                await query.edit_message_text(f"❌ Transfer failed: {msg}")
            return True
        ok, message, _ = runtime.skill_manager.transfer_job(target_kind, target_task_id, target_agent)
        await query.answer(message, show_alert=not ok)
        if ok:
            await query.edit_message_text(
                f"✅ Job transferred to <b>{target_agent}</b> (disabled — review before enabling).",
                parse_mode="HTML",
            )
        return True

    return False
