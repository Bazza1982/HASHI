from __future__ import annotations

import html
from typing import Any

from orchestrator.background_jobs import TERMINAL_STATES
from orchestrator.command_ui import card_title
from orchestrator.command_registry import RuntimeCommand
from orchestrator import ui_language


RESERVED_SUBCOMMANDS = {"run", "status", "tail", "cancel", "list", "ls", "help", "-h", "--help"}

def _usage() -> str:
    return ui_language.tr("bg.usage")

BG_INSTRUCTION = """This request was sent with /bg, meaning the user wants background-safe handling.

Keep the user's task text exact. If you need to run long OS/process work, use HASHI BackgroundJobManager rather than blocking the chat on a foreground shell command. When tool calls are available, start work with background_job_start and inspect it with background_job_status, background_job_tail, background_job_cancel, or background_job_list. If tool calls are not available but the local Workbench API is reachable, use its live background-job endpoints instead: POST /api/background-jobs, GET /api/background-jobs/{job_id}, GET /api/background-jobs/{job_id}/tail, or POST /api/background-jobs/{job_id}/cancel. Do not create a temporary standalone BackgroundJobManager just to simulate success. Start managed background jobs with success/failure notification and agent completion/failure event routing enabled when possible. Report the job id, current state, where logs can be tailed, and how the user can follow up with /bg status, /bg tail, or /bg cancel. When a later background-job-event is delivered, inspect the job status/logs and decide whether to summarize, continue the workflow, ask for confirmation, or report failure; do not restart the same job unless explicitly requested. If no long OS process is needed, proceed normally but keep the response concise and explain that no managed background job was required."""


def _is_authorized(runtime: Any, update: Any) -> bool:
    checker = getattr(runtime, "_is_authorized_user", None)
    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)
    if callable(checker):
        return bool(checker(user_id))
    global_config = getattr(runtime, "global_config", None)
    authorized_id = getattr(global_config, "authorized_id", None)
    return authorized_id is None or user_id == authorized_id


async def _send(runtime: Any, update: Any, text: str) -> None:
    if hasattr(runtime, "_reply_text"):
        await runtime._reply_text(update, text, parse_mode="HTML")
        return
    message = getattr(update, "message", None)
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(text, parse_mode="HTML")
        return
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is not None and hasattr(runtime, "send_long_message"):
        await runtime.send_long_message(chat_id, text, request_id="bg-command", purpose="command")


def _message_text(update: Any) -> str:
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    return str(getattr(message, "text", "") or "")


def _raw_after_bg(update: Any, args: list[str]) -> str:
    text = _message_text(update).strip()
    if text.startswith("/"):
        head, sep, tail = text.partition(" ")
        command = head.split("@", 1)[0].lower()
        if command == "/bg":
            return tail.strip() if sep else ""
    return " ".join(args).strip()


def _split_action(update: Any, args: list[str]) -> tuple[str, str]:
    raw = _raw_after_bg(update, args)
    if not raw:
        return "help", ""
    first, sep, rest = raw.partition(" ")
    sub = first.strip().lower()
    if sub in RESERVED_SUBCOMMANDS:
        if sub in {"-h", "--help"}:
            return "help", ""
        if sub == "ls":
            return "list", rest.strip()
        if sub == "run":
            return "run", rest.strip()
        return sub, rest.strip()
    return "run", raw


def _short(text: str, limit: int = 120) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _manager(runtime: Any) -> Any | None:
    kernel = getattr(runtime, "kernel", None) or getattr(runtime, "orchestrator", None)
    manager = getattr(kernel, "background_job_manager", None) if kernel is not None else None
    return manager or getattr(runtime, "background_job_manager", None)


def _job_line(record: Any) -> str:
    command = html.escape(_short((getattr(record, "command", {}) or {}).get("display", "")))
    state = html.escape(str(getattr(record, "state", "?")))
    job_id = html.escape(str(getattr(record, "job_id", "")))
    code = getattr(record, "returncode", None)
    suffix = f" exit={code}" if code is not None else ""
    return f"<code>{job_id}</code> · <b>{state.upper()}</b>{html.escape(suffix)} · {command}"


