from __future__ import annotations

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
        return (
            f"/usecomputer is ON via /sys {USECOMPUTER_SLOT}.\n"
            "The agent will treat desktop/GUI control as an available operating mode, not a forced one."
        )
    sys_prompt_manager.delete(USECOMPUTER_SLOT)
    return (
        f"/usecomputer is OFF. /sys {USECOMPUTER_SLOT} has been cleared."
    )


def get_usecomputer_status(sys_prompt_manager: SysPromptManager) -> str:
    slot = sys_prompt_manager._slot(USECOMPUTER_SLOT)
    active = bool(slot.get("active"))
    configured = slot.get("text") == USECOMPUTER_SYSTEM_PROMPT
    if active and configured:
        current = "<b>ON</b>"
        consequence = (
            "GUI-aware guidance is available for future requests, but direct tools remain preferred when more reliable."
        )
    elif slot.get("text"):
        current = "<b>CUSTOM</b>"
        consequence = (
            "The reserved system slot contains custom text rather than the managed computer-use prompt."
        )
    else:
        current = "<b>OFF</b>"
        consequence = "Managed GUI-aware guidance is inactive."
    return setting_card(
        "🖥️",
        "Computer use",
        current=current,
        facts=[
            f"<b>System slot</b> · <code>/sys {USECOMPUTER_SLOT}</code>",
            "<b>Alias</b> · <code>/usercomputer</code>",
        ],
        consequence=consequence,
        action=(
            "Use <code>/usecomputer on</code>, <code>/usecomputer off</code>, or "
            "<code>/usecomputer examples</code>. Send <code>/usecomputer &lt;task&gt;</code> to run a task."
        ),
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
        "<b>Current</b> · reference\n\n"
        "<code>/usecomputer status</code>\n"
        "<code>/usecomputer on</code>\n\n"
        "<code>/usecomputer Please code this material in NVivo; use mouse and keyboard if needed.</code>\n\n"
        "<code>/usecomputer Verify this Chrome extension on the real Windows desktop.</code>\n\n"
        "<code>/usecomputer Finish this form in the Linux virtual desktop when no reliable API exists.</code>\n\n"
        "Alias · <code>/usercomputer</code>"
    )
