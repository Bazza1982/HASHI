from __future__ import annotations

import json
import logging
from html import escape
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator.command_ui import BACK_LABEL, REFRESH_LABEL, card_title
from orchestrator.superloop_compiler import SuperloopCompiler
from orchestrator.superloop_issues import SuperloopIssuesService
from orchestrator.superloop_recording import SuperloopRecordingService
from orchestrator.superloop_runner import SuperloopRunner
from orchestrator.superloop_store import SuperloopStore, agent_actor
from orchestrator.superloop_taskboard import SuperloopTaskboardService
from orchestrator.superloop_validator import format_validation_report, validate_loop
from orchestrator.superloop_waits import SuperloopWaitsService

logger = logging.getLogger("BridgeU.Superloop")

TEMPLATES_PER_PAGE = 4


def _local_instance_id() -> str:
    try:
        from tools.hchat_send import _get_instance_id, _load_config

        return str(_get_instance_id(_load_config()) or "HASHI").upper()
    except Exception as exc:
        logger.warning("Falling back to default local instance id HASHI: %s", exc)
        return "HASHI"


def _build_store(runtime) -> SuperloopStore:
    root = Path(runtime.global_config.project_root) / "superloops"
    return SuperloopStore(root)


def _build_services(runtime) -> tuple[SuperloopStore, SuperloopRecordingService, SuperloopCompiler]:
    store = _build_store(runtime)
    return store, SuperloopRecordingService(store), SuperloopCompiler(store)


def _latest_recording_id(store: SuperloopStore) -> str | None:
    candidates = [item for item in store.recordings_dir.iterdir() if item.is_dir()]
    if not candidates:
        return None
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0].name


def _template_cards(store: SuperloopStore) -> list[dict[str, str]]:
    templates_root = store.root_dir / "templates"
    if not templates_root.exists():
        return []
    cards: list[dict[str, str]] = []
    for template_dir in sorted((item for item in templates_root.iterdir() if item.is_dir()), key=lambda item: item.name.lower()):
        title = template_dir.name.replace("_", " ").strip() or template_dir.name
        purpose = "No README summary found."
        readme_path = template_dir / "README.md"
        if readme_path.exists():
            try:
                lines = readme_path.read_text(encoding="utf-8").splitlines()
            except Exception:
                lines = []
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("# "):
                    title = stripped[2:].strip() or title
                    break
            for idx, line in enumerate(lines):
                if line.strip().lower() == "## purpose":
                    snippet: list[str] = []
                    for body_line in lines[idx + 1 :]:
                        stripped = body_line.strip()
                        if not stripped:
                            if snippet:
                                break
                            continue
                        if stripped.startswith("#"):
                            break
                        snippet.append(stripped)
                    if snippet:
                        purpose = " ".join(snippet)
                    break
        includes: list[str] = []
        if readme_path.exists():
            includes.append("README")
        if (template_dir / "taskboard.template.json").exists():
            includes.append("taskboard")
        if (template_dir / "roles.template.json").exists():
            includes.append("roles")
        if (template_dir / "evidence.schema.md").exists():
            includes.append("evidence")
        cards.append(
            {
                "slug": template_dir.name,
                "title": title,
                "purpose": purpose,
                "includes": " · ".join(includes) if includes else "template files",
            }
        )
    return cards


def _compact_text(value: object, *, limit: int = 140) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _directory_count(path: Path) -> int:
    try:
        return sum(1 for item in path.iterdir() if item.is_dir())
    except OSError:
        return 0


def _loop_counts(store: SuperloopStore) -> tuple[int, int]:
    total = 0
    running = 0
    try:
        loop_dirs = [item for item in store.loops_dir.iterdir() if item.is_dir()]
    except OSError:
        loop_dirs = []
    for loop_dir in loop_dirs:
        total += 1
        try:
            state = json.loads((loop_dir / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(state, dict) and str(state.get("status") or "").lower() == "running":
            running += 1
    return total, running


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Browse templates", callback_data="superloop:list:0")],
            [
                InlineKeyboardButton("Recording guide", callback_data="superloop:recording"),
                InlineKeyboardButton("Loop controls", callback_data="superloop:loops"),
            ],
            [InlineKeyboardButton("Tasks, issues & waits", callback_data="superloop:collaboration")],
            [InlineKeyboardButton(REFRESH_LABEL, callback_data="superloop:menu")],
        ]
    )


