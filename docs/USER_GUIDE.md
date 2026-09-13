# HASHI User Guide

[Install](INSTALL.md) · [Configuration](CONFIGURATION.md) ·
[Integrations](INTEGRATIONS.md) · [Troubleshooting](TROUBLESHOOTING.md)

This guide covers everyday operation of the current source candidate.
Installed releases can expose a different command set. Use the live /help
menu and terminal hashi help for the available commands and options.

## Start a conversation

After connecting an engine or model provider during onboarding, choose an
agent in the TUI and describe the task. Use /connect in the local TUI to
repair a connection without sending credentials through model chat.
Telegram is optional.

An agent has its own identity, memory, permissions, and workspace. A
conversation has its own session and execution history. Switching an engine
does not make all engines share one native conversation thread.

## Commands and task control

| Intent | Entry point | Effect |
|---|---|---|
| Inspect the agent | /status | Show the selected engine/model and current state |
| Discover commands | /help | Show the current registered command surface |
| Cancel work | /stop | Cancel the active request |
| Change direction | /steer | Interrupt and continue with added direction |
| Restore task focus | /focus | Refocus work on the original request |
| Withdraw queued work | /recall or /queue | Inspect or remove waiting requests |
| Send a message later | /delay | Persist a delayed message for the normal queue |
| Start a conversation | /new | Create/select a new HASHI conversation session |
| Replay a delivered result | /resend | Repeat output without new model work |
| Retry a request | /retry | Reset the execution context and rerun the last retryable prompt |

