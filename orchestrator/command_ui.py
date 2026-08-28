from __future__ import annotations

from collections.abc import Iterable, Sequence
from html import escape
from typing import Any

from orchestrator.command_specs import COMMAND_GROUPS, COMMAND_SPECS
from orchestrator import ui_language

DIVIDER = "━━━━━━━━━━━━━━━━"
BACK_LABEL = "← Back"
REFRESH_LABEL = "↻ Refresh"


def back_label(*, locale: str | None = None) -> str:
    return ui_language.tr("common.back", locale=locale)


def refresh_label(*, locale: str | None = None) -> str:
    return ui_language.tr("common.refresh", locale=locale)


def card_title(icon: str, title: str, *, locale: str | None = None) -> str:
    """Return the standard Telegram/local command-card heading."""
    translated = ui_language.title(title, locale=locale)
    return f"{icon} <b>{escape(translated.upper())}</b>\n{DIVIDER}"


def selected_label(label: str, selected: bool) -> str:
    """Mark the active choice without changing the choice wording."""
    return f"✓ {label}" if selected else label


def status_label(enabled: bool, *, locale: str | None = None) -> str:
    key = "common.on" if enabled else "common.off"
    return ui_language.tr(key, locale=locale)


def setting_card(
    icon: str,
    title: str,
    *,
    current: str,
    facts: Iterable[str] = (),
    consequence: str | None = None,
    action: str | None = None,
    locale: str | None = None,
) -> str:
    """Render the common information order for an interactive setting card."""
    current_label = ui_language.tr("common.current", locale=locale)
    lines = [
        card_title(icon, title, locale=locale),
        "",
        f"<b>{escape(current_label)}</b> · {current}",
    ]
    fact_lines = [str(fact) for fact in facts if str(fact).strip()]
    if fact_lines:
        lines.extend(["", *fact_lines])
    if consequence:
        lines.extend(["", consequence])
    if action:
        lines.extend(["", action])
    return "\n".join(lines)


def confirm_card(
    icon: str,
    title: str,
    *,
    target: str,
    consequence: str,
    locale: str | None = None,
) -> str:
    """Render a dangerous-operation confirmation with an exact target."""
    target_label = ui_language.tr("common.target", locale=locale)
    return "\n".join(
        [
            card_title(icon, title, locale=locale),
            "",
            f"<b>{escape(target_label)}</b> · {target}",
            "",
            consequence,
            "",
            ui_language.tr("common.confirm_or_keep", locale=locale),
        ]
    )


HELP_GROUPS: tuple[tuple[str, str, str, frozenset[str]], ...] = tuple(
    (
        group,
        icon,
        heading,
        frozenset(spec.name for spec in COMMAND_SPECS if spec.group == group),
    )
    for group, icon, heading in COMMAND_GROUPS
)


def help_menu_text(
    *,
    agent_name: str,
    agent_type: str,
    commands: Sequence[Any],
    disabled: Iterable[str] = (),
    show_descriptions: bool = False,
    locale: str | None = None,
) -> str:
    """Render registered commands as a compact, grouped HTML help card."""
    command_map = {str(command.command): str(command.description) for command in commands}
    lines = [
        card_title("⚔️", "Hashi command centre", locale=locale),
        "",
        f"<b>{escape(agent_name)}</b> · <code>{escape(agent_type)}</code>",
        ui_language.tr(
            "help.commands_available",
            locale=locale,
            count=len(command_map),
        ),
    ]
    rendered: set[str] = set()
    for group, icon, heading, names in HELP_GROUPS:
        entries = [(name, command_map[name]) for name in command_map if name in names]
        if not entries:
            continue
        translated_heading = ui_language.tr(
            f"help.group.{group}",
            locale=locale,
        )
        if translated_heading == f"help.group.{group}":
            translated_heading = heading
        lines.extend(["", f"{icon} <b>{escape(translated_heading.upper())}</b>"])
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
        more_label = ui_language.tr("common.more", locale=locale)
        lines.extend(["", f"◇ <b>{escape(more_label.upper())}</b>"])
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
                f"🔒 <b>{escape(ui_language.tr('help.disabled', locale=locale).upper())}</b>",
                " ".join(f"<code>/{escape(name)}</code>" for name in disabled_names),
            ]
        )
    lines.extend(["", ui_language.tr("help.hint", locale=locale)])
    return "\n".join(lines)
