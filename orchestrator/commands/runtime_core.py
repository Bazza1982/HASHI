from __future__ import annotations

from orchestrator import runtime_active
from orchestrator import runtime_agents
from orchestrator import runtime_controls
from orchestrator import runtime_backend
from orchestrator import runtime_bridge_handoff
from orchestrator import runtime_credit
from orchestrator import runtime_cos
from orchestrator import runtime_debug
from orchestrator import runtime_effort
from orchestrator import runtime_fyi
from orchestrator import runtime_group
from orchestrator import runtime_handoff
from orchestrator import runtime_hchat
from orchestrator import runtime_help
from orchestrator import runtime_jobs
from orchestrator import runtime_load
from orchestrator import runtime_long
from orchestrator import runtime_logo
from orchestrator import runtime_loop
from orchestrator import runtime_move
from orchestrator import runtime_mode
from orchestrator import runtime_model
from orchestrator import runtime_nudge
from orchestrator import runtime_park
from orchestrator import runtime_reboot
from orchestrator import runtime_remote
from orchestrator import runtime_retry
from orchestrator import runtime_safevoice
from orchestrator import runtime_say
from orchestrator import runtime_session
from orchestrator import runtime_skill
from orchestrator import runtime_status
from orchestrator import runtime_start
from orchestrator import runtime_stop
from orchestrator import runtime_sys
from orchestrator import runtime_ticket
from orchestrator import runtime_timeout
from orchestrator import runtime_token
from orchestrator import runtime_toggle
from orchestrator import runtime_usage
from orchestrator import runtime_usecomputer
from orchestrator import runtime_voice
from orchestrator import runtime_whatsapp
from orchestrator import runtime_whisper
from orchestrator import runtime_terminate
from orchestrator import runtime_workspace
from orchestrator.command_registry import RuntimeCallback
from orchestrator.command_registry import RuntimeCommand


