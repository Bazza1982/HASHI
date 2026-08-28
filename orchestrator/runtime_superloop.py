from __future__ import annotations

import json
import logging
from html import escape
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import ui_language
from orchestrator.command_ui import back_label, card_title, refresh_label
from orchestrator.superloop_compiler import SuperloopCompiler
from orchestrator.superloop_control import SuperloopControlService
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
        purpose = ui_language.tr("superloop.no_readme")
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
                "includes": " · ".join(includes) if includes else ui_language.tr("superloop.template_files"),
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
            [InlineKeyboardButton(ui_language.tr("superloop.button.templates"), callback_data="superloop:list:0")],
            [
                InlineKeyboardButton(ui_language.tr("superloop.button.recording"), callback_data="superloop:recording"),
                InlineKeyboardButton(ui_language.tr("superloop.button.controls"), callback_data="superloop:loops"),
            ],
            [InlineKeyboardButton(ui_language.tr("superloop.button.collaboration"), callback_data="superloop:collaboration")],
            [InlineKeyboardButton(refresh_label(), callback_data="superloop:menu")],
        ]
    )


def _page_keyboard(refresh_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(back_label(), callback_data="superloop:menu"),
                InlineKeyboardButton(refresh_label(), callback_data=refresh_data),
            ]
        ]
    )


def _menu_view(store: SuperloopStore) -> tuple[str, InlineKeyboardMarkup]:
    loop_total, running = _loop_counts(store)
    recording_total = _directory_count(store.recordings_dir)
    template_total = len(_template_cards(store))
    current = ui_language.tr("superloop.active" if running else "common.ready")
    lines = [
        card_title("🧭", "Superloop"),
        "",
        f"<b>{escape(ui_language.tr('common.current'))}</b> · <code>{escape(current)}</code>",
        f"<b>{escape(ui_language.tr('common.scope'))}</b> · "
        f"<code>{escape(ui_language.tr('superloop.scope'))}</code>",
        f"<b>{escape(ui_language.tr('superloop.loops'))}</b> · "
        + ui_language.tr(
            "superloop.counts",
            total=f"<code>{loop_total}</code>",
            running=f"<code>{running}</code>",
        ),
        f"<b>{escape(ui_language.tr('superloop.recordings'))}</b> · <code>{recording_total}</code>",
        f"<b>{escape(ui_language.tr('superloop.templates'))}</b> · <code>{template_total}</code>",
        f"<b>{escape(ui_language.tr('common.changes'))}</b> · {escape(ui_language.tr('superloop.changes'))}",
        "",
        ui_language.tr("superloop.summary"),
        "",
        f"<b>{escape(ui_language.tr('superloop.quick_start'))}</b>",
        "<code>/superloop quickstart &lt;goal&gt;</code>",
        "<code>/superloop wizard &lt;goal&gt;</code>",
        "",
        ui_language.tr("superloop.choose"),
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
        f"<b>{escape(ui_language.tr('common.current'))}</b> · "
        f"<code>{escape(ui_language.tr('common.ready'))}</code>",
        f"<b>{escape(ui_language.tr('common.scope'))}</b> · "
        f"<code>{escape(ui_language.tr('superloop.scope'))}</code>",
        "",
        purpose,
        "",
        f"<b>{escape(ui_language.tr('superloop.available_commands'))}</b>",
        *(f"<code>{escape(command)}</code>" for command in commands),
        "",
        note,
    ]
    return "\n".join(lines), _page_keyboard(refresh_data)


def _recording_guide_view() -> tuple[str, InlineKeyboardMarkup]:
    return _guide_view(
        "🎬",
        "Superloop recording",
        purpose=ui_language.tr("superloop.recording.purpose"),
        commands=(
            "/superloop record start <goal>",
            "/superloop record status [recording_id]",
            "/superloop record try <recording_id> <step title>",
            "/superloop record intent <recording_id> <summary>",
            "/superloop record exit <recording_id> <kind> <details-json>",
            "/superloop record finish [recording_id]",
        ),
        note=ui_language.tr("superloop.recording.note"),
        refresh_data="superloop:recording",
    )


