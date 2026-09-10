from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandGuide:
    """Frontend-neutral typed-input guidance for one slash command."""

    usage: str
    choices: tuple[str, ...] = ()
    choice_source: str | None = None
    example: str | None = None


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
    guide: CommandGuide | None = None


COMMAND_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("everyday", "⚡", "Everyday"),
    ("models", "🧠", "Models & modes"),
    ("session", "🎛️", "Session & display"),
    ("tools", "🛠️", "Tasks & tools"),
    ("execution", "🧭", "Execution control"),
)


COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec("help", "cmd_help", "Show help menu", "everyday"),
    CommandSpec(
        "start",
        "cmd_start",
        "Start another stopped agent",
        "everyday",
        guide=CommandGuide("/start <agent>", choice_source="agents"),
    ),
    CommandSpec(
        "status",
        "cmd_status",
        "View agent status",
        "everyday",
        guide=CommandGuide(
            "/status [full|all|more]",
            ("full", "all", "more"),
            example="/status full",
        ),
    ),
    CommandSpec(
        "version",
        "cmd_version",
        "View trusted running and source versions",
        "everyday",
        guide=CommandGuide(
            "/version [full|all|<instance>]",
            ("full", "all"),
            example="/version full",
        ),
    ),
    CommandSpec(
        "sys",
        "cmd_sys",
        "Manage local/global system prompts",
        "tools",
        sensitive=True,
        guide=CommandGuide(
            "/sys [global] <slot> [on|off|save|replace|delete]",
            ("global", "output"),
            example="/sys 1 on",
        ),
    ),
    CommandSpec(
        "habit",
        "cmd_habit",
        "View and manage HER habits",
        "tools",
        guide=CommandGuide(
            "/habit [list|show|create|edit|delete|run]",
            ("list", "show", "create", "edit", "delete", "run"),
            example="/habit list",
        ),
    ),
    CommandSpec(
        "dream",
        "cmd_dream",
        "Maintain HER habits on a schedule",
        "tools",
        guide=CommandGuide(
            "/dream [status|on|off|run|undo]",
            ("status", "on", "off", "run", "undo"),
            example="/dream status",
        ),
    ),
    CommandSpec("credit", "cmd_credit", "Check API credit/usage", "tools", sensitive=True),
    CommandSpec(
        "voice",
        "cmd_voice",
        "Configure voice replies",
        "session",
        guide=CommandGuide(
            "/voice <action> [value]",
            (
                "status",
                "on",
                "off",
                "menu",
                "preset",
                "voices",
                "use",
                "providers",
                "provider",
                "name",
                "rate",
                "mode",
                "target",
                "native-voice",
                "native-format",
                "content",
                "fallback",
                "retention",
                "transcript",
            ),
            example="/voice status",
        ),
    ),
    CommandSpec(
        "safevoice",
        "cmd_safevoice",
        "Toggle voice confirmation safety layer",
        "session",
        guide=CommandGuide(
            "/safevoice [on|off]", ("on", "off"), example="/safevoice on"
        ),
    ),
    CommandSpec("say", "cmd_say", "Read the last assistant reply as voice", "session"),
    CommandSpec(
        "loop",
        "cmd_loop",
        "Create/manage recurring loop tasks",
        "tools",
        guide=CommandGuide(
            "/loop [list|stop [id]|<task>]",
            ("list", "stop"),
            example="/loop list",
        ),
    ),
    CommandSpec(
        "superloop",
        "cmd_superloop",
        "Create/manage long-running superloops",
        "tools",
        guide=CommandGuide(
            "/superloop <action> [arguments]",
            (
                "quickstart",
                "wizard",
                "list",
                "record",
                "status",
                "validate",
                "pause",
                "resume",
                "closeout",
                "next",
                "task",
                "issue",
                "wait",
            ),
            example="/superloop list",
        ),
    ),
    CommandSpec(
        "nudge",
        "cmd_nudge",
        "Nudge this agent when idle until done",
        "tools",
        guide=CommandGuide(
            "/nudge <minutes> <exit-condition>", example="/nudge 15 task is complete"
        ),
    ),
    CommandSpec(
        "whisper",
        "cmd_whisper",
        "Choose the Whisper model size",
        "session",
        guide=CommandGuide(
            "/whisper [small|medium|large]",
            ("small", "medium", "large"),
            example="/whisper medium",
        ),
    ),
    CommandSpec(
        "active",
        "cmd_active",
        "Toggle proactive heartbeat",
        guide=CommandGuide(
            "/active [on [minutes]|off]",
            ("on", "off"),
            example="/active on 30",
        ),
    ),
    CommandSpec("fyi", "cmd_fyi", "Refresh bridge environment awareness", "session"),
    CommandSpec(
        "debug",
        "cmd_debug",
        "Run in strict debug mode",
        "tools",
        guide=CommandGuide("/debug <request>", example="/debug diagnose this failure"),
    ),
    CommandSpec(
        "skill",
        "cmd_skill",
        "Browse, run, and manage skills",
        "tools",
        guide=CommandGuide(
            "/skill [help|find|install|link|enable|disable|validate|delete|rescan|<skill>]",
            (
                "help",
                "find",
                "install",
                "link",
                "enable",
                "disable",
                "validate",
                "delete",
                "rescan",
            ),
            example="/skill help",
        ),
    ),
    CommandSpec(
        "exp",
        "cmd_exp",
        "Run a task with the EXP guidebook",
        "tools",
        guide=CommandGuide("/exp <request>", example="/exp review this result"),
    ),
    CommandSpec(
        "backend",
        "cmd_backend",
        "View or switch backend",
        "models",
        guide=CommandGuide("/backend [backend]", choice_source="backends"),
    ),
    CommandSpec(
        "handoff",
        "cmd_handoff",
        "Fresh session with recent continuity",
        "everyday",
        guide=CommandGuide("/handoff [request]"),
    ),
    CommandSpec(
        "ticket",
        "cmd_ticket",
        "Submit IT support ticket to Arale",
        "everyday",
        guide=CommandGuide(
            "/ticket <description>", example="/ticket TUI command menu is unclear"
        ),
    ),
    CommandSpec(
        "park",
        "cmd_park",
        "List or save parked topics",
        "everyday",
        guide=CommandGuide(
            "/park [chat [title]|delete <slot>]",
            ("chat", "delete"),
            example="/park chat follow up later",
        ),
    ),
    CommandSpec(
        "load",
        "cmd_load",
        "Restore a parked topic",
        "everyday",
        guide=CommandGuide("/load <slot>", example="/load 1"),
    ),
    CommandSpec(
        "transfer",
        "cmd_transfer",
        "Transfer this session to another agent",
        "everyday",
        guide=CommandGuide(
            "/transfer <agent> [instance]", choice_source="agents"
        ),
    ),
    CommandSpec(
        "fork",
        "cmd_fork",
        "Fork this session to another agent",
        "everyday",
        guide=CommandGuide("/fork <agent> [instance]", choice_source="agents"),
    ),
    CommandSpec(
        "cos",
        "cmd_cos",
        "Control Chief of Staff routing",
        "models",
        guide=CommandGuide("/cos [on|off]", ("on", "off"), example="/cos on"),
    ),
    CommandSpec(
        "provider",
        "cmd_provider",
        "Choose HER v2 provider or Hybrid",
        "models",
        guide=CommandGuide(
            "/provider [provider|hybrid]",
            choice_source="providers",
            example="/provider hybrid",
        ),
    ),
    CommandSpec(
        "model",
        "cmd_model",
        "View or change model configuration",
        "models",
        guide=CommandGuide("/model [model]", choice_source="models"),
    ),
    CommandSpec(
        "effort",
        "cmd_effort",
        "View or change effort",
        "models",
        guide=CommandGuide("/effort [level]", choice_source="efforts"),
    ),
    CommandSpec(
        "agents",
        "cmd_agents",
        "View and manage agents",
        "everyday",
        guide=CommandGuide(
            "/agents [add <id> <display-name> [telegram-token]]",
            ("add",),
            example="/agents add helper Helper",
        ),
    ),
    CommandSpec(
        "mode",
        "cmd_mode",
        "View or switch working mode",
        "models",
        guide=CommandGuide(
            "/mode [fixed|flex]",
            ("fixed", "flex"),
            example="/mode fixed",
        ),
    ),
    CommandSpec(
        "privacy",
        "cmd_privacy",
        "View or set privacy protection",
        "models",
        guide=CommandGuide(
            "/privacy [0|1|2|3|4|5]",
            ("0", "1", "2", "3", "4", "5"),
            example="/privacy 1",
        ),
    ),
    CommandSpec(
        "wrapper",
        "cmd_retired_agent_mode",
        "Retired working-mode compatibility notice",
        "models",
        menu_visible=False,
    ),
    CommandSpec(
        "audit",
        "cmd_retired_agent_mode",
        "Retired working-mode compatibility notice",
        "models",
        menu_visible=False,
    ),
    CommandSpec(
        "brain",
        "cmd_retired_agent_mode",
        "Retired working-mode compatibility notice",
        "models",
        menu_visible=False,
    ),
    CommandSpec(
        "core",
        "cmd_retired_agent_mode",
        "Retired working-mode compatibility notice",
        "models",
        menu_visible=False,
    ),
    CommandSpec(
        "wrap",
        "cmd_retired_agent_mode",
        "Retired working-mode compatibility notice",
        "models",
        menu_visible=False,
    ),
    CommandSpec(
        "workzone",
        "cmd_workzone",
        "View or set the working directory",
        "session",
        guide=CommandGuide(
            "/workzone [slot] [on|off|reset|reload|delete|label|replace|set|cancel]",
            ("all", "cancel"),
            example="/workzone all off",
        ),
    ),
    CommandSpec(
        "worzone",
        "cmd_workzone",
        "Alias for /workzone",
        "session",
        menu_visible=False,
        alias_of="workzone",
    ),
    CommandSpec(
        "new",
        "cmd_new",
        "Create and use a new Session",
        "session",
        guide=CommandGuide("/new [title]", example="/new research notes"),
    ),
    CommandSpec("fresh", "cmd_fresh", "Start a fresh context generation", "session"),
    CommandSpec(
        "sessions",
        "cmd_sessions",
        "List this Agent's Sessions",
        "session",
        guide=CommandGuide("/sessions [all]", ("all",), example="/sessions all"),
    ),
    CommandSpec(
        "use",
        "cmd_use",
        "Switch the current channel to a Session",
        "session",
        guide=CommandGuide("/use <session-number-or-id>", example="/use 1"),
    ),
    CommandSpec("current", "cmd_current", "Show the current Session", "session"),
    CommandSpec("archive", "cmd_archive", "Archive the current Session", "session"),
    CommandSpec(
        "promote",
        "cmd_promote",
        "Promote Session history to Agent memory",
        "session",
        sensitive=True,
        guide=CommandGuide(
            "/promote [status|now|all now|auto on|auto off|time HH:MM]",
            ("status", "now", "all", "auto", "time"),
            example="/promote status",
        ),
    ),
    CommandSpec(
        "memory",
        "cmd_memory",
        "Control memory and Memory+ continuity",
        "session",
        sensitive=True,
        guide=CommandGuide(
            "/memory [status|on|pause|plus|search|sync|raw|wipe]",
            ("status", "on", "pause", "plus", "search", "sync", "raw", "wipe"),
            example="/memory status",
        ),
    ),
    CommandSpec(
        "notepad",
        "cmd_notepad",
        "View compact continuity and history",
        "session",
        sensitive=True,
        guide=CommandGuide(
            "/notepad [today|carryover|history|find|edit|replace|compact|clear]",
            (
                "today",
                "carryover",
                "history",
                "find",
                "edit",
                "replace",
                "compact",
                "clear",
            ),
            example="/notepad today",
        ),
    ),
    CommandSpec("wipe", "cmd_wipe", "Wipe local session state", "execution", menu_visible=False),
    CommandSpec("reset", "cmd_reset", "Reset agent state", "execution", menu_visible=False),
    CommandSpec("clear", "cmd_clear", "Clear media/history", "execution"),
    CommandSpec("stop", "cmd_stop", "Stop execution", "execution"),
    CommandSpec(
        "steer",
        "cmd_steer",
        "Stop and continue with new direction",
        "execution",
        guide=CommandGuide(
            "/steer <new-direction>", example="/steer keep the current files"
        ),
    ),
    CommandSpec(
        "focus",
        "cmd_focus",
        "Narrow scope and continue the original task",
        "execution",
        guide=CommandGuide("/focus <scope>", example="/focus tests only"),
    ),
    CommandSpec(
        "recall",
        "cmd_recall",
        "Clear selected queued requests",
        "execution",
        guide=CommandGuide(
            "/recall [count|prompt|response]",
            ("prompt", "response"),
            example="/recall prompt",
        ),
    ),
    CommandSpec("terminate", "cmd_terminate", "Shut down this agent", "execution"),
    CommandSpec(
        "reboot",
        "cmd_reboot",
        "Switch Agent Function Workers",
        "execution",
        guide=CommandGuide(
            "/reboot [help|min|max|same|status|<agent>]",
            ("help", "min", "max", "same", "status"),
            example="/reboot status",
        ),
    ),
    CommandSpec(
        "resend",
        "cmd_resend",
        "Replay previous model or Bridge output",
        "execution",
        guide=CommandGuide(
            "/resend [response|prompt]",
            ("response", "prompt"),
            example="/resend response",
        ),
    ),
    CommandSpec("retry", "cmd_retry", "Reset context and rerun last prompt", "execution"),
    CommandSpec(
        "language",
        "cmd_language",
        "Choose interface language",
        "session",
        guide=CommandGuide(
            "/language [en|zh|default]",
            ("en", "zh", "default"),
            example="/language zh",
        ),
    ),
    CommandSpec(
        "terminal",
        "cmd_terminal",
        "Control terminal detail level",
        "session",
        guide=CommandGuide(
            "/terminal [quiet|activity|debug|raw]",
            ("quiet", "activity", "debug", "raw"),
            example="/terminal activity",
        ),
    ),
    CommandSpec(
        "verbose",
        "cmd_verbose",
        "Show technical execution telemetry",
        "session",
        guide=CommandGuide(
            "/verbose [on|off]", ("on", "off"), example="/verbose on"
        ),
    ),
    CommandSpec(
        "think",
        "cmd_think",
        "Show genuine provider reasoning",
        "session",
        guide=CommandGuide("/think [on|off]", ("on", "off"), example="/think on"),
    ),
    CommandSpec(
        "commentary",
        "cmd_commentary",
        "Show model-authored interim commentary",
        "session",
        guide=CommandGuide(
            "/commentary [on|off]",
            ("on", "off"),
            example="/commentary on",
        ),
    ),
    CommandSpec(
        "typing",
        "cmd_typing",
        "Control Telegram typing indicators",
        "session",
        guide=CommandGuide(
            "/typing [on|off|status]",
            ("on", "off", "status"),
            example="/typing on",
        ),
    ),
    CommandSpec(
        "meter",
        "cmd_meter",
        "Toggle per-turn cost tail",
        "session",
        guide=CommandGuide(
            "/meter [on|off|status|summary|session|provider|turn]",
            ("on", "off", "status", "summary", "session", "provider", "turn"),
            example="/meter summary",
        ),
    ),
    CommandSpec(
        "metre",
        "cmd_meter",
        "Alias for /meter",
        "session",
        menu_visible=False,
        alias_of="meter",
    ),
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
    CommandSpec(
        "cron",
        "cmd_cron",
        "Run or list cron jobs",
        "tools",
        guide=CommandGuide(
            "/cron [list|run <job-id>]",
            ("list", "run"),
            example="/cron list",
        ),
    ),
    CommandSpec(
        "heartbeat",
        "cmd_heartbeat",
        "Run or list heartbeat jobs",
        "tools",
        guide=CommandGuide(
            "/heartbeat [list|run <job-id>]",
            ("list", "run"),
            example="/heartbeat list",
        ),
    ),
    CommandSpec(
        "timeout",
        "cmd_timeout",
        "View or set request timeout",
        "tools",
        guide=CommandGuide(
            "/timeout [minutes|reset]", ("reset",), example="/timeout 30"
        ),
    ),
    CommandSpec(
        "hchat",
        "cmd_hchat",
        "Message another agent",
        "everyday",
        sensitive=True,
        guide=CommandGuide(
            "/hchat <agent|@group> <message>", choice_source="agents"
        ),
    ),
    CommandSpec("group", "cmd_group", "Manage agent groups", "everyday", menu_visible=False),
    CommandSpec("token", "cmd_token", "Manage API tokens", "tools", menu_visible=False, sensitive=True),
    CommandSpec("usage", "cmd_usage", "View detailed usage", "tools", menu_visible=False),
    CommandSpec("logo", "cmd_logo", "Play startup animation", "tools"),
    CommandSpec("move", "cmd_move", "Move an agent to another instance", "tools", menu_visible=False),
    CommandSpec("clone", "cmd_clone", "Clone an agent locally or to another instance", "tools", menu_visible=False),
    CommandSpec("wa_on", "cmd_wa_on", "Start WhatsApp transport", "tools"),
    CommandSpec("wa_off", "cmd_wa_off", "Stop WhatsApp transport", "tools"),
    CommandSpec(
        "wa_send",
        "cmd_wa_send",
        "Send a WhatsApp message",
        "tools",
        guide=CommandGuide(
            "/wa_send <+number> <message>", example="/wa_send +61400000000 Hello"
        ),
    ),
    CommandSpec(
        "usecomputer",
        "cmd_usecomputer",
        "Enable or run GUI-aware computer-use mode",
        "tools",
        guide=CommandGuide(
            "/usecomputer [on|off|<request>]",
            ("on", "off"),
            example="/usecomputer on",
        ),
    ),
    CommandSpec(
        "usercomputer",
        "cmd_usercomputer",
        "Alias for /usecomputer",
        "tools",
        menu_visible=False,
        alias_of="usecomputer",
    ),
    CommandSpec(
        "browser",
        "cmd_browser",
        "Run an internet task with a selected browser/search route",
        "tools",
        guide=CommandGuide(
            "/browser [route] <request>", example="/browser search current weather"
        ),
    ),
    CommandSpec(
        "long",
        "cmd_long",
        "Start multimodal batch (end with /end)",
        "tools",
        guide=CommandGuide("/long [request]", example="/long review these files"),
    ),
    CommandSpec("end", "cmd_end", "Submit collected /long input", "tools"),
    CommandSpec(
        "remote",
        "cmd_remote",
        "Control Hashi Remote",
        "tools",
        guide=CommandGuide(
            "/remote [status|list|on|off]",
            ("status", "list", "on", "off"),
            example="/remote list",
        ),
    ),
    CommandSpec("wol", "cmd_wol", "Send Wake-on-LAN magic packet [pc_name]", "tools", menu_visible=False),
)


COMMAND_SPEC_BY_NAME = {spec.name: spec for spec in COMMAND_SPECS}
SENSITIVE_COMMAND_NAMES = frozenset(
    spec.name for spec in COMMAND_SPECS if spec.sensitive
)