COMMANDS = [
    RuntimeCommand(
        name="help",
        description="Show help menu",
        callback=runtime_help.cmd_help,
    ),
    RuntimeCommand(
        name="start",
        description="Start another stopped agent",
        callback=runtime_start.cmd_start,
    ),
    RuntimeCommand(
        name="active",
        description="Toggle proactive heartbeat",
        callback=runtime_active.cmd_active,
    ),
    RuntimeCommand(
        name="agents",
        description="List all agents with controls; add <id> <name> [token]",
        callback=runtime_agents.cmd_agents,
    ),
    RuntimeCommand(
        name="backend",
        description="Show backend buttons (+ means context)",
        callback=runtime_backend.cmd_backend,
    ),
    RuntimeCommand(
        name="credit",
        description="Check API credit/usage",
        callback=runtime_credit.cmd_credit,
    ),
    RuntimeCommand(
        name="cos",
        description="Chief of Staff decision routing (on/off)",
        callback=runtime_cos.cmd_cos,
    ),
    RuntimeCommand(
        name="debug",
        description="Run in strict debug mode",
        callback=runtime_debug.cmd_debug,
    ),
    RuntimeCommand(
        name="clear",
        description="Clear media/history",
        callback=runtime_workspace.cmd_clear,
    ),
    RuntimeCommand(
        name="effort",
        description="View or change effort",
        callback=runtime_effort.cmd_effort,
    ),
    RuntimeCommand(
        name="fresh",
        description="Start a clean API context",
        callback=runtime_session.cmd_fresh,
    ),
    RuntimeCommand(
        name="fork",
        description="Fork this session to another agent",
        callback=runtime_bridge_handoff.cmd_fork,
    ),
    RuntimeCommand(
        name="handoff",
        description="Fresh session with recent continuity",
        callback=runtime_handoff.cmd_handoff,
    ),
    RuntimeCommand(
        name="hchat",
        description="Send a message to another agent [agent] [message]",
        callback=runtime_hchat.cmd_hchat,
    ),
    RuntimeCommand(
        name="fyi",
        description="Refresh bridge environment awareness",
        callback=runtime_fyi.cmd_fyi,
    ),
    RuntimeCommand(
        name="group",
        description="Manage agent groups",
        callback=runtime_group.cmd_group,
    ),
    RuntimeCommand(
        name="jobs",
        description="Show cron and heartbeat jobs",
        callback=runtime_jobs.cmd_jobs,
    ),
    RuntimeCommand(
        name="load",
        description="Restore a parked topic",
        callback=runtime_load.cmd_load,
    ),
    RuntimeCommand(
        name="loop",
        description="Create/manage recurring loop tasks",
        callback=runtime_loop.cmd_loop,
    ),
    RuntimeCommand(
        name="long",
        description="Start multi-message input (end with /end)",
        callback=runtime_long.cmd_long,
    ),
    RuntimeCommand(
        name="logo",
        description="Play startup animation",
        callback=runtime_logo.cmd_logo,
    ),
    RuntimeCommand(
        name="think",
        description="Toggle thinking trace display [on|off]",
        callback=runtime_controls.cmd_think,
    ),
    RuntimeCommand(
        name="memory",
        description="Manage memory injection",
        callback=runtime_workspace.cmd_memory,
    ),
    RuntimeCommand(
        name="mode",
        description="Switch fixed/flex mode",
        callback=runtime_mode.cmd_mode,
    ),
    RuntimeCommand(
        name="model",
        description="View or change model",
        callback=runtime_model.cmd_model,
    ),
    RuntimeCommand(
        name="move",
        description="Move an agent between HASHI instances",
        callback=runtime_move.cmd_move,
    ),
    RuntimeCommand(
        name="new",
        description="Start a fresh session",
        callback=runtime_session.cmd_new,
    ),
    RuntimeCommand(
        name="nudge",
        description="Nudge this agent when idle until done",
        callback=runtime_nudge.cmd_nudge,
    ),
    RuntimeCommand(
        name="park",
        description="List or save parked topics",
        callback=runtime_park.cmd_park,
    ),
    RuntimeCommand(
        name="remote",
        description="Start/stop Hashi Remote [on|off|status|list]",
        callback=runtime_remote.cmd_remote,
    ),
    RuntimeCommand(
        name="reboot",
        description="Hot restart agents",
        callback=runtime_reboot.cmd_reboot,
    ),
    RuntimeCommand(
        name="safevoice",
        description="Toggle voice confirmation safety layer",
        callback=runtime_safevoice.cmd_safevoice,
    ),
    RuntimeCommand(
        name="say",
        description="Read the last assistant message aloud",
        callback=runtime_say.cmd_say,
    ),
    RuntimeCommand(
        name="skill",
        description="Browse and run skills",
        callback=runtime_skill.cmd_skill,
    ),
    RuntimeCommand(
        name="voice",
        description="Toggle native voice replies",
        callback=runtime_voice.cmd_voice,
    ),
    RuntimeCommand(
        name="whisper",
        description="Set Whisper model size [small|medium|large]",
        callback=runtime_whisper.cmd_whisper,
    ),
    RuntimeCommand(
        name="retry",
        description="Resend response or rerun prompt",
        callback=runtime_retry.cmd_retry,
    ),
    RuntimeCommand(
        name="reset",
        description="Reset workspace state but keep identity",
        callback=runtime_workspace.cmd_reset,
    ),
    RuntimeCommand(
        name="status",
        description="View agent status",
        callback=runtime_status.cmd_status,
    ),
    RuntimeCommand(
        name="stop",
        description="Stop execution",
        callback=runtime_stop.cmd_stop,
    ),
    RuntimeCommand(
        name="sys",
        description="Manage system prompt slots",
        callback=runtime_sys.cmd_sys,
    ),
    RuntimeCommand(
        name="ticket",
        description="Submit IT support ticket to Arale",
        callback=runtime_ticket.cmd_ticket,
    ),
    RuntimeCommand(
        name="end",
        description="Submit collected /long input",
        callback=runtime_long.cmd_end,
    ),
    RuntimeCommand(
        name="terminate",
        description="Shut down this agent",
        callback=runtime_terminate.cmd_terminate,
    ),
    RuntimeCommand(
        name="transfer",
        description="Transfer this session to another agent",
        callback=runtime_bridge_handoff.cmd_transfer,
    ),
    RuntimeCommand(
        name="timeout",
        description="View or set request timeout [minutes]",
        callback=runtime_timeout.cmd_timeout,
    ),
    RuntimeCommand(
        name="token",
        description="Show all-agent token summary by backend",
        callback=runtime_token.cmd_token,
    ),
    RuntimeCommand(
        name="usage",
        description="Show token usage summary [all]",
        callback=runtime_usage.cmd_usage,
    ),
    RuntimeCommand(
        name="wa_off",
        description="Stop WhatsApp transport",
        callback=runtime_whatsapp.cmd_wa_off,
    ),
    RuntimeCommand(
        name="wa_on",
        description="Start WhatsApp transport",
        callback=runtime_whatsapp.cmd_wa_on,
    ),
    RuntimeCommand(
        name="wa_send",
        description="Send a WhatsApp message",
        callback=runtime_whatsapp.cmd_wa_send,
    ),
    RuntimeCommand(
        name="usecomputer",
        description="Enable or run GUI-aware computer-use mode",
        callback=runtime_usecomputer.cmd_usecomputer,
    ),
    RuntimeCommand(
        name="usercomputer",
        description="Alias for /usecomputer",
        callback=runtime_usecomputer.cmd_usercomputer,
    ),
    RuntimeCommand(
        name="verbose",
        description="Toggle verbose long-task status [on|off]",
        callback=runtime_controls.cmd_verbose,
    ),
    RuntimeCommand(
        name="wipe",
        description="Wipe persisted workspace state",
        callback=runtime_workspace.cmd_wipe,
    ),
]


CALLBACKS: list[RuntimeCallback] = []
for module in (
    runtime_agents,
    runtime_group,
    runtime_model,
    runtime_move,
    runtime_safevoice,
    runtime_skill,
    runtime_start,
    runtime_toggle,
    runtime_voice,
):
    CALLBACKS.extend(getattr(module, "CALLBACKS", []))
