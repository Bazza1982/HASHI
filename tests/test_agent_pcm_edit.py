from __future__ import annotations

import pytest

from orchestrator.agent_pcm_edit import read_agent_pcm, replace_agent_pcm
from orchestrator.pcm import PCMValidationError, render_pcm_document


def test_agent_document_round_trip_and_stale_edit_are_fenced(tmp_path):
    path = tmp_path / "agent.md"
    original = render_pcm_document(persona="before", system="system")
    changed = render_pcm_document(persona="edited", system="system")
    path.write_text(original, encoding="utf-8", newline="")
    snapshot = read_agent_pcm(tmp_path)
    assert snapshot["content"] == original
    saved = replace_agent_pcm(tmp_path, snapshot["revision"], changed)
    assert saved["revision"] != snapshot["revision"]
    assert path.read_text(encoding="utf-8") == changed
    with pytest.raises(PCMValidationError) as error:
        replace_agent_pcm(tmp_path, snapshot["revision"], original)
    assert error.value.code == "pcm_conflict"
    assert path.read_text(encoding="utf-8") == changed


def test_invalid_document_never_replaces_existing_agent_document(tmp_path):
    path = tmp_path / "agent.md"
    original = render_pcm_document(persona="before", system="system")
    path.write_text(original, encoding="utf-8", newline="")
    snapshot = read_agent_pcm(tmp_path)
    with pytest.raises(PCMValidationError):
        replace_agent_pcm(tmp_path, snapshot["revision"], "not a valid agent document")
    assert path.read_text(encoding="utf-8") == original


def test_configured_large_document_round_trip_and_conflict(tmp_path):
    path = tmp_path / "agent.md"
    original = render_pcm_document(persona="before", system="x" * (2 * 1024 * 1024 + 80))
    updated = render_pcm_document(persona="edited", system="y" * (2 * 1024 * 1024 + 80))
    path.write_text(original, encoding="utf-8", newline="")
    limit = 3 * 1024 * 1024
    snapshot = read_agent_pcm(tmp_path, max_bytes=limit)
    assert snapshot["content"] == original
    saved = replace_agent_pcm(tmp_path, snapshot["revision"], updated, max_bytes=limit)
    assert saved["revision"] != snapshot["revision"]
    assert path.read_text(encoding="utf-8") == updated
    with pytest.raises(PCMValidationError) as error:
        replace_agent_pcm(tmp_path, snapshot["revision"], original, max_bytes=limit)
    assert error.value.code == "pcm_conflict"
    with pytest.raises(PCMValidationError) as error:
        replace_agent_pcm(tmp_path, saved["revision"], original, max_bytes=1024)
    assert error.value.code == "pcm_too_large"
    assert path.read_text(encoding="utf-8") == updated
