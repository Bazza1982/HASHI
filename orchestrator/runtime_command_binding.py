from __future__ import annotations

from dataclasses import dataclass

from telegram import BotCommand
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters

from orchestrator.command_registry import bind_runtime_commands, runtime_bot_commands
from orchestrator.private_wol import private_wol_available


@dataclass(frozen=True)
class CommandBinding:
    name: str
    method_name: str


@dataclass(frozen=True)
class CallbackBinding:
    pattern: str
    method_name: str


@dataclass(frozen=True)
class BotCommandBinding:
    name: str
    description: str


COMMAND_BINDINGS: tuple[CommandBinding, ...] = (
    CommandBinding("help", "cmd_help"),
    CommandBinding("start", "cmd_start"),
    CommandBinding("status", "cmd_status"),
    CommandBinding("sys", "cmd_sys"),
    CommandBinding("credit", "cmd_credit"),
    CommandBinding("voice", "cmd_voice"),
    CommandBinding("safevoice", "cmd_safevoice"),
    CommandBinding("say", "cmd_say"),
    CommandBinding("loop", "cmd_loop"),
    CommandBinding("whisper", "cmd_whisper"),
    CommandBinding("active", "cmd_active"),
    CommandBinding("fyi", "cmd_fyi"),
    CommandBinding("debug", "cmd_debug"),
    CommandBinding("skill", "cmd_skill"),
    CommandBinding("exp", "cmd_exp"),
    CommandBinding("backend", "cmd_backend"),
    CommandBinding("handoff", "cmd_handoff"),
    CommandBinding("ticket", "cmd_ticket"),
    CommandBinding("park", "cmd_park"),
    CommandBinding("load", "cmd_load"),
    CommandBinding("transfer", "cmd_transfer"),
    CommandBinding("fork", "cmd_fork"),
    CommandBinding("cos", "cmd_cos"),
    CommandBinding("model", "cmd_model"),
    CommandBinding("effort", "cmd_effort"),
    CommandBinding("agents", "cmd_agents"),
    CommandBinding("mode", "cmd_mode"),
    CommandBinding("wrapper", "cmd_wrapper"),
    CommandBinding("audit", "cmd_audit"),
    CommandBinding("core", "cmd_core"),
    CommandBinding("wrap", "cmd_wrap"),
    CommandBinding("workzone", "cmd_workzone"),
    CommandBinding("worzone", "cmd_workzone"),
    CommandBinding("new", "cmd_new"),
    CommandBinding("fresh", "cmd_fresh"),
    CommandBinding("memory", "cmd_memory"),
    CommandBinding("wipe", "cmd_wipe"),
    CommandBinding("reset", "cmd_reset"),
    CommandBinding("clear", "cmd_clear"),
    CommandBinding("stop", "cmd_stop"),
    CommandBinding("terminate", "cmd_terminate"),
    CommandBinding("reboot", "cmd_reboot"),
    CommandBinding("retry", "cmd_retry"),
    CommandBinding("verbose", "cmd_verbose"),
    CommandBinding("think", "cmd_think"),
    CommandBinding("jobs", "cmd_jobs"),
    CommandBinding("cron", "cmd_cron"),
    CommandBinding("heartbeat", "cmd_heartbeat"),
    CommandBinding("timeout", "cmd_timeout"),
    CommandBinding("hchat", "cmd_hchat"),
    CommandBinding("group", "cmd_group"),
    CommandBinding("token", "cmd_token"),
    CommandBinding("usage", "cmd_usage"),
    CommandBinding("logo", "cmd_logo"),
    CommandBinding("move", "cmd_move"),
    CommandBinding("wa_on", "cmd_wa_on"),
    CommandBinding("wa_off", "cmd_wa_off"),
    CommandBinding("wa_send", "cmd_wa_send"),
    CommandBinding("usecomputer", "cmd_usecomputer"),
    CommandBinding("usercomputer", "cmd_usercomputer"),
    CommandBinding("browser", "cmd_browser"),
    CommandBinding("long", "cmd_long"),
    CommandBinding("end", "cmd_end"),
    CommandBinding("remote", "cmd_remote"),
    CommandBinding("oll", "cmd_oll"),
    CommandBinding("wol", "cmd_wol"),
)