Menus provide exact syntax, scope, and confirmation where needed. See
[task control](https://github.com/Bazza1982/HASHI/blob/main/docs/FOCUS_RECALL_COMMANDS.md),
[delayed messages](https://github.com/Bazza1982/HASHI/blob/main/docs/DELAY_COMMAND.md),
and [recovery commands](https://github.com/Bazza1982/HASHI/blob/main/docs/RETRY_RESEND_COMMANDS.md)
for persistence and failure behavior.

## Projects and Workzones

Use /workzone to attach a project to the current conversation. Backends and
HASHI tools receive the exact enabled roots. Attaching several projects does
not implicitly authorize their common parent directory.

The agent's own workspace contains identity and continuity data; a project
Workzone is where the requested project work belongs. Available filesystem
actions also depend on engine and tool permissions.

## Engines, models, and working modes

/mode selects Fixed or Flex. Fixed is the default for session-capable engines
and preserves their native session continuity. /backend is available directly
in both modes. A successful selection saves Fixed for a session-capable target
and Flex for a stateless target; failure preserves the old selection and mode.

Within HER v2, /provider and /model configure model routing. Its durable
Engine Session remains bound to the HASHI conversation. Changing a HER model
provider is distinct from selecting another top-level engine.

For HER v2, /effort selects Direct, Strategic, or Planned execution.
For other engines, it controls supported model reasoning settings. The live
menus derive choices from configuration and capabilities; this guide does not
maintain a second model/effort catalogue.

See [working modes](https://github.com/Bazza1982/HASHI/blob/main/docs/FIXED_FLEX_WORKING_MODES.md)
and the [HER three-mode decision](https://github.com/Bazza1982/HASHI/blob/main/docs/HER_V2_THREE_MODE_DECISION.md).

## Memory and instructions

/memory controls memory injection. Memory+ is an independent optional
continuity layer; /notepad shows its current work card and archived pointers.
/handoff restores recent completed exchanges for explicit context recovery.

On HER v2, /fresh creates a durable context boundary and stops pre-boundary
history and automatic continuity sources from entering new prompts. It
preserves underlying records and files. Explicitly re-enable the desired
memory or Habit source when needed.

Use /sys for Agent-local instructions. Instance-global slots apply across
agents in that instance and show confirmation for broad changes. A running
request is not rewritten by changing a slot.

HER Habit–Meditation is optional, disabled by default, and owned by HER v2.
The /habit menu controls agent-local learned advice. It is separate from
ordinary skills and memory consolidation; it is not a guarantee that an agent
improves after every task.

See [PCM](https://github.com/Bazza1982/HASHI/blob/main/docs/HASHI_PCM_SYSTEM_DESIGN.md),
[Memory+](https://github.com/Bazza1982/HASHI/blob/main/docs/MEMORY_PLUS_V2.md),
and [Habit–Meditation](https://github.com/Bazza1982/HASHI/blob/main/docs/HER_HABIT_MEDITATION.md).

## Skills, jobs, and workflows

/skill browses the instruction packages available to the instance. Packages
use the Agent Skills layout with a SKILL.md entry. Tools and permissions
remain governed by HASHI and the chosen engine.

/jobs manages scheduled work, including prompts, skills, and deterministic
Function actions. /bg tracks long-running OS/process jobs with status, logs,
cancellation, and terminal notifications. Scheduled work is subject to
permissions and provider policy. Missed schedules may produce a recovery
choice; they should not be assumed to replay every occurrence automatically.

Nagare coordinates dependency-ordered workflows. Superloop provides a
long-running controller with taskboards, waits, issues, and closeout evidence.
HChat connects agents locally and across trusted Remote peers.

See the [skills guide](https://github.com/Bazza1982/HASHI/blob/main/skills/README.md),
[background jobs](https://github.com/Bazza1982/HASHI/blob/main/docs/HASHI_BACKGROUND_JOBS_DESIGN.md),
[Nagare reference](https://github.com/Bazza1982/HASHI/blob/main/docs/NAGARE_FLOW_SYSTEM.md),
and [Superloop contract](https://github.com/Bazza1982/HASHI/blob/main/docs/SUPERLOOP_FUNCTION_CONTRACT.md).

## Media, voice, and notifications

Telegram's /long … /end controls group text and attachments into one request.
Media remains ordered; native input and local inspection depend on the
selected model's capabilities and configured routes.

SafeVoice adds confirmation for transcribed voice commands. Native audio,
transcription, and spoken replies have separate capability requirements.
/voice configures speech; /say attempts to read the most recent confirmed
assistant reply on the current route.

/notify chooses normal, quiet, or silent Telegram delivery. Quiet retains
final results and important error/recovery notices while silencing interim
activity. /verbose, /think, and /commentary control different kinds of visible
progress; turning them off does not delete execution records.
/meter reports usage and available cost evidence. Missing prices are unknown,
not zero.

See [audio](https://github.com/Bazza1982/HASHI/blob/main/docs/HASHI_NATIVE_AUDIO_CHAT_DESIGN.md),
[notifications](https://github.com/Bazza1982/HASHI/blob/main/docs/TELEGRAM_NOTIFICATION_MODES.md),
and [metering](https://github.com/Bazza1982/HASHI/blob/main/docs/METER_COST_DISPLAY_PLAN.md).

## Terminal UI and instance management

hashi opens the selected instance's TUI. hashi tui --attach-only connects
without starting an instance. hashi status, hashi doctor, and hashi logs
provide inspection; hashi help shows detailed subcommand help.

In the TUI, /to selects an agent and /instance selects an eligible trusted
peer. Peer switching requires the Remote handshake and advertised TUI proxy
capability. The TUI is a reference client; it does not yet implement the full
Persistent Session API multi-session experience.

Presentation controls include /language, /theme, and /sidepanel. These affect
the interface rather than the model's identity or stored conversation state.
See [TUI instance switching](https://github.com/Bazza1982/HASHI/blob/main/docs/TUI_INSTANCE_SWITCHING.md).

## Apply changes at the right scope

/reboot adopts qualified Functions for the selected Agent scope. Shared
services have a separate replacement operation. Python, dependencies, and
protected Core changes require the declared runtime migration.

npm program updates and instance adoption are separate steps. See
[upgrade and data preservation](INSTALL.md#stop-remove-upgrade-and-uninstall-safety).
