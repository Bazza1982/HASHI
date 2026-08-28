from __future__ import annotations

import json
import html
import logging
import time
import uuid
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import ui_language
from orchestrator.command_ui import back_label, card_title
from orchestrator.her_v2.request_policy import job_effort_policy
from orchestrator.her_v2.models import effort_display_label
from orchestrator.job_ownership import ownership_mismatch_label

CALLBACK_DATA_LIMIT = 64
CALLBACK_TOKEN_TTL_SECONDS = 30 * 60
MAX_CALLBACK_TOKENS = 256


def _her_v2_job_effort_label(job: dict) -> str:
    policy = job_effort_policy(job)
    return (
        f"{effort_display_label(policy['effective'])} · "
        f"{ui_language.tr('jobs.fixed_policy')}"
    )


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
        return ui_language.tr("jobs.no_scheduler"), None
    try:
        if not skill_manager.tasks_path.exists():
            data = {"heartbeats": [], "crons": []}
        else:
            data = json.loads(skill_manager.tasks_path.read_text(encoding="utf-8"))
    except Exception:
        return ui_language.tr("jobs.read_failed"), None

    scope = filter_agent or ui_language.tr("jobs.scope.all")
    buttons: list = []

    all_jobs: list[tuple[str, dict]] = []

    for h in data.get("heartbeats", []):
        if filter_agent and h.get("agent") != filter_agent:
            continue
        all_jobs.append(("heartbeat", h))

    for c in data.get("crons", []):
        if filter_agent and c.get("agent") != filter_agent:
            continue
        all_jobs.append(("cron", c))

    active_count = sum(bool(job.get("enabled")) for _kind, job in all_jobs)
    lines = [
        card_title("📋", "Scheduled jobs"),
        "",
        f"<b>{html.escape(ui_language.tr('common.current'))}</b> · "
        + ui_language.tr(
            "jobs.current",
            active=f"<code>{active_count}</code>",
            configured=f"<code>{len(all_jobs)}</code>",
        ),
        f"<b>{html.escape(ui_language.tr('common.scope'))}</b> · "
        f"<code>{html.escape(scope)}</code>",
        f"<b>{html.escape(ui_language.tr('common.changes'))}</b> · "
        f"{html.escape(ui_language.tr('jobs.changes'))}",
    ]

    for kind, job in all_jobs:
        enabled = bool(job.get("enabled"))
        icon = "⏱" if kind == "heartbeat" else "📅"
        status = ui_language.tr("common.on" if enabled else "common.off")
        jid = str(job.get("id") or "unknown")
        owner = str(job.get("agent") or "?")
        if kind == "heartbeat":
            interval = int(job.get("interval_seconds", 0) or 0)
            if interval >= 3600:
                schedule = ui_language.tr(
                    "jobs.every_hours", count=interval // 3600
                )
            elif interval >= 60:
                schedule = ui_language.tr(
                    "jobs.every_minutes", count=interval // 60
                )
            else:
                schedule = ui_language.tr(
                    "jobs.every_seconds", count=interval
                )
        else:
            schedule = str(job.get("schedule") or job.get("time") or "unknown")
        lines.extend(
            [
                "",
                f"{icon} <b>{status}</b> · <code>{html.escape(jid)}</code>",
                f"<b>{html.escape(ui_language.tr('jobs.schedule'))}</b> · "
                f"<code>{html.escape(schedule)}</code>",
                f"<b>{html.escape(ui_language.tr('jobs.owner'))}</b> · "
                f"<code>{html.escape(owner)}</code>",
                f"<b>{html.escape(ui_language.tr('jobs.her_mode'))}</b> · "
                f"<code>{html.escape(_her_v2_job_effort_label(job))}</code>",
            ]
        )
        note = str(job.get("note") or "")
        if note and note != jid:
            lines.append(html.escape(note))
        mismatch = ownership_mismatch_label(job)
        if mismatch:
            lines.append(f"⚠️ {html.escape(str(mismatch))}")

    for kind, job in all_jobs:
        jid = job["id"]
        enabled = job.get("enabled", False)
        toggle_mode = "off" if enabled else "on"
        toggle_label = ui_language.tr(
            "jobs.button.turn_off" if enabled else "jobs.button.turn_on"
        )
        icon = "⏱" if kind == "heartbeat" else "📅"
        short_id = str(jid)[:22]
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
            InlineKeyboardButton(ui_language.tr("jobs.button.run"), callback_data=f"skilljob:{kind}:key:{run_token}:run"),
            InlineKeyboardButton(toggle_label, callback_data=f"skilljob:{kind}:key:{toggle_token}:toggle"),
            InlineKeyboardButton(ui_language.tr("jobs.button.transfer"), callback_data=f"skilljob:{kind}:key:{transfer_token}:transfer"),
            InlineKeyboardButton(ui_language.tr("jobs.button.delete"), callback_data=f"skilljob:{kind}:key:{delete_token}:delete"),
        ])

    if not all_jobs:
        lines.append("\n" + ui_language.tr("jobs.none"))

    markup = InlineKeyboardMarkup(buttons) if buttons else None
    return "\n".join(lines), markup