def _page_keyboard(refresh_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(BACK_LABEL, callback_data="superloop:menu"),
                InlineKeyboardButton(REFRESH_LABEL, callback_data=refresh_data),
            ]
        ]
    )


def _menu_view(store: SuperloopStore) -> tuple[str, InlineKeyboardMarkup]:
    loop_total, running = _loop_counts(store)
    recording_total = _directory_count(store.recordings_dir)
    template_total = len(_template_cards(store))
    current = "ACTIVE" if running else "READY"
    lines = [
        card_title("🧭", "Superloop"),
        "",
        f"<b>Current</b> · <code>{current}</code>",
        "<b>Scope</b> · <code>local project</code>",
        f"<b>Loops</b> · <code>{loop_total}</code> total · <code>{running}</code> running",
        f"<b>Recordings</b> · <code>{recording_total}</code>",
        f"<b>Templates</b> · <code>{template_total}</code>",
        "<b>Changes</b> · immediate and persistent",
        "",
        "Superloop coordinates long-running work with persisted tasks, waits, issues, and evidence.",
        "",
        "<b>Quick start</b>",
        "<code>/superloop quickstart &lt;goal&gt;</code>",
        "<code>/superloop wizard &lt;goal&gt;</code>",
        "",
        "Choose a section below for detailed commands.",
    ]
    return "\n".join(lines), _menu_keyboard()


def _guide_view(
    icon: str,
    title: str,
    *,
    purpose: str,
    commands: tuple[str, ...],
    note: str,
    refresh_data: str,
) -> tuple[str, InlineKeyboardMarkup]:
    lines = [
        card_title(icon, title),
        "",
        "<b>Current</b> · <code>READY</code>",
        "<b>Scope</b> · <code>local project</code>",
        "",
        purpose,
        "",
        "<b>Available commands</b>",
        *(f"<code>{escape(command)}</code>" for command in commands),
        "",
        note,
    ]
    return "\n".join(lines), _page_keyboard(refresh_data)


def _recording_guide_view() -> tuple[str, InlineKeyboardMarkup]:
    return _guide_view(
        "🎬",
        "Superloop recording",
        purpose="Capture a successful workflow before compiling it into a reusable loop.",
        commands=(
            "/superloop record start <goal>",
            "/superloop record status [recording_id]",
            "/superloop record try <recording_id> <step title>",
            "/superloop record intent <recording_id> <summary>",
            "/superloop record exit <recording_id> <kind> <details-json>",
            "/superloop record finish [recording_id]",
        ),
        note="Finish compiles only when the recording has a clear intent, usable steps, and an exit condition.",
        refresh_data="superloop:recording",
    )


def _loop_guide_view() -> tuple[str, InlineKeyboardMarkup]:
    return _guide_view(
        "🛠️",
        "Superloop controls",
        purpose="Inspect, validate, advance, pause, resume, or close a compiled loop.",
        commands=(
            "/superloop status <loop_id>",
            "/superloop validate <loop_id>",
            "/superloop next <loop_id>",
            "/superloop pause <loop_id>",
            "/superloop resume <loop_id>",
            "/superloop closeout <loop_id>",
        ),
        note=(
            "⚠️ Closeout remains blocked until worker and reviewer replies are drained, "
            "classified, and backed by the required evidence."
        ),
        refresh_data="superloop:loops",
    )


def _collaboration_guide_view() -> tuple[str, InlineKeyboardMarkup]:
    return _guide_view(
        "📋",
        "Superloop collaboration",
        purpose="Add the tasks, issues, and wait conditions that keep a long-running loop explicit.",
        commands=(
            "/superloop task add <loop_id> <title>",
            "/superloop issue add <loop_id> <title>",
            "/superloop wait add <loop_id> <kind> [deadline-iso]",
        ),
        note="ℹ️ Waits default to <code>on_timeout=advance</code> and do not automatically open an issue.",
        refresh_data="superloop:collaboration",
    )