def _help_text(runtime: Any) -> str:
    manager_state = ui_language.tr(
        "bg.manager.ready" if _manager(runtime) is not None else "bg.manager.unavailable"
    )
    return "\n".join(
        [
            card_title("🧵", "Background jobs"),
            "",
            f"<b>{html.escape(ui_language.tr('common.current'))}</b> · <b>{html.escape(manager_state)}</b>",
            f"<b>{html.escape(ui_language.tr('common.scope'))}</b> · {html.escape(ui_language.tr('bg.scope'))}",
            "",
            ui_language.tr("bg.help.summary"),
            "",
            f"<b>{html.escape(ui_language.tr('common.use'))}</b>",
            f"<code>/bg &lt;task&gt;</code> · {html.escape(ui_language.tr('bg.help.run'))}",
            f"<code>/bg status [job_id]</code> · {html.escape(ui_language.tr('bg.help.status'))}",
            f"<code>/bg tail &lt;job_id&gt;</code> · {html.escape(ui_language.tr('bg.help.tail'))}",
            f"<code>/bg cancel &lt;job_id&gt;</code> · {html.escape(ui_language.tr('bg.help.cancel'))}",
            f"<code>/bg list</code> · {html.escape(ui_language.tr('bg.help.list'))}",
        ]
    )