def _build_jobs_text(agent_name: str, skill_manager) -> str:
    """Build a formatted jobs listing for a single agent."""
    if skill_manager is None or not hasattr(skill_manager, "tasks_path"):
        return ui_language.tr("jobs.no_scheduler")
    try:
        if not skill_manager.tasks_path.exists():
            data = {"heartbeats": [], "crons": []}
        else:
            data = json.loads(skill_manager.tasks_path.read_text(encoding="utf-8"))
    except Exception:
        return ui_language.tr("jobs.read_failed")

    hbs = [h for h in data.get("heartbeats", []) if h.get("agent") == agent_name]
    crons = [c for c in data.get("crons", []) if c.get("agent") == agent_name]
    all_jobs = [*hbs, *crons]
    active_count = sum(bool(job.get("enabled")) for job in all_jobs)
    lines = [
        card_title("📋", "Scheduled jobs"),
        "",
        f"<b>{html.escape(ui_language.tr('common.current'))}</b> · "
        + ui_language.tr(
            "jobs.current",
            active=f"<code>{active_count}</code>",
            configured=f"<code>{len(all_jobs)}</code>",
        ),
        f"<b>{html.escape(ui_language.tr('common.scope'))}</b> · "
        f"<code>{html.escape(str(agent_name))}</code>",
        f"<b>{html.escape(ui_language.tr('common.changes'))}</b> · "
        f"{html.escape(ui_language.tr('jobs.changes'))}",
        "",
    ]
    found = False

    if hbs:
        lines.append(f"<b>{html.escape(ui_language.tr('jobs.heartbeats'))}</b>")
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
            job_id = str(h.get("id") or "unknown")
            every = ui_language.tr("status.every", interval=interval_s)
            lines.append(
                f"  {enabled} <code>{html.escape(job_id)}</code> · "
                f"{html.escape(every)}"
            )
            if action != "enqueue_prompt":
                lines.append(
                    f"      {html.escape(ui_language.tr('jobs.action'))} · "
                    f"<code>{html.escape(str(action))}</code>"
                )
            lines.append(
                f"      {html.escape(ui_language.tr('jobs.her_mode'))} · "
                f"<code>{html.escape(_her_v2_job_effort_label(h))}</code>"
            )
            if note and note != job_id:
                lines.append(f"      {html.escape(str(note))}")
            mismatch = ownership_mismatch_label(h)
            if mismatch:
                lines.append(f"      ⚠️ {html.escape(str(mismatch))}")
        lines.append("")
        found = True

    if crons:
        lines.append(f"<b>{html.escape(ui_language.tr('jobs.crons'))}</b>")
        for c in crons:
            enabled = "✓" if c.get("enabled") else "✗"
            time_s = c.get("time", "??:??")
            action = c.get("action", "enqueue_prompt")
            note = c.get("note", c.get("id", ""))
            job_id = str(c.get("id") or "unknown")
            lines.append(
                f"  {enabled} <code>{html.escape(job_id)}</code> · "
                f"{html.escape(ui_language.tr('jobs.at', time=time_s))}"
            )
            if action != "enqueue_prompt":
                lines.append(
                    f"      {html.escape(ui_language.tr('jobs.action'))} · "
                    f"<code>{html.escape(str(action))}</code>"
                )
            lines.append(
                f"      {html.escape(ui_language.tr('jobs.her_mode'))} · "
                f"<code>{html.escape(_her_v2_job_effort_label(c))}</code>"
            )
            if note and note != job_id:
                lines.append(f"      {html.escape(str(note))}")
            mismatch = ownership_mismatch_label(c)
            if mismatch:
                lines.append(f"      ⚠️ {html.escape(str(mismatch))}")
        lines.append("")
        found = True

    if not found:
        lines.append(ui_language.tr("jobs.none_agent"))

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
        buttons.append(
            [
                InlineKeyboardButton(
                    ui_language.tr("jobs.this_instance"),
                    callback_data="noop",
                )
            ]
        )
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

    buttons.append([InlineKeyboardButton(back_label(), callback_data="noop")])
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
        await query.answer(ui_language.tr("jobs.malformed_callback"), show_alert=True)
        return True
    _, kind, action, task_id, value = parts
    if action == "key":
        _runtime_logger(runtime).debug("Handling tokenized jobs callback: %s", data)
        selection = resolve_callback_token(runtime, "skilljob_action", task_id)
        if not selection:
            await query.answer(ui_language.tr("jobs.action_expired"), show_alert=True)
            return True
        if selection.get("kind") != kind or selection.get("action") != value:
            await query.answer(ui_language.tr("jobs.invalid_action"), show_alert=True)
            return True
        task_id = selection["task_id"]
        action = selection["action"]
        value = str(selection.get("value", ""))
    if action == "toggle":
        ok, message = runtime.skill_manager.set_job_enabled(kind, task_id, enabled=(value == "on"))
        await query.answer(message, show_alert=not ok)
        await runtime._render_jobs(query, kind)
        return True
    if action == "delete":
        ok, message = runtime.skill_manager.delete_job(kind, task_id)
        await query.answer(message, show_alert=not ok)
        await runtime._render_jobs(query, kind)
        return True
    if action == "run":
        job = runtime.skill_manager.get_job(kind, task_id)
        if not job:
            await query.answer(ui_language.tr("jobs.unknown"), show_alert=True)
            return True
        mismatch = ownership_mismatch_label(job)
        if mismatch:
            await query.answer(
                ui_language.tr("jobs.run_refused", reason=mismatch),
                show_alert=True,
            )
            return True
        await query.answer(ui_language.tr("jobs.running_now"))
        await runtime._run_job_now(job, kind=kind)
        return True
    if action == "transfer":
        markup = runtime._build_job_transfer_keyboard(kind, task_id)
        job = runtime.skill_manager.get_job(kind, task_id)
        job_label = (job.get("note") or task_id) if job else task_id
        await query.edit_message_text(
            f"📤 <b>{html.escape(ui_language.tr('jobs.transfer_title'))}</b>\n"
            f"<code>{html.escape(str(job_label)[:60])}</code>\n\n"
            f"{html.escape(ui_language.tr('jobs.select_target'))}",
            parse_mode="HTML",
            reply_markup=markup,
        )
        await query.answer()
        return True
    if action == "xfer_to":
        target_agent = value
        job = runtime.skill_manager.get_job(kind, task_id)
        if not job:
            await query.answer(ui_language.tr("jobs.not_found"), show_alert=True)
            return True
        ok, message, _ = runtime.skill_manager.transfer_job(kind, task_id, target_agent)
        await query.answer(message, show_alert=not ok)
        if ok:
            await query.edit_message_text(
                ui_language.tr(
                    "jobs.transferred_local", agent=html.escape(target_agent)
                ),
                parse_mode="HTML",
            )
        return True
    if action == "xfer_remote":
        parts = value.split(":", 1)
        if len(parts) != 2:
            await query.answer(ui_language.tr("jobs.invalid_target"), show_alert=True)
            return True
        target_agent, instance_id = parts
        job = runtime.skill_manager.get_job(kind, task_id)
        if not job:
            await query.answer(ui_language.tr("jobs.not_found"), show_alert=True)
            return True
        await query.answer(ui_language.tr("jobs.sending_remote"))
        ok, msg = await runtime._transfer_job_remote(kind, job, target_agent, instance_id)
        if ok:
            runtime.skill_manager.set_job_enabled(kind, task_id, enabled=False)
            await query.edit_message_text(
                ui_language.tr(
                    "jobs.transferred_remote",
                    agent=html.escape(target_agent),
                    instance=html.escape(instance_id),
                ),
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text(
                ui_language.tr("jobs.transfer_failed", reason=str(msg))
            )
        return True
    if action == "xferkey":
        selection = resolve_callback_token(runtime, "skilljob_transfer", task_id)
        if not selection:
            selection = getattr(runtime, "_job_transfer_selections", {}).get(task_id)
        if not selection:
            await query.answer(ui_language.tr("jobs.transfer_expired"), show_alert=True)
            return True
        target_kind = selection["kind"]
        target_task_id = selection["task_id"]
        target_agent = selection["target_agent"]
        job = runtime.skill_manager.get_job(target_kind, target_task_id)
        if not job:
            await query.answer(ui_language.tr("jobs.not_found"), show_alert=True)
            return True
        if selection.get("remote"):
            instance_id = selection["instance_id"]
            await query.answer(ui_language.tr("jobs.sending_remote"))
            ok, msg = await runtime._transfer_job_remote(target_kind, job, target_agent, instance_id)
            if ok:
                runtime.skill_manager.set_job_enabled(target_kind, target_task_id, enabled=False)
                await query.edit_message_text(
                    ui_language.tr(
                        "jobs.transferred_remote",
                        agent=html.escape(target_agent),
                        instance=html.escape(instance_id),
                    ),
                    parse_mode="HTML",
                )
            else:
                await query.edit_message_text(
                    ui_language.tr("jobs.transfer_failed", reason=str(msg))
                )
            return True
        ok, message, _ = runtime.skill_manager.transfer_job(target_kind, target_task_id, target_agent)
        await query.answer(message, show_alert=not ok)
        if ok:
            await query.edit_message_text(
                ui_language.tr(
                    "jobs.transferred_local", agent=html.escape(target_agent)
                ),
                parse_mode="HTML",
            )
        return True

    return False
