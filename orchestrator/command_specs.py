from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandSpec:
    """Canonical metadata for one built-in slash command."""

    name: str
    method_name: str
    description: str
    group: str | None = None
    menu_visible: bool = True
    sensitive: bool = False
    alias_of: str | None = None


COMMAND_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("everyday", "⚡", "Everyday"),
    ("models", "🧠", "Models & modes"),
    ("session", "🎛️", "Session & display"),
    ("tools", "🛠️", "Tasks & tools"),
    ("execution", "🧭", "Execution control"),
)


COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec("help", "cmd_help", "Show help menu", "everyday"),
    CommandSpec("start", "cmd_start", "Start another stopped agent", "everyday"),
    CommandSpec("status", "cmd_status", "View agent status", "everyday"),
    CommandSpec("sys", "cmd_sys", "Manage local/global system prompts", "tools", sensitive=True),
    CommandSpec("habit", "cmd_habit", "View and manage HER habits", "tools"),
    CommandSpec("dream", "cmd_dream", "Maintain HER habits on a schedule", "tools"),
    CommandSpec("credit", "cmd_credit", "Check API credit/usage", "tools", sensitive=True),
    CommandSpec("voice", "cmd_voice", "Toggle native voice replies", "session"),
    CommandSpec("safevoice", "cmd_safevoice", "Toggle voice confirmation safety layer", "session"),
    CommandSpec("say", "cmd_say", "Read the last assistant reply as voice", "session"),
    CommandSpec("loop", "cmd_loop", "Create/manage recurring loop tasks", "tools"),
    CommandSpec("superloop", "cmd_superloop", "Create/manage long-running superloops", "tools"),
    CommandSpec("nudge", "cmd_nudge", "Nudge this agent when idle until done", "tools"),
    CommandSpec("whisper", "cmd_whisper", "Choose the Whisper model size", "session"),
    CommandSpec("active", "cmd_active", "Toggle proactive heartbeat"),
    CommandSpec("fyi", "cmd_fyi", "Refresh bridge environment awareness", "session"),
    CommandSpec("debug", "cmd_debug", "Run in strict debug mode", "tools"),
    CommandSpec("skill", "cmd_skill", "Browse, run, and manage skills", "tools"),
    CommandSpec("exp", "cmd_exp", "Run a task with the EXP guidebook", "tools"),
    CommandSpec("backend", "cmd_backend", "View or switch model backend", "models"),
    CommandSpec("handoff", "cmd_handoff", "Fresh session with recent continuity", "everyday"),
    CommandSpec("ticket", "cmd_ticket", "Submit IT support ticket to Arale", "everyday"),
    CommandSpec("park", "cmd_park", "List or save parked topics", "everyday"),
    CommandSpec("load", "cmd_load", "Restore a parked topic", "everyday"),
    CommandSpec("transfer", "cmd_transfer", "Transfer this session to another agent", "everyday"),
    CommandSpec("fork", "cmd_fork", "Fork this session to another agent", "everyday"),
    CommandSpec("cos", "cmd_cos", "Control Chief of Staff routing", "models"),
    CommandSpec("provider", "cmd_provider", "Choose HER model provider", "models"),
    CommandSpec("model", "cmd_model", "View or change model", "models"),
    CommandSpec("effort", "cmd_effort", "View or change effort", "models"),
    CommandSpec("agents", "cmd_agents", "View and manage agents", "everyday"),
    CommandSpec("mode", "cmd_mode", "View or switch working mode", "models"),
    CommandSpec("privacy", "cmd_privacy", "View or set privacy protection", "models"),
    CommandSpec("wrapper", "cmd_wrapper", "Configure wrapper persona slots", "models"),
    CommandSpec("audit", "cmd_audit", "Configure audit model and criteria", "models"),
    CommandSpec("brain", "cmd_brain", "Configure dual-brain models and prompts", "models"),
    CommandSpec("core", "cmd_core", "Configure managed core model", "models"),
    CommandSpec("wrap", "cmd_wrap", "Configure wrapper translator model", "models"),
    CommandSpec("workzone", "cmd_workzone", "View or set the working directory", "session"),
    CommandSpec(
        "worzone",
        "cmd_workzone",
        "Alias for /workzone",
        "session",
        menu_visible=False,
        alias_of="workzone",
    ),
    CommandSpec("new", "cmd_new", "Start a fresh CLI session", "session"),
    CommandSpec("fresh", "cmd_fresh", "Start a clean API context", "session"),
    CommandSpec("memory", "cmd_memory", "Control memory and Memory+ continuity", "session", sensitive=True),
    CommandSpec("notepad", "cmd_notepad", "View compact continuity and history", "session", sensitive=True),
    CommandSpec("wipe", "cmd_wipe", "Wipe local session state", "execution", menu_visible=False),
    CommandSpec("reset", "cmd_reset", "Reset agent state", "execution", menu_visible=False),
    CommandSpec("clear", "cmd_clear", "Clear media/history", "execution"),
    CommandSpec("stop", "cmd_stop", "Stop execution", "execution"),
    CommandSpec("steer", "cmd_steer", "Stop and continue with new direction", "execution"),
    CommandSpec("focus", "cmd_focus", "Narrow scope and continue the original task", "execution"),
    CommandSpec("recall", "cmd_recall", "Clear selected queued requests", "execution"),
    CommandSpec("terminate", "cmd_terminate", "Shut down this agent", "execution"),
    CommandSpec("reboot", "cmd_reboot", "Hot restart agents", "execution"),
    CommandSpec("resend", "cmd_resend", "Replay previous model or Bridge output", "execution"),
    CommandSpec("retry", "cmd_retry", "Reset context and rerun last prompt", "execution"),
    CommandSpec("verbose", "cmd_verbose", "Show technical execution telemetry", "session"),
    CommandSpec("think", "cmd_think", "Show commentary and provider reasoning", "session"),
    CommandSpec("commentary", "cmd_commentary", "Show HER Persona interim reports", "session"),
    CommandSpec("typing", "cmd_typing", "Control Telegram typing indicators", "session"),
    CommandSpec(
        "stream",
        "cmd_stream",
        "Moved to /typing, /verbose and /think",
        "session",
        menu_visible=False,
    ),
    CommandSpec(
        "preview",
        "cmd_preview",
        "Live answer preview has been retired",
        "session",
        menu_visible=False,
    ),
    CommandSpec("jobs", "cmd_jobs", "Show cron and heartbeat jobs", "tools"),
    CommandSpec("cron", "cmd_cron", "Run or list cron jobs", "tools"),
    CommandSpec("heartbeat", "cmd_heartbeat", "Run or list heartbeat jobs", "tools"),
    CommandSpec("timeout", "cmd_timeout", "View or set request timeout", "tools"),
    CommandSpec("hchat", "cmd_hchat", "Message another agent", "everyday", sensitive=True),
    CommandSpec("group", "cmd_group", "Manage agent groups", "everyday", menu_visible=False),
    CommandSpec("token", "cmd_token", "Manage API tokens", "tools", menu_visible=False, sensitive=True),
    CommandSpec("usage", "cmd_usage", "View detailed usage", "tools", menu_visible=False),
    CommandSpec("logo", "cmd_logo", "Play startup animation", "tools"),
    CommandSpec("move", "cmd_move", "Move an agent to another instance", "tools", menu_visible=False),
    CommandSpec("wa_on", "cmd_wa_on", "Start WhatsApp transport", "tools"),
    CommandSpec("wa_off", "cmd_wa_off", "Stop WhatsApp transport", "tools"),
    CommandSpec("wa_send", "cmd_wa_send", "Send a WhatsApp message", "tools"),
    CommandSpec("usecomputer", "cmd_usecomputer", "Enable or run GUI-aware computer-use mode", "tools"),
    CommandSpec(
        "usercomputer",
        "cmd_usercomputer",
        "Alias for /usecomputer",
        "tools",
        menu_visible=False,
        alias_of="usecomputer",
    ),
    CommandSpec("browser", "cmd_browser", "Run an internet task with a selected browser/search route", "tools"),
    CommandSpec("long", "cmd_long", "Start multimodal batch (end with /end)", "tools"),
    CommandSpec("end", "cmd_end", "Submit collected /long input", "tools"),
    CommandSpec("remote", "cmd_remote", "Control Hashi Remote", "tools"),
    CommandSpec("wol", "cmd_wol", "Send Wake-on-LAN magic packet [pc_name]", "tools", menu_visible=False),
)


COMMAND_SPEC_BY_NAME = {spec.name: spec for spec in COMMAND_SPECS}
SENSITIVE_COMMAND_NAMES = frozenset(
    spec.name for spec in COMMAND_SPECS if spec.sensitive
)
