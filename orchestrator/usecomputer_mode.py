from __future__ import annotations

from orchestrator import ui_language
from orchestrator.bridge_memory import SysPromptManager
from orchestrator.command_ui import card_title, setting_card


USECOMPUTER_SLOT = "10"

USECOMPUTER_SYSTEM_PROMPT = """Computer-use mode is available for this task.

Use GUI automation only when it is actually the best tool for the job. Prefer direct APIs, shell commands, local file edits, or browser/dev tools when they are more reliable and efficient.

If GUI control is needed, operate carefully like a human:
- first inspect the environment and choose the right tool family (`windows_*` for Windows desktop control, `desktop_*` for WSL/X11 Linux desktop control)
- establish orientation before acting: detect the active window when possible, take screenshots, and verify focus
- work in small reversible steps; re-check the screen after important actions
- prefer native window focus/list/info helpers before blind clicking
- avoid assumptions about UI state, cursor position, selected text, or scroll position
- if an action fails or the UI looks different, stop, reassess from the latest screenshot, and recover explicitly
- when a task is high-risk (destructive actions, bulk edits, submissions), confirm the target state before committing

The user may mention GUI interaction only as an available option. Do not force computer use when a better non-GUI path exists."""


def ensure_usecomputer_slot(sys_prompt_manager: SysPromptManager) -> str:
    slot = sys_prompt_manager._slot(USECOMPUTER_SLOT)
    if slot.get("text") != USECOMPUTER_SYSTEM_PROMPT:
        sys_prompt_manager.replace(USECOMPUTER_SLOT, USECOMPUTER_SYSTEM_PROMPT)
    if not sys_prompt_manager._slot(USECOMPUTER_SLOT).get("active"):
        sys_prompt_manager.activate(USECOMPUTER_SLOT)
    return USECOMPUTER_SLOT


def set_usecomputer_mode(sys_prompt_manager: SysPromptManager, enabled: bool) -> str:
    if enabled:
        ensure_usecomputer_slot(sys_prompt_manager)
        return ui_language.tr(
            "computer.enabled",
            slot=USECOMPUTER_SLOT,
        )
    sys_prompt_manager.delete(USECOMPUTER_SLOT)
    return ui_language.tr("computer.disabled", slot=USECOMPUTER_SLOT)


def get_usecomputer_status(sys_prompt_manager: SysPromptManager) -> str:
    slot = sys_prompt_manager._slot(USECOMPUTER_SLOT)
    active = bool(slot.get("active"))
    configured = slot.get("text") == USECOMPUTER_SYSTEM_PROMPT
    if active and configured:
        current = f"<b>{ui_language.tr('common.on')}</b>"
        consequence = ui_language.tr("computer.effect.on")
    elif slot.get("text"):
        current = f"<b>{ui_language.tr('computer.state.custom')}</b>"
        consequence = ui_language.tr("computer.effect.custom")
    else:
        current = f"<b>{ui_language.tr('common.off')}</b>"
        consequence = ui_language.tr("computer.effect.off")
    return setting_card(
        "🖥️",
        "Computer use",
        current=current,
        facts=[
            f"<b>{ui_language.tr('computer.system_slot')}</b> · <code>/sys {USECOMPUTER_SLOT}</code>",
            f"<b>{ui_language.tr('computer.alias')}</b> · <code>/usercomputer</code>",
        ],
        consequence=consequence,
        action=ui_language.tr("computer.action"),
    )


def build_usecomputer_task_prompt(task: str) -> str:
    cleaned = (task or "").strip()
    return (
        "The user wants this handled in /usecomputer mode.\n"
        "Treat GUI/desktop control as available when needed, but do not force it if a better non-GUI method exists.\n\n"
        f"Task:\n{cleaned}"
    ).strip()


def get_usecomputer_examples_text() -> str:
    return (
        f"{card_title('🖥️', 'Computer use examples')}\n\n"
        f"<b>{ui_language.tr('common.current')}</b> · {ui_language.tr('computer.reference')}\n\n"
        "<code>/usecomputer status</code>\n"
        "<code>/usecomputer on</code>\n\n"
        f"{ui_language.tr('computer.example.nvivo')}\n\n"
        f"{ui_language.tr('computer.example.extension')}\n\n"
        f"{ui_language.tr('computer.example.form')}\n\n"
        f"{ui_language.tr('computer.alias')} · <code>/usercomputer</code>"
    )