def _template_list_view(
    store: SuperloopStore,
    *,
    page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    cards = _template_cards(store)
    page_count = max(1, (len(cards) + TEMPLATES_PER_PAGE - 1) // TEMPLATES_PER_PAGE)
    page = min(max(int(page), 0), page_count - 1)
    start = page * TEMPLATES_PER_PAGE
    visible_cards = cards[start : start + TEMPLATES_PER_PAGE]
    lines = [
        card_title("📚", "Superloop templates"),
        "",
        f"<b>Current</b> · <code>{len(cards)}</code> templates",
        f"<b>Page</b> · <code>{page + 1}/{page_count}</code>",
        "<b>Source</b> · <code>superloops/templates/</code>",
    ]
    if not cards:
        lines.extend(
            [
                "",
                "No templates are available yet.",
                "Add a template directory under the source path, then refresh this view.",
            ]
        )
    for index, card in enumerate(visible_cards, start=start + 1):
        lines.extend(
            [
                "",
                f"<b>{index} · {escape(card['title'])}</b>",
                f"<b>ID</b> · <code>{escape(card['slug'])}</code>",
                f"<b>Includes</b> · <code>{escape(card['includes'])}</code>",
                escape(_compact_text(card["purpose"])),
            ]
        )
    lines.extend(
        [
            "",
            "Open <code>superloops/templates/&lt;slug&gt;/README.md</code> for full details.",
        ]
    )

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("← Previous", callback_data=f"superloop:list:{page - 1}"))
    nav.append(InlineKeyboardButton(REFRESH_LABEL, callback_data=f"superloop:list:{page}"))
    if page + 1 < page_count:
        nav.append(InlineKeyboardButton("Next →", callback_data=f"superloop:list:{page + 1}"))
    keyboard = InlineKeyboardMarkup(
        [
            nav,
            [InlineKeyboardButton(BACK_LABEL, callback_data="superloop:menu")],
        ]
    )
    return "\n".join(lines), keyboard


async def handle_superloop_callback(runtime, update, _context=None) -> None:
    query = getattr(update, "callback_query", None)
    if query is None:
        return

    parts = str(getattr(query, "data", "") or "").split(":")
    action = parts[1] if len(parts) > 1 else "menu"
    store = _build_store(runtime)

    if action == "menu":
        text, markup = _menu_view(store)
    elif action == "list":
        try:
            page = int(parts[2]) if len(parts) > 2 else 0
        except ValueError:
            page = 0
        text, markup = _template_list_view(store, page=page)
    elif action == "recording":
        text, markup = _recording_guide_view()
    elif action == "loops":
        text, markup = _loop_guide_view()
    elif action == "collaboration":
        text, markup = _collaboration_guide_view()
    else:
        await query.answer("Unknown Superloop view.", show_alert=True)
        return

    await query.answer()
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)


