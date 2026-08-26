from __future__ import annotations

import hashlib

import pytest

from adapters.her_persona import (
    load_configured_persona,
    load_persona_packaging_source,
)
from orchestrator.pcm import render_pcm_document


def _write_pcm(path, *, persona="Fictional Persona: 星砂守望者。"):
    path.write_text(
        render_pcm_document(persona=persona, system="PRIVATE SYSTEM POLICY"),
        encoding="utf-8",
    )


def test_exact_lowercase_agent_md_is_the_only_persona_source(tmp_path):
    configured = tmp_path / "agent.md"
    _write_pcm(configured)
    (tmp_path / "AGENT.md").write_text("WRONG FALLBACK", encoding="utf-8")

    source = load_configured_persona(configured)

    assert source.usable is True
    assert source.path == configured
    assert source.content == "Fictional Persona: 星砂守望者。"
    assert source.content_sha256 == hashlib.sha256(source.content.encode()).hexdigest()
    assert "PRIVATE SYSTEM POLICY" not in source.content


@pytest.mark.parametrize(
    ("name", "content", "reason"),
    [
        ("AGENT.md", "valid", "pcm_noncanonical_filename"),
        ("agent.md", "", "pcm_required_block_missing"),
        ("agent.md", "outside\n[persona]\nx\n[persona_end]\n[sys]\ny\n[sys_end]\n", "pcm_unmarked_content"),
    ],
)
def test_invalid_pcm_sources_fail_closed(tmp_path, name, content, reason):
    configured = tmp_path / name
    if content == "valid":
        _write_pcm(configured)
    else:
        configured.write_text(content, encoding="utf-8")
    source = load_configured_persona(configured)
    assert source.usable is False
    assert source.unavailable_reason == reason


def test_missing_and_invalid_utf8_sources_fail_closed_without_creation(tmp_path):
    missing = tmp_path / "agent.md"
    missing_source = load_configured_persona(missing)
    assert missing_source.unavailable_reason == "pcm_missing"
    assert not missing.exists()

    missing.write_bytes(b"\xff\xfe\x00")
    invalid_source = load_configured_persona(missing)
    assert invalid_source.unavailable_reason == "pcm_invalid_utf8"


def test_persona_audit_fields_never_include_private_content_or_full_path(tmp_path):
    configured = tmp_path / "agent.md"
    _write_pcm(configured, persona="PRIVATE PERSONA CONTENT")

    fields = load_configured_persona(configured).audit_fields()
    serialized = repr(fields)

    assert fields["persona_source_label"] == "agent.md"
    assert fields["persona_source_available"] is True
    assert "PRIVATE PERSONA CONTENT" not in serialized
    assert str(configured) not in serialized


def test_v2_packaging_exposes_only_validated_persona_block(tmp_path):
    configured = tmp_path / "agent.md"
    _write_pcm(
        configured,
        persona="Use a warm voice and address the user as Captain.",
    )

    source = load_persona_packaging_source(configured, display_name="Navigator")

    assert source.usable is True
    assert source.display_name == "Navigator"
    assert source.guidance == "Use a warm voice and address the user as Captain."
    assert "PRIVATE SYSTEM POLICY" not in source.guidance
    assert "Captain" not in repr(source.audit_fields())


def test_invalid_pcm_selects_internal_packaging_fallback_but_is_not_usable(tmp_path):
    configured = tmp_path / "agent.md"
    configured.write_text("No markers here", encoding="utf-8")

    source = load_persona_packaging_source(configured, display_name=" Zelda ")

    assert source.usable is False
    assert source.guidance == ""
    assert source.display_name == "Zelda"
    assert source.unavailable_reason == "pcm_unmarked_content"
