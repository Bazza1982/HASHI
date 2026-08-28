from __future__ import annotations

import shutil
from contextlib import suppress
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from orchestrator import runtime_pending, ui_language
from orchestrator.bridge_memory import BridgeContextAssembler, BridgeMemoryStore
from orchestrator.command_ui import card_title, confirm_card
from orchestrator.handoff_builder import HandoffBuilder
from orchestrator.her_v2.runtime_configuration import HER_V2_CONFIGURATION_STATE_KEY
from orchestrator.memory_index import MemoryIndex
from orchestrator.memory_search_mode import (
    apply_memory_search_preference,
    is_memory_search_enabled,
    set_memory_search_enabled,
)
from orchestrator.memory_plus_mode import (
    ensure_memory_plus_notepad,
    ensure_memory_plus_observer,
    get_memory_plus_status,
    is_memory_plus_enabled,
    set_memory_plus_enabled,
)


def memory_plus_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    toggle_label = ui_language.tr(
        "memory.button.pause_plus" if enabled else "memory.button.enable_plus"
    )
    toggle_action = "memory_off" if enabled else "memory_on"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    ui_language.tr("memory.button.today"),
                    callback_data="npad:refresh",
                ),
                InlineKeyboardButton(
                    ui_language.tr("memory.button.carryover"),
                    callback_data="npad:carryover",
                ),
            ],
            [
                InlineKeyboardButton(
                    ui_language.tr("memory.button.history"),
                    callback_data="npad:history",
                ),
                InlineKeyboardButton(
                    ui_language.tr("memory.button.find"),
                    callback_data="npad:help_find",
                ),
            ],
            [
                InlineKeyboardButton(toggle_label, callback_data=f"npad:{toggle_action}"),
                InlineKeyboardButton(
                    ui_language.tr("memory.button.compact"),
                    callback_data="npad:compact",
                ),
            ],
        ]
    )


