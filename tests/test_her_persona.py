from __future__ import annotations

import hashlib

import pytest

from adapters.her_persona import (
    PERSONA_BLOCK_BEGIN,
    PERSONA_BLOCK_END,
    load_configured_persona,
    load_persona_packaging_source,
)


@pytest.mark.parametrize("relative", ["agent.md", "AGENT.md", "nested/voice.md"])
def test_configured_system_md_is_the_only_persona_source(tmp_path, relative):
    configured = tmp_path / relative
    configured.parent.mkdir(parents=True, exist_ok=True)
    configured.write_text("Fictional Persona: 星砂守望者。", encoding="utf-8")
    conflicting = tmp_path / ("AGENT.md" if relative != "AGENT.md" else "agent.md")
    conflicting.write_text("WRONG CONVENTIONAL FALLBACK", encoding="utf-8")
    before = configured.read_bytes()

    source = load_configured_persona(configured)

    assert source.usable is True
    assert source.path == configured
    assert source.content == "Fictional Persona: 星砂守望者。"
    assert "WRONG CONVENTIONAL FALLBACK" not in source.content
    assert source.content_sha256 == hashlib.sha256(before).hexdigest()
    assert configured.read_bytes() == before


def test_missing_empty_and_invalid_utf8_sources_fail_closed_without_creation(tmp_path):
    missing = tmp_path / "custom" / "persona.txt"
    missing_source = load_configured_persona(missing)
    assert missing_source.usable is False
    assert missing_source.unavailable_reason == "system_md_missing"
    assert not missing.exists()

    empty = tmp_path / "empty.md"
    empty.write_text(" \n", encoding="utf-8")
    empty_source = load_configured_persona(empty)
    assert empty_source.available is True
    assert empty_source.nonempty is False
    assert empty_source.unavailable_reason == "system_md_empty"

    invalid = tmp_path / "invalid.md"
    invalid.write_bytes(b"\xff\xfe\x00")
    invalid_source = load_configured_persona(invalid)
    assert invalid_source.usable is False
    assert (
        invalid_source.unavailable_reason == "system_md_unreadable:UnicodeDecodeError"
    )


def test_persona_audit_fields_never_include_private_content_or_full_path(tmp_path):
    configured = tmp_path / "private" / "persona.md"
    configured.parent.mkdir()
    configured.write_text("PRIVATE PERSONA CONTENT", encoding="utf-8")

    fields = load_configured_persona(configured).audit_fields()
    serialized = repr(fields)

    assert fields["persona_source_label"] == "persona.md"
    assert fields["persona_source_available"] is True
    assert fields["persona_source_nonempty"] is True
    assert "PRIVATE PERSONA CONTENT" not in serialized
    assert str(configured) not in serialized


def test_v2_packaging_source_exposes_only_the_explicit_persona_block(tmp_path):
    assert PERSONA_BLOCK_BEGIN == "[persona]"
    assert PERSONA_BLOCK_END == "[persona_end]"

    configured = tmp_path / "agent.md"
    configured.write_text(
        "\n".join(
            [
                "PRIVATE OPERATION RULE OUTSIDE THE BLOCK",
                PERSONA_BLOCK_BEGIN,
                "Use a warm voice and address the user as Captain.",
                PERSONA_BLOCK_END,
                "ANOTHER PRIVATE RULE OUTSIDE THE BLOCK",
            ]
        ),
        encoding="utf-8",
    )

    source = load_persona_packaging_source(
        configured,
        display_name="Navigator",
    )

    assert source.usable is True
    assert source.display_name == "Navigator"
    assert source.guidance == "Use a warm voice and address the user as Captain."
    assert "PRIVATE OPERATION" not in source.guidance
    assert "ANOTHER PRIVATE" not in source.guidance
    assert "Captain" not in repr(source.audit_fields())


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("No markers here", "persona_block_missing"),
        (
            f"{PERSONA_BLOCK_BEGIN}\n\n{PERSONA_BLOCK_END}",
            "persona_block_empty",
        ),
        (
            f"{PERSONA_BLOCK_BEGIN}\none\n{PERSONA_BLOCK_BEGIN}\ntwo\n"
            f"{PERSONA_BLOCK_END}",
            "persona_block_ambiguous",
        ),
    ],
)
def test_invalid_v2_persona_blocks_select_minimal_fallback(
    tmp_path, content, reason
):
    configured = tmp_path / "agent.md"
    configured.write_text(content, encoding="utf-8")

    source = load_persona_packaging_source(configured, display_name="  Zelda  ")

    assert source.usable is False
    assert source.guidance == ""
    assert source.display_name == "Zelda"
    assert source.unavailable_reason == reason
