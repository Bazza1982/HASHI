from __future__ import annotations

from types import SimpleNamespace

from orchestrator.command_ui import (
    BACK_LABEL,
    REFRESH_LABEL,
    card_title,
    confirm_card,
    help_menu_text,
    selected_label,
    setting_card,
)


def test_shared_labels_and_title_follow_command_ui_contract() -> None:
    assert BACK_LABEL == "← Back"
    assert REFRESH_LABEL == "↻ Refresh"
    assert selected_label("Flex", True) == "✓ Flex"
    assert selected_label("Flex", False) == "Flex"
    assert card_title("🧭", "Hashi mode") == "🧭 <b>HASHI MODE</b>\n━━━━━━━━━━━━━━━━"


def test_help_menu_groups_registered_commands_and_escapes_values() -> None:
    commands = [
        SimpleNamespace(command="status", description="View <status>"),
        SimpleNamespace(command="privacy", description="View or set privacy"),
        SimpleNamespace(command="focus", description="Narrow scope and continue"),
        SimpleNamespace(command="private_x", description="Private command"),
    ]

    text = help_menu_text(
        agent_name="lin<yueru>",
        agent_type="flex&safe",
        commands=commands,
        disabled=["terminate"],
        show_descriptions=True,
    )

    assert text.startswith("⚔️ <b>HASHI COMMAND CENTRE</b>")
    assert "<b>lin&lt;yueru&gt;</b> · <code>flex&amp;safe</code>" in text
    assert "⚡ <b>EVERYDAY</b>" in text
    assert "🧠 <b>MODELS &amp; MODES</b>" in text
    assert "🧭 <b>EXECUTION CONTROL</b>" in text
    assert "◇ <b>MORE</b>" in text
    assert "<code>/status</code>  View &lt;status&gt;" in text
    assert "<code>/terminate</code>" in text


def test_setting_card_keeps_standard_information_order() -> None:
    text = setting_card(
        "📡",
        "Stream",
        current="<b>OFF</b>",
        facts=["<b>Saved</b> · workspace"],
        consequence="Only final answers are sent.",
        action="Choose a state.",
    )

    assert text.startswith("📡 <b>STREAM</b>\n━━━━━━━━━━━━━━━━")
    assert text.index("<b>Current</b>") < text.index("<b>Saved</b>")
    assert text.index("<b>Saved</b>") < text.index("Only final answers")
    assert text.endswith("Choose a state.")


def test_confirm_card_names_target_and_safe_escape_instruction() -> None:
    text = confirm_card(
        "⚠️",
        "Delete agent",
        target="<code>zelda</code>",
        consequence="Configuration is removed; files remain.",
    )

    assert "<b>Target</b> · <code>zelda</code>" in text
    assert "Confirm below, or keep the current state." in text
