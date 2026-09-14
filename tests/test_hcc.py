from pathlib import Path

import pytest

from orchestrator.hcc import inspect_hcc_entry, is_hcc_enabled, replace_hcc_entry, set_hcc_enabled
from orchestrator.pcm import load_pcm_document, parse_pcm_text, render_pcm_document


def seed(workspace: Path, *, hcc: str = "") -> Path:
    path = workspace / "agent.md"
    path.write_text(
        render_pcm_document(persona="P", system="S", memory="M", hcc=hcc),
        encoding="utf-8",
    )
    return path


def test_hcc_block_is_optional_and_may_be_empty(tmp_path: Path):
    no_hcc = parse_pcm_text("[persona]\nP\n[persona_end]\n\n[sys]\nS\n[sys_end]\n")
    assert no_hcc.hcc == ""
    empty = parse_pcm_text("[persona]\nP\n[persona_end]\n\n[sys]\nS\n[sys_end]\n\n[hcc]\n[hcc_end]\n")
    assert empty.hcc == ""


def test_hcc_switch_persists_without_touching_agent_md(tmp_path: Path):
    path = seed(tmp_path, hcc="cached")
    before = path.read_bytes()
    assert is_hcc_enabled(tmp_path) is False
    assert set_hcc_enabled(tmp_path, True) is True
    assert is_hcc_enabled(tmp_path) is True
    assert path.read_bytes() == before
    assert set_hcc_enabled(tmp_path, False) is False
    assert is_hcc_enabled(tmp_path) is False


def test_replace_entry_preserves_other_pcm_and_hcc_entries(tmp_path: Path):
    path = seed(tmp_path)
    first = replace_hcc_entry(tmp_path, "weather", "Sydney 20C")
    replace_hcc_entry(tmp_path, "news", "Headline A")
    second = replace_hcc_entry(tmp_path, "weather", "Sydney 21C", expected_digest=first)
    doc = load_pcm_document(path, workspace_dir=tmp_path)
    assert doc.persona == "P" and doc.system == "S" and doc.memory == "M"
    assert "Sydney 20C" not in doc.hcc
    assert "Sydney 21C" in doc.hcc
    assert "Headline A" in doc.hcc
    assert inspect_hcc_entry(tmp_path, "weather")["digest"] == second


def test_stale_entry_update_fails_without_changing_file(tmp_path: Path):
    path = seed(tmp_path)
    replace_hcc_entry(tmp_path, "weather", "old")
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="changed since inspection"):
        replace_hcc_entry(tmp_path, "weather", "new", expected_digest="0" * 64)
    assert path.read_bytes() == before


def test_invalid_refresh_does_not_clear_last_success(tmp_path: Path):
    path = seed(tmp_path)
    replace_hcc_entry(tmp_path, "weather", "last good")
    before = path.read_bytes()
    with pytest.raises(ValueError):
        replace_hcc_entry(tmp_path, "weather", "   ")
    assert path.read_bytes() == before