async def cmd_memory(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    raw_args = [str(value).strip() for value in (context.args or []) if str(value).strip()]
    args = " ".join(raw_args).strip().lower()
    assembler = getattr(runtime, "context_assembler", None)

    if raw_args and raw_args[0].casefold() == "raw":
        if len(raw_args) < 4:
            await runtime._reply_text(
                update,
                ui_language.tr("memory.raw_usage"),
                parse_mode="HTML",
            )
            return
        connectivity_check = getattr(runtime, "_is_hashi_tool_connected", None)
        connected = (
            bool(connectivity_check("memory_search"))
            if callable(connectivity_check)
            else "memory_search"
            in {
                item.get("name")
                for item in runtime._get_available_tool_catalogue()
                if isinstance(item, dict)
            }
        )
        if not connected:
            await runtime._reply_text(
                update,
                ui_language.tr("memory.raw_unavailable"),
            )
            return
        instance_id, agent_id = raw_args[1], raw_args[2]
        query = " ".join(raw_args[3:]).strip()
        purpose = f"User invoked /memory raw for {instance_id}/{agent_id}"
        request_id = await runtime.enqueue_request(
            chat_id=update.effective_chat.id,
            prompt=(
                "Use the authorised memory_search Tool exactly once with "
                f"scope=cross_agent, instance_id={instance_id!r}, agent_id={agent_id!r}, "
                f"purpose={purpose!r}, and query={query!r}. Preserve provenance in the answer."
            ),
            source="memory:raw-search",
            summary=f"Authorised raw memory search: {instance_id}/{agent_id}",
            request_metadata={
                "tool_allowlist": ["memory_search"],
                "memory_search_authorization": {
                    "authorization": "explicit_user_authorization",
                    "instance_id": instance_id,
                    "agent_id": agent_id,
                    "purpose": purpose,
                    "authorizing_user_id": update.effective_user.id,
                }
            },
        )
        if not request_id:
            await runtime._reply_text(update, ui_language.tr("memory.raw_queue_failed"))
        return

    if args in ("", "status"):
        from orchestrator import runtime_session

        search_enabled = is_memory_search_enabled(runtime.workspace_dir)
        if assembler:
            turns_state = (
                f"{ui_language.tr('common.on')} ✅"
                if assembler.turns_injection_enabled
                else f"{ui_language.tr('common.off')} ⏸️"
            )
            search_state = (
                f"{ui_language.tr('common.on')} ✅"
                if search_enabled
                else f"{ui_language.tr('common.off')} ⬜"
            )
            state = ui_language.tr(
                "memory.state.turns",
                turns=turns_state,
                search=search_state,
            )
        else:
            state = ui_language.tr("memory.state.unknown")
        session = runtime_session.current_session_for_update(runtime, update)
        session_messages = runtime_session.ensure_store(runtime).messages(
            session["session_id"], owner_id=runtime_session.owner_id(runtime), limit=1000
        )
        stats = runtime.memory_store.get_stats() if hasattr(runtime, "memory_store") else {}
        memories = stats.get("memories", "?")
        sync_on = runtime._get_skill_state().get("memory_sync", False)
        sync_state = (
            f"{ui_language.tr('common.on')} 🔄"
            if sync_on
            else f"{ui_language.tr('common.off')} ⬜"
        )
        continuity = get_memory_plus_status(
            runtime_session.current_session_workspace(runtime, update)
        )
        continuity["enabled"] = is_memory_plus_enabled(runtime.workspace_dir)
        continuity_state = (
            f"{ui_language.tr('common.on')} ✅"
            if continuity["enabled"]
            else f"{ui_language.tr('common.off')} ⬜"
        )
        carryover = continuity.get("carryover_from") or ui_language.tr(
            "status.none"
        )
        await runtime._reply_text(
            update,
            f"{card_title('🧠', 'Memory controls')}\n\n"
            f"<b>{ui_language.tr('common.current')}</b> · Memory+ {continuity_state}\n"
            f"<b>{ui_language.tr('memory.today')}</b> · "
            f"<code>{continuity['today_chars']}</code> {ui_language.tr('status.chars')} · "
            f"{ui_language.tr('status.memory.open', count=continuity['open_items'])}\n"
            f"<b>{ui_language.tr('memory.carryover')}</b> · <code>{carryover}</code>\n"
            f"<b>{ui_language.tr('memory.context_injection')}</b> · {state}\n"
            f"<b>{ui_language.tr('status.session')}</b> · "
            f"<code>{session['session_id']}</code> · {ui_language.tr('status.generation')} "
            f"<code>{session['context_generation']}</code>\n"
            f"<b>{ui_language.tr('memory.stored')}</b> · "
            f"<code>{len(session_messages)}</code> {ui_language.tr('memory.session_messages')} · "
            f"<code>{memories}</code> {ui_language.tr('memory.promoted_memories')}\n"
            f"<b>{ui_language.tr('memory.bge_sync')}</b> · <code>{sync_state}</code>\n\n"
            f"{ui_language.tr('memory.effect')}\n\n"
            f"{ui_language.tr('memory.commands')}",
            parse_mode="HTML",
            reply_markup=memory_plus_keyboard(bool(continuity["enabled"])),
        )
    elif args in {"plus", "plus on", "continuity", "continuity on"}:
        from orchestrator.fresh_context import resume_automatic_context

        set_memory_plus_enabled(runtime.workspace_dir, True)
        resume_automatic_context(runtime)
        ensure_memory_plus_observer(runtime.workspace_dir)
        ensure_memory_plus_notepad(runtime.workspace_dir)
        runtime.reload_post_turn_observers()
        await runtime._reply_text(
            update,
            ui_language.tr(
                "memory.plus_on", mode=runtime.backend_manager.agent_mode
            ),
            parse_mode="Markdown",
        )
    elif args in {"plus off", "continuity off"}:
        set_memory_plus_enabled(runtime.workspace_dir, False)
        runtime.reload_post_turn_observers()
        await runtime._reply_text(
            update,
            ui_language.tr("memory.plus_off"),
        )
    elif args == "on":
        from orchestrator.fresh_context import resume_automatic_context

        set_memory_search_enabled(runtime.workspace_dir, True)
        resume_automatic_context(runtime)
        if assembler:
            assembler.turns_injection_enabled = True
            assembler.saved_memory_injection_enabled = True
        await runtime._reply_text(update, ui_language.tr("memory.injection_on"))
    elif args == "pause":
        set_memory_search_enabled(runtime.workspace_dir, False)
        if assembler:
            assembler.turns_injection_enabled = False
            assembler.saved_memory_injection_enabled = False
        await runtime._reply_text(
            update,
            ui_language.tr("memory.injection_paused"),
        )
    elif args in {"search on", "saved on"}:
        set_memory_search_enabled(runtime.workspace_dir, True)
        if assembler:
            assembler.saved_memory_injection_enabled = True
        await runtime._reply_text(update, ui_language.tr("memory.search_on"))
    elif args in {"search off", "saved off"}:
        set_memory_search_enabled(runtime.workspace_dir, False)
        if assembler:
            assembler.saved_memory_injection_enabled = False
        await runtime._reply_text(update, ui_language.tr("memory.search_off"))
    elif args in {"search status", "saved status"}:
        enabled = is_memory_search_enabled(runtime.workspace_dir)
        state = (
            f"{ui_language.tr('common.on')} ✅"
            if enabled
            else f"{ui_language.tr('common.off')} ⬜"
        )
        await runtime._reply_text(
            update, ui_language.tr("memory.search_status", state=state)
        )
    elif args == "wipe":
        if hasattr(runtime, "memory_store"):
            result = runtime.memory_store.clear_all()
            turns = result.get("deleted_turns", 0)
            mems = result.get("deleted_memories", 0)
            if assembler:
                turns_state = ui_language.tr(
                    "common.on"
                    if assembler.turns_injection_enabled
                    else "memory.state.paused"
                )
                saved_state = ui_language.tr(
                    "common.on"
                    if assembler.saved_memory_injection_enabled
                    else "memory.state.paused"
                )
                state = ui_language.tr(
                    "memory.injection_state",
                    turns=turns_state,
                    saved=saved_state,
                )
            else:
                state = ui_language.tr("common.unknown")
            await runtime._reply_text(
                update,
                ui_language.tr(
                    "memory.wiped", turns=turns, memories=mems, state=state
                ),
            )
        else:
            await runtime._reply_text(update, ui_language.tr("memory.store_unavailable"))
    elif args == "sync on":
        runtime._set_skill_state("memory_sync", True)
        agent = runtime.workspace_dir.name
        await runtime._reply_text(
            update,
            ui_language.tr("memory.sync_on", agent=agent),
        )
    elif args == "sync off":
        runtime._set_skill_state("memory_sync", False)
        agent = runtime.workspace_dir.name
        await runtime._reply_text(
            update,
            ui_language.tr("memory.sync_off", agent=agent),
        )
    else:
        await runtime._reply_text(
            update,
            ui_language.tr("memory.usage"),
            parse_mode="HTML",
        )


async def cmd_wipe(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    if runtime._backend_busy():
        await runtime._reply_text(
            update, ui_language.tr("workspace.blocked.busy.wipe")
        )
        return
    delayed = await runtime_pending.delayed_count(runtime)
    if delayed:
        await runtime._reply_text(
            update,
            ui_language.tr(
                "workspace.blocked.delayed.wipe", count=delayed
            ),
        )
        return

    args = [a.strip() for a in (context.args or []) if a.strip()]
    if not args or args[0].upper() != "CONFIRM":
        await runtime._reply_text(
            update,
            confirm_card(
                "⚠️",
                "Wipe workspace",
                target=f"<code>{runtime.name}</code>",
                consequence=ui_language.tr("workspace.wipe.effect"),
            )
            + "\n\n"
            + ui_language.tr("workspace.wipe.confirm"),
            parse_mode="HTML",
        )
        return

    keep_names = {"agent.md", "post_turn_observers.json"} | runtime._observer_workspace_keep_names()
    removed_files, removed_dirs = await _wipe_workspace(runtime, keep_names)
    _reinitialize_workspace_runtime(runtime)
    _reset_pending_context(runtime)

    if runtime.backend_manager.current_backend and getattr(runtime.backend_manager.current_backend.capabilities, "supports_sessions", False):
        with suppress(Exception):
            await runtime.backend_manager.current_backend.handle_new_session()

    await runtime._reply_text(
        update,
        ui_language.tr(
            "workspace.wipe.done",
            agent=runtime.name,
            dirs=removed_dirs,
            files=removed_files,
        ),
        parse_mode="HTML",
    )


async def cmd_reset(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    if runtime._backend_busy():
        await runtime._reply_text(
            update, ui_language.tr("workspace.blocked.busy.reset")
        )
        return
    delayed = await runtime_pending.delayed_count(runtime)
    if delayed:
        await runtime._reply_text(
            update,
            ui_language.tr(
                "workspace.blocked.delayed.reset", count=delayed
            ),
        )
        return

    args = [a.strip() for a in (context.args or []) if a.strip()]
    if not args or args[0].upper() != "CONFIRM":
        await runtime._reply_text(
            update,
            confirm_card(
                "⚠️",
                "Reset workspace",
                target=f"<code>{runtime.name}</code>",
                consequence=ui_language.tr("workspace.reset.effect"),
            )
            + "\n\n"
            + ui_language.tr("workspace.reset.confirm"),
            parse_mode="HTML",
        )
        return

    keep_names = {"agent.md", "sys_prompts.json", "post_turn_observers.json"} | runtime._observer_workspace_keep_names()
    preserved_state = _preserve_backend_state(runtime)
    removed_files, removed_dirs = await _wipe_workspace(runtime, keep_names)
    _reinitialize_workspace_runtime(runtime)
    _reset_pending_context(runtime)
    if preserved_state:
        with suppress(Exception):
            runtime.backend_manager._write_state_dict(dict(preserved_state))

    if runtime.backend_manager.current_backend and getattr(runtime.backend_manager.current_backend.capabilities, "supports_sessions", False):
        with suppress(Exception):
            await runtime.backend_manager.current_backend.handle_new_session()

    await runtime._reply_text(
        update,
        ui_language.tr(
            "workspace.reset.done",
            agent=runtime.name,
            dirs=removed_dirs,
            files=removed_files,
        ),
        parse_mode="HTML",
    )


async def cmd_clear(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return

    cleared = 0
    if runtime.media_dir.exists():
        for file_path in runtime.media_dir.iterdir():
            if file_path.is_file():
                try:
                    file_path.unlink()
                    cleared += 1
                except Exception:
                    pass

    if runtime.backend_manager.current_backend:
        await runtime.backend_manager.current_backend.handle_new_session()
    await runtime._reply_text(
        update, ui_language.tr("workspace.clear.done", count=cleared)
    )


async def _wipe_workspace(runtime: Any, keep_names: set[str]) -> tuple[int, int]:
    removed_files = 0
    removed_dirs = 0
    if runtime.backend_manager.current_backend:
        with suppress(Exception):
            await runtime.backend_manager.current_backend.shutdown()

    for child in list(runtime.workspace_dir.iterdir()):
        if child.name in keep_names:
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child)
                removed_dirs += 1
            else:
                child.unlink(missing_ok=True)
                removed_files += 1
        except Exception:
            pass
    return removed_files, removed_dirs


def _reinitialize_workspace_runtime(runtime: Any) -> None:
    runtime.workspace_dir.mkdir(parents=True, exist_ok=True)
    runtime.memory_dir = runtime.workspace_dir / "memory"
    runtime.backend_state_dir = runtime.workspace_dir / "backend_state"
    runtime.memory_dir.mkdir(parents=True, exist_ok=True)
    runtime.backend_state_dir.mkdir(parents=True, exist_ok=True)

    runtime.memory_index = MemoryIndex(runtime.workspace_dir / "memory_index.sqlite")
    runtime.handoff_builder = HandoffBuilder(runtime.workspace_dir)
    runtime.memory_store = BridgeMemoryStore(runtime.workspace_dir)
    runtime.context_assembler = BridgeContextAssembler(
        runtime.memory_store,
        runtime.config.system_md,
        active_skill_provider=runtime._get_active_skill_sections,
        sys_prompt_manager=runtime.sys_prompt_manager,
        global_sys_prompt_manager=getattr(runtime, "global_sys_prompt_manager", None),
        skill_catalog_provider=runtime._get_available_skill_catalogue,
        tool_catalog_provider=runtime._get_available_tool_catalogue,
    )
    apply_memory_search_preference(runtime.context_assembler, runtime.workspace_dir)
    runtime.reload_post_turn_observers()


def _reset_pending_context(runtime: Any) -> None:
    runtime._pending_auto_recall_context = None
    runtime._pending_session_primer = None
    runtime._clear_transfer_state()


def _preserve_backend_state(runtime: Any) -> dict[str, Any]:
    preserved_state: dict[str, Any] = {}
    try:
        state_snapshot = runtime.backend_manager.get_state_snapshot()
        for key in (
            "active_backend",
            "active_model",
            "agent_mode",
            "core",
            "wrapper",
            "wrapper_slots",
            "audit",
            "audit_criteria",
            "her_habit_meditation",
            HER_V2_CONFIGURATION_STATE_KEY,
        ):
            if key in state_snapshot:
                preserved_state[key] = state_snapshot[key]
    except Exception as exc:
        runtime.logger.warning(f"Reset could not preserve wrapper state: {exc}")
    return preserved_state