async def handle_superloop_command(runtime, update, args_text: str) -> None:
    raw = (args_text or "").strip()
    if not raw:
        text, markup = _menu_view(_build_store(runtime))
        await runtime._reply_text(update, text, parse_mode="HTML", reply_markup=markup)
        return

    store, recording_service, compiler = _build_services(runtime)
    parts = raw.split()
    lowered = [part.lower() for part in parts]
    local_instance = _local_instance_id()
    command_actor = agent_actor(runtime.name, instance=local_instance, source="superloop_command")

    if lowered[:1] == ["quickstart"]:
        goal = raw[len("quickstart") :].strip()
        if not goal:
            await runtime._reply_text(update, "Usage: /superloop quickstart <goal>")
            return
        start_result = recording_service.start_recording(
            goal=goal,
            owner_agent=runtime.name,
            owner_instance=local_instance,
            source_mode="one_shot_prompt",
        )
        recording_id = start_result["recording_id"]
        recording_service.set_intent_summary(
            recording_id,
            intent_summary=goal,
            actor_agent=runtime.name,
            actor_instance=local_instance,
        )
        recording_service.record_trial_step(
            recording_id,
            title=f"Bootstrap loop for: {goal}",
            step_kind="human_or_agent_action",
            owner_agent=runtime.name,
            owner_instance=local_instance,
            execution_mode="simulated",
            success=True,
        )
        recording_service.set_exit_condition(
            recording_id,
            exit_condition={"kind": "all_tasks_completed", "details": {"task_ids": []}},
            actor_agent=runtime.name,
            actor_instance=local_instance,
        )
        result = compiler.compile_recording(
            recording_id,
            actor_agent=runtime.name,
            actor_instance=local_instance,
        )
        if not result.get("ok"):
            await runtime._reply_text(update, f"⚠️ quickstart compile failed: {result}")
            return
        loop_id = str(result["loop_id"])
        store.save_loop_state(loop_id, {**store.load_loop_state(loop_id), "status": "running"})
        store.append_loop_event(loop_id, event_type="loop.resumed", data={"source": "quickstart"}, actor=command_actor)
        task = SuperloopTaskboardService(store).add_task(
            loop_id,
            title=f"First actionable task for: {goal}",
            owner_agent=runtime.name,
            owner_instance=local_instance,
            actor=command_actor,
        )
        await runtime._reply_text(
            update,
            (
                "🚀 Superloop Quickstart 完成\n"
                f"goal: {goal}\n"
                f"recording_id: `{recording_id}`\n"
                f"loop_id: `{loop_id}`\n"
                f"seed_task: `{task['task_id']}`\n\n"
                "下一步建议：\n"
                f"1) `/superloop status {loop_id}`\n"
                f"2) `/superloop next {loop_id}`\n"
                f"3) `/superloop wait add {loop_id} sleep_until <ISO时间>`\n\n"
                "Closeout guard: final 前请先 drain/classify 同 loop 的 worker/reviewer replies，"
                "避免旧 hchat reply 在 close 后逐条回放。"
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:1] == ["wizard"]:
        goal = raw[len("wizard") :].strip()
        if not goal:
            await runtime._reply_text(
                update,
                (
                    "Usage: /superloop wizard <goal>\n\n"
                    "示例：\n"
                    "/superloop wizard 每天追踪远端实例健康并异常通知"
                ),
            )
            return
        await handle_superloop_command(runtime, update, f"quickstart {goal}")
        latest_rec = _latest_recording_id(store) or "N/A"
        await runtime._reply_text(
            update,
            (
                "🪄 Superloop Wizard 引导\n"
                "已为您自动完成基础建模（quickstart）。\n\n"
                "建议完善（可选）：\n"
                f"1) `/superloop record intent {latest_rec} <更精确意图>`\n"
                f"2) `/superloop record exit {latest_rec} all_tasks_completed {{\"task_ids\":[]}}`\n"
                "3) 给 loop 增加 task / wait / issue 以形成长期闭环"
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:1] == ["list"]:
        text, markup = _template_list_view(store)
        await runtime._reply_text(update, text, parse_mode="HTML", reply_markup=markup)
        return

    if lowered[:2] == ["record", "start"]:
        goal = raw[len("record start") :].strip()
        if not goal:
            await runtime._reply_text(update, "Usage: /superloop record start <goal>")
            return
        result = recording_service.start_recording(
            goal=goal,
            owner_agent=runtime.name,
            owner_instance=local_instance,
            source_mode="incremental",
        )
        recording_service.set_intent_summary(
            result["recording_id"],
            intent_summary=goal,
            actor_agent=runtime.name,
            actor_instance=local_instance,
        )
        recording_service.set_exit_condition(
            result["recording_id"],
            exit_condition={"kind": "all_tasks_completed", "details": {"task_ids": []}},
            actor_agent=runtime.name,
            actor_instance=local_instance,
        )
        await runtime._reply_text(
            update,
            (
                "✅ Superloop recording started\n"
                f"recording_id: `{result['recording_id']}`\n"
                f"status: `{result['status']}`"
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:2] == ["record", "intent"]:
        if len(parts) < 4:
            await runtime._reply_text(update, "Usage: /superloop record intent <recording_id> <summary>")
            return
        recording_id = parts[2]
        summary = raw.split(None, 3)[3].strip()
        recording_service.set_intent_summary(
            recording_id,
            intent_summary=summary,
            actor_agent=runtime.name,
            actor_instance=local_instance,
        )
        await runtime._reply_text(update, f"✅ intent summary updated for `{recording_id}`", parse_mode="Markdown")
        return

    if lowered[:2] == ["record", "exit"]:
        if len(parts) < 5:
            await runtime._reply_text(
                update,
                "Usage: /superloop record exit <recording_id> <kind> <details-json>",
            )
            return
        recording_id = parts[2]
        kind = parts[3]
        json_text = raw.split(None, 4)[4].strip()
        try:
            details = json.loads(json_text)
            if not isinstance(details, dict):
                raise ValueError("details must be a JSON object")
        except Exception as exc:
            await runtime._reply_text(update, f"Invalid details JSON: {exc}")
            return
        recording_service.set_exit_condition(
            recording_id,
            exit_condition={"kind": kind, "details": details},
            actor_agent=runtime.name,
            actor_instance=local_instance,
        )
        await runtime._reply_text(update, f"✅ exit condition updated for `{recording_id}`", parse_mode="Markdown")
        return

    if lowered[:2] == ["record", "status"]:
        recording_id = parts[2] if len(parts) >= 3 else _latest_recording_id(store)
        if not recording_id:
            await runtime._reply_text(update, "No recording sessions found.")
            return
        payload = recording_service.get_status(recording_id)
        state = payload["state"]
        await runtime._reply_text(
            update,
            (
                "🧾 Superloop recording status\n"
                f"recording_id: `{recording_id}`\n"
                f"status: `{state.get('status')}`\n"
                f"goal: {state.get('goal')}\n"
                f"finish_ready: `{state.get('finish_ready')}`\n"
                f"candidate_steps: `{len(state.get('candidate_steps') or [])}`"
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:2] == ["record", "try"]:
        if len(parts) < 4:
            await runtime._reply_text(update, "Usage: /superloop record try <recording_id> <step title>")
            return
        recording_id = parts[2]
        title = raw.split(None, 3)[3].strip()
        result = recording_service.record_trial_step(
            recording_id,
            title=title,
            step_kind="human_or_agent_action",
            owner_agent=runtime.name,
            owner_instance=local_instance,
            execution_mode="simulated",
            success=True,
        )
        await runtime._reply_text(
            update,
            (
                "🧪 Recorded trial step\n"
                f"recording_id: `{recording_id}`\n"
                f"step_id: `{result['recorded_as_step_id']}`"
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:2] == ["record", "finish"]:
        recording_id = parts[2] if len(parts) >= 3 else _latest_recording_id(store)
        if not recording_id:
            await runtime._reply_text(update, "No recording sessions found.")
            return
        result = compiler.compile_recording(
            recording_id,
            actor_agent=runtime.name,
            actor_instance=local_instance,
        )
        if not result.get("ok"):
            await runtime._reply_text(
                update,
                (
                    "⚠️ compile_blocked\n"
                    f"recording_id: `{recording_id}`\n"
                    f"missing: `{', '.join(result.get('missing') or [])}`"
                ),
                parse_mode="Markdown",
            )
            return
        await runtime._reply_text(
            update,
            (
                "✅ Superloop compiled\n"
                f"recording_id: `{recording_id}`\n"
                f"loop_id: `{result['loop_id']}`"
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:1] == ["status"]:
        if len(parts) < 2:
            await runtime._reply_text(update, "Usage: /superloop status <loop_id>")
            return
        loop_id = parts[1]
        try:
            state = store.load_loop_state(loop_id)
        except FileNotFoundError:
            await runtime._reply_text(update, f"Loop not found: {loop_id}")
            return
        await runtime._reply_text(
            update,
            (
                "📌 Superloop status\n"
                f"loop_id: `{loop_id}`\n"
                f"status: `{state.get('status')}`\n"
                f"current_step: `{state.get('current_step')}`\n"
                f"next_action: `{json.dumps(state.get('next_action'), ensure_ascii=False)}`"
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:1] == ["validate"]:
        if len(parts) < 2:
            await runtime._reply_text(update, "Usage: /superloop validate <loop_id>")
            return
        loop_id = parts[1]
        report = validate_loop(store, loop_id, closeout=False)
        await runtime._reply_text(update, format_validation_report(report), parse_mode="Markdown")
        return

    if lowered[:1] == ["closeout"]:
        if len(parts) < 2:
            await runtime._reply_text(update, "Usage: /superloop closeout <loop_id>")
            return
        loop_id = parts[1]
        report = validate_loop(store, loop_id, closeout=True)
        if report.get("blocking"):
            if store.loop_dir(loop_id).exists():
                store.append_loop_event(
                    loop_id,
                    event_type="loop.closeout_blocked",
                    data={"source": "command", "summary": report.get("summary"), "findings": report.get("findings", [])[:8]},
                    actor=command_actor,
                )
            await runtime._reply_text(update, format_validation_report(report), parse_mode="Markdown")
            return
        try:
            state = store.load_loop_state(loop_id)
        except FileNotFoundError:
            await runtime._reply_text(update, f"Loop not found: {loop_id}")
            return
        state["status"] = "completed"
        state["next_action"] = {"kind": "none", "reason": "validated_closeout"}
        store.save_loop_state(loop_id, state)
        store.append_loop_event(loop_id, event_type="loop.completed", data={"reason": "validated_closeout"}, actor=command_actor)
        await runtime._reply_text(
            update,
            format_validation_report(report) + "\n\n✅ closeout accepted: loop marked `completed`.",
            parse_mode="Markdown",
        )
        return

    if lowered[:1] == ["pause"]:
        if len(parts) < 2:
            await runtime._reply_text(update, "Usage: /superloop pause <loop_id>")
            return
        loop_id = parts[1]
        try:
            state = store.load_loop_state(loop_id)
        except FileNotFoundError:
            await runtime._reply_text(update, f"Loop not found: {loop_id}")
            return
        state["status"] = "paused"
        store.save_loop_state(loop_id, state)
        store.append_loop_event(loop_id, event_type="loop.paused", data={"source": "command"}, actor=command_actor)
        await runtime._reply_text(update, f"⏸ Paused `{loop_id}`", parse_mode="Markdown")
        return

    if lowered[:1] == ["resume"]:
        if len(parts) < 2:
            await runtime._reply_text(update, "Usage: /superloop resume <loop_id>")
            return
        loop_id = parts[1]
        try:
            state = store.load_loop_state(loop_id)
        except FileNotFoundError:
            await runtime._reply_text(update, f"Loop not found: {loop_id}")
            return
        state["status"] = "running"
        store.save_loop_state(loop_id, state)
        store.append_loop_event(loop_id, event_type="loop.resumed", data={"source": "command"}, actor=command_actor)
        await runtime._reply_text(update, f"▶ Resumed `{loop_id}`", parse_mode="Markdown")
        return

    if lowered[:1] == ["next"]:
        if len(parts) < 2:
            await runtime._reply_text(update, "Usage: /superloop next <loop_id>")
            return
        loop_id = parts[1]
        runner = SuperloopRunner(store)
        try:
            result = runner.next_action(loop_id)
        except FileNotFoundError:
            await runtime._reply_text(update, f"Loop not found: {loop_id}")
            return
        await runtime._reply_text(
            update,
            (
                "⏭ Next action evaluated\n"
                f"loop_id: `{loop_id}`\n"
                f"advanced: `{result.get('advanced')}`\n"
                f"reason: `{result.get('reason', '')}`\n"
                f"task_id: `{result.get('task_id', '')}`"
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:2] == ["task", "add"]:
        if len(parts) < 4:
            await runtime._reply_text(update, "Usage: /superloop task add <loop_id> <title>")
            return
        loop_id = parts[2]
        title = raw.split(None, 3)[3].strip()
        service = SuperloopTaskboardService(store)
        try:
            task = service.add_task(
                loop_id,
                title=title,
                owner_agent=runtime.name,
                owner_instance=local_instance,
                actor=command_actor,
            )
        except FileNotFoundError:
            await runtime._reply_text(update, f"Loop not found: {loop_id}")
            return
        await runtime._reply_text(update, f"✅ task added: `{task['task_id']}`", parse_mode="Markdown")
        return

    if lowered[:2] == ["issue", "add"]:
        if len(parts) < 4:
            await runtime._reply_text(update, "Usage: /superloop issue add <loop_id> <title>")
            return
        loop_id = parts[2]
        title = raw.split(None, 3)[3].strip()
        service = SuperloopIssuesService(store)
        try:
            issue = service.open_issue(
                loop_id,
                title=title,
                severity="medium",
                opened_by_agent=runtime.name,
                opened_by_instance=local_instance,
                actor=command_actor,
            )
        except FileNotFoundError:
            await runtime._reply_text(update, f"Loop not found: {loop_id}")
            return
        await runtime._reply_text(update, f"✅ issue opened: `{issue['issue_id']}`", parse_mode="Markdown")
        return

    if lowered[:2] == ["wait", "add"]:
        if len(parts) < 4:
            await runtime._reply_text(update, "Usage: /superloop wait add <loop_id> <kind> [deadline-iso]")
            return
        loop_id = parts[2]
        kind = parts[3]
        deadline = parts[4] if len(parts) >= 5 else None
        details = {"until": deadline} if kind == "sleep_until" and deadline else None
        service = SuperloopWaitsService(store)
        try:
            wait = service.add_wait(loop_id, kind=kind, details=details, deadline=deadline, actor=command_actor)
        except FileNotFoundError:
            await runtime._reply_text(update, f"Loop not found: {loop_id}")
            return
        await runtime._reply_text(update, f"✅ wait added: `{wait['wait_id']}`", parse_mode="Markdown")
        return

    text, markup = _menu_view(store)
    await runtime._reply_text(update, text, parse_mode="HTML", reply_markup=markup)
