from __future__ import annotations

from orchestrator.bridge_memory import SysPromptManager


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
        return (
            f"/usecomputer is ON via /sys {USECOMPUTER_SLOT}.\n"
            "GUI-aware operating guidance is active for future requests."
        )
    if slot.get("text"):
        return (
            f"/usecomputer is not fully active.\n"
            f"/sys {USECOMPUTER_SLOT} contains custom text but not the managed /usecomputer prompt."
        )
    return (
        f"/usecomputer is OFF.\n"
        f"/sys {USECOMPUTER_SLOT} is empty."
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
        "Examples:\n"
        "/usecomputer status\n"
        "/usecomputer on\n"
        "/usecomputer Please do some qualitative coding for me in NVivo here. It has no API, so use mouse and keyboard if needed.\n"
        "/usecomputer Please use the desktop/browser tools to verify this Chrome extension on the real Windows desktop.\n"
        "/usecomputer Please finish this form submission in the Linux virtual desktop if there is no reliable API path.\n"
        "\n"
        "Alias: /usercomputer also works."
    )
