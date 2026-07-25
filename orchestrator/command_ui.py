from __future__ import annotations

from collections.abc import Iterable, Sequence
from html import escape
from typing import Any


DIVIDER = "━━━━━━━━━━━━━━━━"
BACK_LABEL = "← Back"
REFRESH_LABEL = "↻ Refresh"


def card_title(icon: str, title: str) -> str:
    """Return the standard Telegram/local command-card heading."""
    return f"{icon} <b>{escape(title.upper())}</b>\n{DIVIDER}"


def selected_label(label: str, selected: bool) -> str:
    """Mark the active choice without changing the choice wording."""
    return f"✓ {label}" if selected else label


def status_label(enabled: bool) -> str:
    return "ON" if enabled else "OFF"


def setting_card(
    icon: str,
    title: str,
    *,
    current: str,
    facts: Iterable[str] = (),
    consequence: str | None = None,
    action: str | None = None,
) -> str:
    """Render the common information order for an interactive setting card."""
    lines = [card_title(icon, title), "", f"<b>Current</b> · {current}"]
    fact_lines = [str(fact) for fact in facts if str(fact).strip()]
    if fact_lines:
        lines.extend(["", *fact_lines])
    if consequence:
        lines.extend(["", consequence])
    if action:
        lines.extend(["", action])
    return "\n".join(lines)


def confirm_card(icon: str, title: str, *, target: str, consequence: str) -> str:
    """Render a dangerous-operation confirmation with an exact target."""
    return "\n".join(
        [
            card_title(icon, title),
            "",
            f"<b>Target</b> · {target}",
            "",
            consequence,
            "",
            "Confirm below, or keep the current state.",
        ]
    )


HELP_GROUPS: tuple[tuple[str, str, frozenset[str]], ...] = (
    (
        "⚡",
        "Everyday",
        frozenset(
            {
                "help",
                "status",
                "start",
                "agents",
                "hchat",
                "group",
                "ticket",
                "park",
                "load",
                "handoff",
                "transfer",
                "fork",
            }
        ),
    ),
    (
        "🧠",
        "Models & modes",
        frozenset(
            {
                "backend",
                "model",
                "effort",
                "mode",
                "privacy",
                "wrapper",
                "audit",
                "brain",
                "core",
                "wrap",
                "cos",
            }
        ),
    ),
    (
        "🎛️",
        "Session & display",
        frozenset(
            {
                "new",
                "fresh",
                "memory",
                "notepad",
                "workzone",
                "verbose",
                "think",
                "stream",
                "preview",
                "voice",
                "safevoice",
                "say",
                "whisper",
                "fyi",
            }
        ),
    ),
    (
        "🛠️",
        "Tasks & tools",
        frozenset(
            {
                "skill",
                "exp",
                "debug",
                "loop",
                "superloop",
                "nudge",
                "jobs",
                "cron",
                "heartbeat",
                "timeout",
                "browser",
                "usecomputer",
                "wa_on",
                "wa_off",
                "wa_send",
                "remote",
                "wol",
                "credit",
                "sys",
                "token",
                "usage",
                "logo",
                "move",
                "long",
                "end",
            }
        ),
    ),
    (
        "🧭",
        "Execution control",
        frozenset(
            {
                "stop",
                "steer",
                "focus",
                "recall",
                "retry",
                "clear",
                "wipe",
                "reset",
                "reboot",
                "terminate",
            }
        ),
    ),
)


def help_menu_text(
    *,
    agent_name: str,
    agent_type: str,
    commands: Sequence[Any],
    disabled: Iterable[str] = (),
    show_descriptions: bool = False,
) -> str:
    """Render registered commands as a compact, grouped HTML help card."""
    command_map = {str(command.command): str(command.description) for command in commands}
    lines = [
        card_title("⚔️", "Hashi command centre"),
        "",
        f"<b>{escape(agent_name)}</b> · <code>{escape(agent_type)}</code>",
        f"{len(command_map)} commands available",
    ]
    rendered: set[str] = set()
    for icon, heading, names in HELP_GROUPS:
        entries = [(name, command_map[name]) for name in command_map if name in names]
        if not entries:
            continue
        lines.extend(["", f"{icon} <b>{escape(heading.upper())}</b>"])
        if show_descriptions:
            lines.extend(
                f"<code>/{escape(name)}</code>  {escape(description)}"
                for name, description in entries
            )
        else:
            lines.append("  ".join(f"<code>/{escape(name)}</code>" for name, _ in entries))
        for name, _description in entries:
            rendered.add(name)

    remaining = [(name, description) for name, description in command_map.items() if name not in rendered]
    if remaining:
        lines.extend(["", "◇ <b>MORE</b>"])
        if show_descriptions:
            lines.extend(
                f"<code>/{escape(name)}</code>  {escape(description)}"
                for name, description in remaining
            )
        else:
            lines.append("  ".join(f"<code>/{escape(name)}</code>" for name, _ in remaining))

    disabled_names = sorted({str(name) for name in disabled})
    if disabled_names:
        lines.extend(
            [
                "",
                "🔒 <b>DISABLED FOR THIS AGENT</b>",
                " ".join(f"<code>/{escape(name)}</code>" for name in disabled_names),
            ]
        )
    lines.extend(["", "Open Telegram’s command button for a short description of each command."])
    return "\n".join(lines)
