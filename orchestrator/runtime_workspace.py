from __future__ import annotations

import shutil
from contextlib import suppress
from typing import Any

from orchestrator.bridge_memory import BridgeContextAssembler, BridgeMemoryStore
from orchestrator.habits import HabitStore
from orchestrator.handoff_builder import HandoffBuilder
from orchestrator.memory_index import MemoryIndex


async def cmd_memory(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    args = " ".join(context.args).strip().lower() if context.args else ""
    assembler = getattr(runtime, "context_assembler", None)

    if args in ("", "status"):
        if assembler:
            state = "ON ✅" if assembler.memory_injection_enabled else "PAUSED ⏸️"
        else:
            state = "unknown (assembler not ready)"
        stats = runtime.memory_store.get_stats() if hasattr(runtime, "memory_store") else {}
        turns = stats.get("turns", "?")
        memories = stats.get("memories", "?")
        sync_on = runtime._get_skill_state().get("memory_sync", False)
        sync_state = "ON 🔄" if sync_on else "OFF ⬜"
        await runtime._reply_text(
            update,
            f"Memory injection: {state}\n"
            f"Stored: {turns} turns, {memories} memories\n"
            f"BGE sync: {sync_state}\n\n"
            f"Commands: /memory on | pause | wipe | sync on | sync off",
        )
    elif args == "on":
        if assembler:
            assembler.memory_injection_enabled = True
        await runtime._reply_text(update, "✅ Memory injection ON. Long-term memories will be included in context.")
    elif args == "pause":
        if assembler:
            assembler.memory_injection_enabled = False
        await runtime._reply_text(
            update,
            "⏸️ Memory injection PAUSED. Memories are preserved but not injected into context.\n"
            "Use /memory on to resume.",
        )
    elif args == "wipe":
        if hasattr(runtime, "memory_store"):
            result = runtime.memory_store.clear_all()
            turns = result.get("deleted_turns", 0)
            mems = result.get("deleted_memories", 0)
            state = "ON ✅" if (assembler and assembler.memory_injection_enabled) else "PAUSED ⏸️"
            await runtime._reply_text(
                update,
                f"🗑️ Memory wiped: {turns} turns and {mems} memories deleted.\n"
                f"Database structure preserved. Injection is still {state}.",
            )
        else:
            await runtime._reply_text(update, "❌ Memory store not available.")
    elif args == "sync on":
        runtime._set_skill_state("memory_sync", True)
        agent = runtime.workspace_dir.name
        await runtime._reply_text(
            update,
            f"🔄 Memory sync ON for {agent}.\n"
            f"This agent's important memories will be queued for nightly BGE consolidation.\n"
            f"Use /memory sync off to opt out.",
        )
    elif args == "sync off":
        runtime._set_skill_state("memory_sync", False)
        agent = runtime.workspace_dir.name
        await runtime._reply_text(
            update,
            f"⬜ Memory sync OFF for {agent}.\n"
            f"This agent will not participate in BGE consolidation.\n"
            f"Local memories are unaffected. Use /memory sync on to re-enable.",
        )
    else:
        await runtime._reply_text(update, "Usage: /memory [on | pause | wipe | sync on | sync off | status]")


async def cmd_wipe(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    if runtime._backend_busy():
        await runtime._reply_text(update, "Wipe is blocked while a request is running or queued. Use /stop first.")
        return

    args = [a.strip() for a in (context.args or []) if a.strip()]
    if not args or args[0].upper() != "CONFIRM":
        await runtime._reply_text(
            update,
            "⚠️ /wipe will permanently delete this agent's persisted workspace state (memory, transcript, handoff, backend_state, etc.).\n"
            "Only agent instructions (agent.md / AGENT.md) will remain.\n\n"
            "To proceed: /wipe CONFIRM",
        )
        return

    keep_names = {"agent.md", "AGENT.md"}
    removed_files, removed_dirs = await _wipe_workspace(runtime, keep_names)
    _reinitialize_workspace_runtime(runtime)
    _reset_pending_context(runtime)

    if runtime.backend_manager.current_backend and getattr(runtime.backend_manager.current_backend.capabilities, "supports_sessions", False):
        with suppress(Exception):
            await runtime.backend_manager.current_backend.handle_new_session()

    await runtime._reply_text(
        update,
        f"✅ Wiped workspace for {runtime.name}. Removed {removed_dirs} dirs and {removed_files} files.\n"
        "Only agent instructions remain. Start fresh with /new.",
    )


async def cmd_reset(runtime: Any, update: Any, context: Any) -> None:
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    if runtime._backend_busy():
        await runtime._reply_text(update, "Reset is blocked while a request is running or queued. Use /stop first.")
        return

    args = [a.strip() for a in (context.args or []) if a.strip()]
    if not args or args[0].upper() != "CONFIRM":
        await runtime._reply_text(
            update,
            "⚠️ /reset will clear this agent's memory, transcripts, and session state.\n"
            "agent.md and /sys prompt slots will be preserved — the agent's identity stays intact.\n\n"
            "To proceed: /reset CONFIRM",
        )
        return

    keep_names = {"agent.md", "AGENT.md", "sys_prompts.json", "post_turn_observers.json", "workspace_commands.json"}
    keep_names.update(runtime._observer_workspace_keep_names())
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
        f"✅ Reset workspace for {runtime.name}. Removed {removed_dirs} dirs and {removed_files} files.\n"
        "Agent identity and /sys slots are intact. Start fresh with /new.",
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
    await runtime._reply_text(update, f"Cleared {cleared} media files and reset session state for current backend.")


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
    )
    runtime.reload_post_turn_observers()

    runtime.habit_store = HabitStore(
        runtime.workspace_dir,
        runtime.global_config.project_root,
        runtime.name,
        runtime._get_agent_class(),
    )


def _reset_pending_context(runtime: Any) -> None:
    runtime._pending_auto_recall_context = None
    runtime._pending_session_primer = None
    runtime._clear_transfer_state()


def _preserve_backend_state(runtime: Any) -> dict[str, Any]:
    preserved_state: dict[str, Any] = {}
    try:
        state_snapshot = runtime.backend_manager.get_state_snapshot()
        for key in ("active_backend", "active_model", "agent_mode", "core", "wrapper", "wrapper_slots", "audit", "audit_criteria"):
            if key in state_snapshot:
                preserved_state[key] = state_snapshot[key]
    except Exception as exc:
        runtime.logger.warning(f"Reset could not preserve wrapper state: {exc}")
    return preserved_state