BOT_COMMAND_BINDINGS: tuple[BotCommandBinding, ...] = (
    BotCommandBinding("help", "Show help menu"),
    BotCommandBinding("start", "Start another stopped agent"),
    BotCommandBinding("agents", "List all agents with controls; add <id> <name> [token]"),
    BotCommandBinding("status", "View agent status"),
    BotCommandBinding("voice", "Toggle native voice replies"),
    BotCommandBinding("safevoice", "Toggle voice confirmation safety layer"),
    BotCommandBinding("whisper", "Set Whisper model size [small|medium|large]"),
    BotCommandBinding("active", "Toggle proactive heartbeat"),
    BotCommandBinding("fyi", "Refresh bridge environment awareness"),
    BotCommandBinding("debug", "Run in strict debug mode"),
    BotCommandBinding("skill", "Browse and run skills"),
    BotCommandBinding("exp", "Run a task with the EXP guidebook"),
    BotCommandBinding("backend", "Show backend buttons (+ means context)"),
    BotCommandBinding("handoff", "Fresh session with recent continuity"),
    BotCommandBinding("ticket", "Submit IT support ticket to Arale"),
    BotCommandBinding("park", "List or save parked topics"),
    BotCommandBinding("load", "Restore a parked topic"),
    BotCommandBinding("transfer", "Transfer this session to another agent"),
    BotCommandBinding("fork", "Fork this session to another agent"),
    BotCommandBinding("cos", "Chief of Staff decision routing (on/off)"),
    BotCommandBinding("long", "Start multi-message input (end with /end)"),
    BotCommandBinding("end", "Submit collected /long input"),
    BotCommandBinding("mode", "Switch fixed/flex/wrapper/audit mode"),
    BotCommandBinding("wrapper", "Configure wrapper persona slots"),
    BotCommandBinding("audit", "Configure audit model and criteria"),
    BotCommandBinding("core", "Configure managed core model"),
    BotCommandBinding("wrap", "Configure wrapper translator model"),
    BotCommandBinding("workzone", "Set temporary working directory [path|off]"),
    BotCommandBinding("model", "View or change model"),
    BotCommandBinding("effort", "View or change effort"),
    BotCommandBinding("new", "Start a fresh CLI session"),
    BotCommandBinding("fresh", "Start a clean API context"),
    BotCommandBinding("memory", "Control memory injection"),
    BotCommandBinding("clear", "Clear media/history"),
    BotCommandBinding("stop", "Stop execution"),
    BotCommandBinding("reboot", "Hot restart agents"),
    BotCommandBinding("terminate", "Shut down this agent"),
    BotCommandBinding("retry", "Resend response or rerun prompt"),
    BotCommandBinding("verbose", "Toggle verbose long-task status [on|off]"),
    BotCommandBinding("think", "Toggle thinking trace display [on|off]"),
    BotCommandBinding("loop", "Create/manage recurring loop tasks"),
    BotCommandBinding("jobs", "Show cron and heartbeat jobs"),
    BotCommandBinding("cron", "Run or list cron jobs"),
    BotCommandBinding("heartbeat", "Run or list heartbeat jobs"),
    BotCommandBinding("timeout", "View or set request timeout [minutes]"),
    BotCommandBinding("hchat", "Send a message to another agent [agent] [message]"),
    BotCommandBinding("logo", "Play startup animation"),
    BotCommandBinding("remote", "Start/stop Hashi Remote [on|off|status]"),
    BotCommandBinding("oll", "Start/stop OLL Browser Gateway [on|off|status]"),
    BotCommandBinding("wa_on", "Start WhatsApp transport"),
    BotCommandBinding("wa_off", "Stop WhatsApp transport"),
    BotCommandBinding("wa_send", "Send a WhatsApp message"),
    BotCommandBinding("usecomputer", "Enable or run GUI-aware computer-use mode"),
    BotCommandBinding("browser", "Run an internet task with a selected browser/search route"),
    BotCommandBinding("sys", "Manage system prompt slots"),
    BotCommandBinding("credit", "Check API credit/usage"),
)


CALLBACK_BINDINGS: tuple[CallbackBinding, ...] = (
    CallbackBinding(r"^(model|backend|bmodel|effort|backend_menu)", "callback_model"),
    CallbackBinding(r"^wcfg:", "callback_wrapper_config"),
    CallbackBinding(r"^acfg:", "callback_audit_config"),
    CallbackBinding(r"^voice:", "callback_voice"),
    CallbackBinding(r"^safevoice:", "callback_safevoice"),
    CallbackBinding(r"^startagent:", "callback_start_agent"),
    CallbackBinding(r"^agents:", "callback_agents"),
    CallbackBinding(r"^(skill|skilljob):", "callback_skill"),
    CallbackBinding(r"^tgl:", "callback_toggle"),
    CallbackBinding(r"^group:", "callback_group"),
    CallbackBinding(r"^move:", "callback_move"),
)


def bind_flexible_runtime_handlers(runtime) -> None:
    runtime.app.add_error_handler(runtime.handle_telegram_error)
    for binding in COMMAND_BINDINGS:
        callback = getattr(runtime, binding.method_name)
        runtime.app.add_handler(CommandHandler(binding.name, runtime._wrap_cmd(binding.name, callback)))
    for binding in CALLBACK_BINDINGS:
        runtime.app.add_handler(CallbackQueryHandler(getattr(runtime, binding.method_name), pattern=binding.pattern))

    bind_runtime_commands(runtime, wrap=True)

    runtime.app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), runtime.handle_message))
    runtime.app.add_handler(MessageHandler(filters.PHOTO, runtime.handle_photo))
    runtime.app.add_handler(MessageHandler(filters.VOICE, runtime.handle_voice))
    runtime.app.add_handler(MessageHandler(filters.AUDIO, runtime.handle_audio))
    runtime.app.add_handler(MessageHandler(filters.Document.ALL, runtime.handle_document))
    runtime.app.add_handler(MessageHandler(filters.VIDEO, runtime.handle_video))
    runtime.app.add_handler(MessageHandler(filters.Sticker.ALL, runtime.handle_sticker))


def get_flexible_bot_commands(runtime) -> list[BotCommand]:
    commands = [BotCommand(binding.name, binding.description) for binding in BOT_COMMAND_BINDINGS]
    if private_wol_available(runtime.global_config.project_root):
        commands.append(BotCommand("wol", "Send Wake-on-LAN magic packet [pc_name]"))
    return commands + runtime_bot_commands()