async def _run_task(runtime: Any, update: Any, task_text: str) -> None:
    if not task_text:
        await _send(runtime, update, _usage())
        return
    chat = getattr(update, "effective_chat", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        await _send(runtime, update, ui_language.tr("bg.error.missing_chat"))
        return
    enqueue = getattr(runtime, "enqueue_request", None)
    if not callable(enqueue):
        await _send(runtime, update, ui_language.tr("bg.error.queue_unsupported"))
        return
    prompt = (
        f"{BG_INSTRUCTION}\n\n"
        "--- USER TASK ---\n"
        f"{task_text}"
    )
    request_id = await enqueue(
        chat_id,
        prompt,
        "background:prompt",
        f"Background task: {_short(task_text)}",
    )
    suffix = (
        f"\n{html.escape(ui_language.tr('bg.request'))}: "
        f"<code>{html.escape(str(request_id))}</code>"
        if request_id
        else ""
    )
    await _send(
        runtime,
        update,
        f"{ui_language.tr('bg.queued')}\n"
        f"{html.escape(ui_language.tr('bg.task'))}: <code>{html.escape(_short(task_text))}</code>{suffix}\n"
        f"{ui_language.tr('bg.queued_effect')}",
    )


async def _list_jobs(runtime: Any, update: Any, _arg: str) -> None:
    manager = _manager(runtime)
    if manager is None:
        await _send(runtime, update, ui_language.tr("bg.manager_missing"))
        return
    records = manager.list(limit=20)
    if not records:
        await _send(
            runtime,
            update,
            "\n".join(
                [
                    card_title("🧵", "Background jobs"),
                    "",
                    f"<b>{html.escape(ui_language.tr('common.current'))}</b> · <code>0</code>",
                    f"<b>{html.escape(ui_language.tr('common.scope'))}</b> · {html.escape(ui_language.tr('bg.scope.processes'))}",
                    "",
                    ui_language.tr("bg.none"),
                    "",
                    ui_language.tr("bg.start_help"),
                ]
            ),
        )
        return
    active = sum(getattr(record, "state", "") not in TERMINAL_STATES for record in records)
    lines = [
        card_title("🧵", "Background jobs"),
        "",
        f"<b>{html.escape(ui_language.tr('common.current'))}</b> · "
        + ui_language.tr("bg.count", active=f"<code>{active}</code>", recent=f"<code>{len(records)}</code>"),
        f"<b>{html.escape(ui_language.tr('common.scope'))}</b> · {html.escape(ui_language.tr('bg.scope.processes'))}",
        "",
        f"<b>{html.escape(ui_language.tr('bg.jobs'))}</b>",
    ]
    lines.extend(_job_line(record) for record in records)
    await _send(runtime, update, "\n".join(lines))


async def _status(runtime: Any, update: Any, arg: str) -> None:
    manager = _manager(runtime)
    if manager is None:
        await _send(runtime, update, ui_language.tr("bg.manager_missing"))
        return
    job_id = arg.strip()
    if not job_id:
        records = manager.list(limit=10)
        running = [record for record in records if getattr(record, "state", "") not in TERMINAL_STATES]
        failed = [record for record in records if getattr(record, "state", "") in {"failed", "timeout", "start_failed"}]
        lines = [
            card_title("🧵", "Background job status"),
            "",
            f"<b>{html.escape(ui_language.tr('common.current'))}</b> · "
            + ui_language.tr("bg.active_count", count=f"<code>{len(running)}</code>"),
            f"<b>{html.escape(ui_language.tr('common.recent'))}</b> · <code>{len(records)}</code>",
            f"<b>{html.escape(ui_language.tr('common.failed_or_timed_out'))}</b> · <code>{len(failed)}</code>",
        ]
        if records:
            lines.append("")
            lines.extend(_job_line(record) for record in records[:5])
        await _send(runtime, update, "\n".join(lines))
        return
    record = manager.get(job_id)
    if record is None:
        await _send(
            runtime,
            update,
            ui_language.tr("bg.not_found", job_id=html.escape(job_id)),
        )
        return
    lines = [
        card_title("🧵", "Background job"),
        "",
        f"<b>{html.escape(ui_language.tr('common.current'))}</b> · <b>{html.escape(str(record.state).upper())}</b>",
        f"<b>{html.escape(ui_language.tr('common.id'))}</b> · <code>{html.escape(record.job_id)}</code>",
        f"<b>{html.escape(ui_language.tr('common.return_code'))}</b> · <code>{html.escape(str(record.returncode))}</code>",
        f"<b>{html.escape(ui_language.tr('common.created'))}</b> · <code>{html.escape(str(record.created_at))}</code>",
        f"<b>{html.escape(ui_language.tr('common.updated'))}</b> · <code>{html.escape(str(record.updated_at))}</code>",
        f"<b>{html.escape(ui_language.tr('common.command'))}</b> · <code>{html.escape(_short(record.command.get('display', ''), 300))}</code>",
    ]
    if record.error:
        lines.append(
            f"<b>{html.escape(ui_language.tr('common.error'))}</b> · "
            f"{html.escape(str(record.error))}"
        )
    await _send(runtime, update, "\n".join(lines))


async def _tail(runtime: Any, update: Any, arg: str) -> None:
    manager = _manager(runtime)
    if manager is None:
        await _send(runtime, update, ui_language.tr("bg.manager_missing"))
        return
    parts = arg.split()
    if not parts:
        await _send(runtime, update, ui_language.tr("bg.tail_usage"))
        return
    job_id = parts[0]
    try:
        text = manager.tail(job_id, lines=80)
    except KeyError:
        await _send(runtime, update, ui_language.tr("bg.not_found", job_id=html.escape(job_id)))
        return
    body = html.escape(text[-3500:] if text else ui_language.tr("bg.no_stdout"))
    await _send(
        runtime,
        update,
        f"{card_title('🧵', 'Background job output')}\n\n"
        f"<b>{html.escape(ui_language.tr('common.current'))}</b> · <code>{html.escape(job_id)}</code>\n"
        f"<b>{html.escape(ui_language.tr('common.view'))}</b> · {html.escape(ui_language.tr('bg.last_lines'))}\n\n"
        f"<pre>{body}</pre>",
    )


async def _cancel(runtime: Any, update: Any, arg: str) -> None:
    manager = _manager(runtime)
    if manager is None:
        await _send(runtime, update, ui_language.tr("bg.manager_missing"))
        return
    job_id = arg.strip().split()[0] if arg.strip() else ""
    if not job_id:
        await _send(runtime, update, ui_language.tr("bg.cancel_usage"))
        return
    try:
        record = await manager.cancel(job_id)
    except KeyError:
        await _send(runtime, update, ui_language.tr("bg.not_found", job_id=html.escape(job_id)))
        return
    await _send(
        runtime,
        update,
        ui_language.tr(
            "bg.cancelled",
            job_id=html.escape(record.job_id),
            state=html.escape(record.state),
        ),
    )


async def bg_command(runtime: Any, update: Any, context: Any) -> None:
    if not _is_authorized(runtime, update):
        return
    args = [str(arg) for arg in (getattr(context, "args", None) or [])]
    action, rest = _split_action(update, args)
    if action == "help":
        await _send(runtime, update, _help_text(runtime))
        return
    if action == "run":
        await _run_task(runtime, update, rest)
        return
    if action == "list":
        await _list_jobs(runtime, update, rest)
        return
    if action == "status":
        await _status(runtime, update, rest)
        return
    if action == "tail":
        await _tail(runtime, update, rest)
        return
    if action == "cancel":
        await _cancel(runtime, update, rest)
        return
    await _send(runtime, update, _usage())


COMMANDS = [
    RuntimeCommand(
        name="bg",
        description="Run or manage background-capable agent tasks",
        callback=bg_command,
    )
]