def _loop_guide_view() -> tuple[str, InlineKeyboardMarkup]:
    return _guide_view(
        "🛠️",
        "Superloop controls",
        purpose=ui_language.tr("superloop.controls.purpose"),
        commands=(
            "/superloop status <loop_id>",
            "/superloop validate <loop_id>",
            "/superloop next <loop_id>",
            "/superloop pause <loop_id> [--drain|--immediate]",
            "/superloop resume <loop_id>",
            "/superloop closeout <loop_id>",
        ),
        note=ui_language.tr("superloop.controls.note"),
        refresh_data="superloop:loops",
    )


def _collaboration_guide_view() -> tuple[str, InlineKeyboardMarkup]:
    return _guide_view(
        "📋",
        "Superloop collaboration",
        purpose=ui_language.tr("superloop.collaboration.purpose"),
        commands=(
            "/superloop task add <loop_id> <title>",
            "/superloop issue add <loop_id> <title>",
            "/superloop wait add <loop_id> <kind> [deadline-iso]",
        ),
        note=ui_language.tr("superloop.collaboration.note"),
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
        f"<b>{escape(ui_language.tr('common.current'))}</b> · "
        f"{ui_language.tr('superloop.template_count', count=f'<code>{len(cards)}</code>')}",
        f"<b>{escape(ui_language.tr('superloop.page'))}</b> · <code>{page + 1}/{page_count}</code>",
        f"<b>{escape(ui_language.tr('common.source'))}</b> · <code>superloops/templates/</code>",
    ]
    if not cards:
        lines.extend(
            [
                "",
                ui_language.tr("superloop.none_templates"),
                ui_language.tr("superloop.add_template"),
            ]
        )
    for index, card in enumerate(visible_cards, start=start + 1):
        lines.extend(
            [
                "",
                f"<b>{index} · {escape(card['title'])}</b>",
                f"<b>{escape(ui_language.tr('common.id'))}</b> · <code>{escape(card['slug'])}</code>",
                f"<b>{escape(ui_language.tr('superloop.includes'))}</b> · <code>{escape(card['includes'])}</code>",
                escape(_compact_text(card["purpose"])),
            ]
        )
    lines.extend(
        [
            "",
            ui_language.tr("superloop.open_readme"),
        ]
    )

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(ui_language.tr("superloop.previous"), callback_data=f"superloop:list:{page - 1}"))
    nav.append(InlineKeyboardButton(refresh_label(), callback_data=f"superloop:list:{page}"))
    if page + 1 < page_count:
        nav.append(InlineKeyboardButton(ui_language.tr("superloop.next"), callback_data=f"superloop:list:{page + 1}"))
    keyboard = InlineKeyboardMarkup(
        [
            nav,
            [InlineKeyboardButton(back_label(), callback_data="superloop:menu")],
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
        await query.answer(ui_language.tr("superloop.error.unknown_view"), show_alert=True)
        return

    await query.answer()
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)


async def handle_superloop_command(runtime, update, args_text: str) -> None:
    raw = (args_text or "").strip()
    locale = ui_language.preferred_locale(runtime, update)
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
            await runtime._reply_text(
                update,
                ui_language.tr("superloop.usage.quickstart", locale=locale),
            )
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
            await runtime._reply_text(
                update,
                ui_language.tr(
                    "superloop.quickstart_failed",
                    locale=locale,
                    result=result,
                ),
            )
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
                f"{ui_language.tr('superloop.quickstart_title', locale=locale)}\n"
                f"{ui_language.tr('superloop.goal_label', locale=locale)}: {goal}\n"
                f"recording_id: `{recording_id}`\n"
                f"loop_id: `{loop_id}`\n"
                f"seed_task: `{task['task_id']}`\n\n"
                f"{ui_language.tr('superloop.next_steps', locale=locale)}\n"
                f"1) `/superloop status {loop_id}`\n"
                f"2) `/superloop next {loop_id}`\n"
                f"3) `/superloop wait add {loop_id} sleep_until <ISO-time>`\n\n"
                f"{ui_language.tr('superloop.closeout_guard', locale=locale)}"
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
                    f"{ui_language.tr('superloop.wizard_usage', locale=locale)}\n\n"
                    f"{ui_language.tr('superloop.example_heading', locale=locale)}\n"
                    f"{ui_language.tr('superloop.wizard_example', locale=locale)}"
                ),
            )
            return
        await handle_superloop_command(runtime, update, f"quickstart {goal}")
        latest_rec = _latest_recording_id(store) or "N/A"
        await runtime._reply_text(
            update,
            (
                f"{ui_language.tr('superloop.wizard_title', locale=locale)}\n"
                f"{ui_language.tr('superloop.wizard_completed', locale=locale)}\n\n"
                f"{ui_language.tr('superloop.optional_improvements', locale=locale)}\n"
                f"1) `/superloop record intent {latest_rec} "
                f"{ui_language.tr('superloop.intent_placeholder', locale=locale)}`\n"
                f"2) `/superloop record exit {latest_rec} all_tasks_completed {{\"task_ids\":[]}}`\n"
                f"3) {ui_language.tr('superloop.add_items', locale=locale)}"
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
            await runtime._reply_text(
                update,
                ui_language.tr("superloop.usage.record_start", locale=locale),
            )
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
            ui_language.tr(
                "superloop.record_started",
                locale=locale,
                recording_id=result["recording_id"],
                status=result["status"],
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:2] == ["record", "intent"]:
        if len(parts) < 4:
            await runtime._reply_text(
                update,
                ui_language.tr("superloop.usage.record_intent", locale=locale),
            )
            return
        recording_id = parts[2]
        summary = raw.split(None, 3)[3].strip()
        recording_service.set_intent_summary(
            recording_id,
            intent_summary=summary,
            actor_agent=runtime.name,
            actor_instance=local_instance,
        )
        await runtime._reply_text(
            update,
            ui_language.tr(
                "superloop.intent_updated",
                locale=locale,
                recording_id=recording_id,
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:2] == ["record", "exit"]:
        if len(parts) < 5:
            await runtime._reply_text(
                update,
                ui_language.tr("superloop.usage.record_exit", locale=locale),
            )
            return
        recording_id = parts[2]
        kind = parts[3]
        json_text = raw.split(None, 4)[4].strip()
        try:
            details = json.loads(json_text)
            if not isinstance(details, dict):
                raise ValueError(
                    ui_language.tr(
                        "superloop.details_object_required",
                        locale=locale,
                    )
                )
        except Exception as exc:
            await runtime._reply_text(
                update,
                ui_language.tr(
                    "superloop.invalid_details",
                    locale=locale,
                    reason=str(exc),
                ),
            )
            return
        recording_service.set_exit_condition(
            recording_id,
            exit_condition={"kind": kind, "details": details},
            actor_agent=runtime.name,
            actor_instance=local_instance,
        )
        await runtime._reply_text(
            update,
            ui_language.tr(
                "superloop.exit_updated",
                locale=locale,
                recording_id=recording_id,
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:2] == ["record", "status"]:
        recording_id = parts[2] if len(parts) >= 3 else _latest_recording_id(store)
        if not recording_id:
            await runtime._reply_text(
                update,
                ui_language.tr("superloop.no_recordings", locale=locale),
            )
            return
        payload = recording_service.get_status(recording_id)
        state = payload["state"]
        await runtime._reply_text(
            update,
            ui_language.tr(
                "superloop.record_status",
                locale=locale,
                recording_id=recording_id,
                status=state.get("status"),
                goal=state.get("goal"),
                finish_ready=state.get("finish_ready"),
                candidate_steps=len(state.get("candidate_steps") or []),
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:2] == ["record", "try"]:
        if len(parts) < 4:
            await runtime._reply_text(
                update,
                ui_language.tr("superloop.usage.record_try", locale=locale),
            )
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
            ui_language.tr(
                "superloop.trial_recorded",
                locale=locale,
                recording_id=recording_id,
                step_id=result["recorded_as_step_id"],
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:2] == ["record", "finish"]:
        recording_id = parts[2] if len(parts) >= 3 else _latest_recording_id(store)
        if not recording_id:
            await runtime._reply_text(
                update,
                ui_language.tr("superloop.no_recordings", locale=locale),
            )
            return
        result = compiler.compile_recording(
            recording_id,
            actor_agent=runtime.name,
            actor_instance=local_instance,
        )
        if not result.get("ok"):
            await runtime._reply_text(
                update,
                ui_language.tr(
                    "superloop.compile_blocked",
                    locale=locale,
                    recording_id=recording_id,
                    missing=", ".join(result.get("missing") or []),
                ),
                parse_mode="Markdown",
            )
            return
        await runtime._reply_text(
            update,
            ui_language.tr(
                "superloop.compiled",
                locale=locale,
                recording_id=recording_id,
                loop_id=result["loop_id"],
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:1] == ["status"]:
        if len(parts) < 2:
            await runtime._reply_text(
                update,
                ui_language.tr("superloop.usage.status", locale=locale),
            )
            return
        loop_id = parts[1]
        try:
            state = store.load_loop_state(loop_id)
        except FileNotFoundError:
            await runtime._reply_text(
                update,
                ui_language.tr(
                    "superloop.loop_not_found",
                    locale=locale,
                    loop_id=loop_id,
                ),
            )
            return
        await runtime._reply_text(
            update,
            ui_language.tr(
                "superloop.status_result",
                locale=locale,
                loop_id=loop_id,
                status=state.get("status"),
                current_step=state.get("current_step"),
                next_action=json.dumps(
                    state.get("next_action"), ensure_ascii=False
                ),
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:1] == ["validate"]:
        if len(parts) < 2:
            await runtime._reply_text(
                update,
                ui_language.tr("superloop.usage.validate", locale=locale),
            )
            return
        loop_id = parts[1]
        report = validate_loop(store, loop_id, closeout=False)
        await runtime._reply_text(update, format_validation_report(report), parse_mode="Markdown")
        return

    if lowered[:1] == ["closeout"]:
        if len(parts) < 2:
            await runtime._reply_text(
                update,
                ui_language.tr("superloop.usage.closeout", locale=locale),
            )
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
            await runtime._reply_text(
                update,
                ui_language.tr(
                    "superloop.loop_not_found",
                    locale=locale,
                    loop_id=loop_id,
                ),
            )
            return
        state["status"] = "completed"
        state["next_action"] = {"kind": "none", "reason": "validated_closeout"}
        store.save_loop_state(loop_id, state)
        store.append_loop_event(loop_id, event_type="loop.completed", data={"reason": "validated_closeout"}, actor=command_actor)
        await runtime._reply_text(
            update,
            format_validation_report(report)
            + "\n\n"
            + ui_language.tr("superloop.closeout_accepted", locale=locale),
            parse_mode="Markdown",
        )
        return

    if lowered[:1] == ["pause"]:
        if len(parts) < 2:
            await runtime._reply_text(
                update,
                ui_language.tr("superloop.usage.pause", locale=locale),
            )
            return
        loop_id = parts[1]
        mode = "immediate" if "--immediate" in lowered[2:] else "drain"
        try:
            result = SuperloopControlService(store).pause(
                loop_id,
                mode=mode,
                actor=command_actor,
                source="command",
            )
        except FileNotFoundError:
            await runtime._reply_text(
                update,
                ui_language.tr(
                    "superloop.loop_not_found",
                    locale=locale,
                    loop_id=loop_id,
                ),
            )
            return
        drain = "complete" if result["drain_complete"] else "pending"
        await runtime._reply_text(
            update,
            ui_language.tr(
                "superloop.pause_result",
                locale=locale,
                loop_id=loop_id,
                mode=mode,
                drain=drain,
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:1] == ["resume"]:
        if len(parts) < 2:
            await runtime._reply_text(
                update,
                ui_language.tr("superloop.usage.resume", locale=locale),
            )
            return
        loop_id = parts[1]
        try:
            result = SuperloopControlService(store).resume(
                loop_id,
                actor=command_actor,
                source="command",
            )
        except FileNotFoundError:
            await runtime._reply_text(
                update,
                ui_language.tr(
                    "superloop.loop_not_found",
                    locale=locale,
                    loop_id=loop_id,
                ),
            )
            return
        if not result.get("ok"):
            await runtime._reply_text(
                update,
                ui_language.tr(
                    "superloop.resume_blocked",
                    locale=locale,
                    loop_id=loop_id,
                    reason=result.get("reason"),
                    details=json.dumps(
                        result.get("details") or {}, ensure_ascii=False
                    ),
                ),
                parse_mode="Markdown",
            )
            return
        await runtime._reply_text(
            update,
            ui_language.tr(
                "superloop.resumed",
                locale=locale,
                loop_id=loop_id,
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:1] == ["next"]:
        if len(parts) < 2:
            await runtime._reply_text(
                update,
                ui_language.tr("superloop.usage.next", locale=locale),
            )
            return
        loop_id = parts[1]
        runner = SuperloopRunner(store)
        try:
            result = runner.next_action(loop_id)
        except FileNotFoundError:
            await runtime._reply_text(
                update,
                ui_language.tr(
                    "superloop.loop_not_found",
                    locale=locale,
                    loop_id=loop_id,
                ),
            )
            return
        await runtime._reply_text(
            update,
            ui_language.tr(
                "superloop.next_result",
                locale=locale,
                loop_id=loop_id,
                advanced=result.get("advanced"),
                reason=result.get("reason", ""),
                task_id=result.get("task_id", ""),
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:2] == ["task", "add"]:
        if len(parts) < 4:
            await runtime._reply_text(
                update,
                ui_language.tr("superloop.usage.task_add", locale=locale),
            )
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
            await runtime._reply_text(
                update,
                ui_language.tr(
                    "superloop.loop_not_found",
                    locale=locale,
                    loop_id=loop_id,
                ),
            )
            return
        await runtime._reply_text(
            update,
            ui_language.tr(
                "superloop.task_added",
                locale=locale,
                task_id=task["task_id"],
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:2] == ["issue", "add"]:
        if len(parts) < 4:
            await runtime._reply_text(
                update,
                ui_language.tr("superloop.usage.issue_add", locale=locale),
            )
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
            await runtime._reply_text(
                update,
                ui_language.tr(
                    "superloop.loop_not_found",
                    locale=locale,
                    loop_id=loop_id,
                ),
            )
            return
        await runtime._reply_text(
            update,
            ui_language.tr(
                "superloop.issue_opened",
                locale=locale,
                issue_id=issue["issue_id"],
            ),
            parse_mode="Markdown",
        )
        return

    if lowered[:2] == ["wait", "add"]:
        if len(parts) < 4:
            await runtime._reply_text(
                update,
                ui_language.tr("superloop.usage.wait_add", locale=locale),
            )
            return
        loop_id = parts[2]
        kind = parts[3]
        deadline = parts[4] if len(parts) >= 5 else None
        details = {"until": deadline} if kind == "sleep_until" and deadline else None
        service = SuperloopWaitsService(store)
        try:
            wait = service.add_wait(loop_id, kind=kind, details=details, deadline=deadline, actor=command_actor)
        except FileNotFoundError:
            await runtime._reply_text(
                update,
                ui_language.tr(
                    "superloop.loop_not_found",
                    locale=locale,
                    loop_id=loop_id,
                ),
            )
            return
        await runtime._reply_text(
            update,
            ui_language.tr(
                "superloop.wait_added",
                locale=locale,
                wait_id=wait["wait_id"],
            ),
            parse_mode="Markdown",
        )
        return

    text, markup = _menu_view(store)
    await runtime._reply_text(update, text, parse_mode="HTML", reply_markup=markup)
