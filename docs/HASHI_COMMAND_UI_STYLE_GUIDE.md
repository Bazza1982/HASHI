# HASHI Command UI Style Guide

This is the display contract for HASHI slash commands in Telegram and local
text surfaces. It standardizes presentation, not command behavior.

## 1. Information order

Interactive command menus use this order:

1. one icon plus a short uppercase title;
2. a single divider;
3. the current state or selected value;
4. the few facts needed to make a decision;
5. a short risk, limitation, or consequence when relevant;
6. the available action;
7. inline buttons.

Do not begin with usage syntax when the command can show a useful status card.
Put advanced text syntax last, or show it only after invalid input.

## 2. Text rules

- Telegram cards use HTML, with user/configuration values escaped.
- Use `<b>` for headings and the primary state, `<code>` for commands, paths,
  identifiers, models, endpoints, and literal values.
- Keep one fact per line. Use blank lines between sections.
- Prefer plain labels such as `Backend`, `Model`, `Status`, and `Privacy`.
- Use `ON`, `OFF`, `READY`, `BLOCKED`, or a short plain-language state.
- Explain consequences in one or two sentences. Avoid implementation detail.
- Success, warning, error, and information notices start with `✅`, `⚠️`, `❌`,
  and `ℹ️` respectively.
- Local/plain-text surfaces keep the same order and wording, omitting HTML tags.

## 3. Button rules

- Mark the active choice with `✓`, for example `✓ Flex`.
- Use `← Back` for navigation and `↻ Refresh` for a non-mutating reload.
- Use verbs for actions: `Turn on`, `Run now`, `Transfer`, `Delete`.
- Use sentence case. Do not mix `ON`, `Turn On`, `✅ ON`, and `Voice ON`.
- Put mutually exclusive choices on one row when they fit.
- Put destructive confirmation on its own row. The safe escape action follows
  as `← Keep current …` or `← Cancel`.
- Locked or unavailable choices start with `🔒`.
- Keep callback data stable when changing display text.

## 4. Command help and registration

- `/help` is generated from the same registered command metadata used by the
  Telegram command picker.
- Commands are grouped by user intent, not implementation module.
- Telegram picker descriptions are short action phrases, without a trailing
  period and without long parameter grammar.
- Detailed syntax belongs in the command response, not the picker description.
- A command hidden or disabled by policy is shown separately and never presented
  as available.
- Machine-specific commands live in `~/.hashi/private_commands/*.py`; do not add
  their handlers or metadata to the public static binding tables. Private
  modules still follow this display contract when they render menus or notices.

## 5. Required menu content

Every settings menu shows:

- the setting name;
- the effective current value;
- runtime/backend scope when that affects the result;
- whether a change is immediate, persistent, or needs a reboot;
- unavailable choices and why they are unavailable;
- a safe way back or to refresh when the menu has sub-pages.

When a command expresses a clear intent but the current mode blocks the action,
offer a concise confirmation that can satisfy the intent instead of ending with
instructions to run another command. The confirmation must state the current
mode, the effect of leaving it, and a safe keep-current action. After
confirmation, continue directly to the requested menu.

Backend and model selection are one continuous configuration flow. After a
backend and model are saved, models with selectable effort levels show an
optional effort step. If the user makes no effort selection, the current or
model-default effort remains active. Models without selectable effort skip that
step and show `n/a` in the final configuration summary.

Memory+ is a continuity setting, not an execution mode. `/mode` shows its
independent ON/OFF summary and `/memory plus on|off` controls it without
changing mode, backend, or stored files. `/notepad` separates Today, Carryover,
History, and Find views; default cards never mix archived prompts into the
current work card. The status card describes open-item and carryover counts as
background, never as automatically queued work.

The main `/status` card always shows the active backend and model. For HER v2 it
shows **HER execution mode** with the descriptive name and canonical value,
such as `Assured (max)`. Other backends continue to show **Effort**. Use `n/a`
when the active non-HER model does not support a selectable effort level.

Every dangerous operation shows:

- the exact target;
- the consequence;
- an explicit confirmation button;
- a cancel/keep-current button.

## 6. Length and accessibility

- A normal status card should fit on one phone screen where practical.
- Avoid decorative emoji on every line; emoji identify sections and states.
- Never rely on color or emoji alone to communicate state.
- Button labels must remain understandable when read without the message body.
- Prefer two columns only for short peer choices; long model names use one
  column.

## 7. Shared implementation

Common headings and navigation labels live in
`orchestrator/command_ui.py`. New menus should use these helpers rather than
inventing new dividers, selected markers, Back labels, or Refresh labels.

Tests should verify information order, HTML escaping, active-choice markers,
navigation labels, and that `/help` is derived from registered command metadata.

## 8. Migration coverage

The shared contract applies to every command surface that opens a status card,
settings panel, picker, multi-step flow, or dangerous-operation confirmation.
Current coverage includes:

- help, status, backend, model, effort, mode, privacy, API, wrapper, audit, and
  dual-brain configuration;
- stream, verbose, think, preview, voice, safe voice, Whisper, active
  continuation, memory, notepad, and computer-use controls;
- agents, start, groups, skills, jobs, cron, heartbeat, nudge, loop,
  superloop, timeout, browser routes, request queue, and background jobs;
- reboot, hard restart, retry, move, workspace reset, workspace wipe, and other
  destructive confirmation cards.

All supported Flex Agent execution modes use the same shared headings,
selection marker, navigation labels, and information order wherever they expose
the same menu. Callback data and command behavior remain stable during
presentation-only migrations.

Private commands are outside this migration inventory because they are installed
per machine. HASHI2's local `/oll` command follows the same card structure but
is not part of the public repository or public static command registry.
