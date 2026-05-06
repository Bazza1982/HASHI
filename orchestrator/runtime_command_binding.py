from __future__ import annotations

from dataclasses import dataclass

from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters

from orchestrator.command_registry import bind_runtime_commands


@dataclass(frozen=True)
class CommandBinding:
    name: str
    method_name: str


@dataclass(frozen=True)
class CallbackBinding:
    pattern: str
    method_name: str


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
    CommandBinding("long", "cmd_long"),
    CommandBinding("end", "cmd_end"),
    CommandBinding("remote", "cmd_remote"),
    CommandBinding("oll", "cmd_oll"),
    CommandBinding("wol", "cmd_wol"),
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
