from pathlib import Path

from orchestrator.bridge_memory import BridgeContextAssembler, BridgeMemoryStore
from orchestrator.hcc import HCC_SYSTEM_GUIDANCE, replace_hcc_entry, set_hcc_enabled
from orchestrator.pcm import render_pcm_document


def assembler(tmp_path: Path) -> BridgeContextAssembler:
    pcm = tmp_path / "agent.md"
    pcm.write_text(render_pcm_document(persona="P", system="S", memory="M", hcc=""), encoding="utf-8")
    return BridgeContextAssembler(BridgeMemoryStore(tmp_path), pcm)


def section(payload, key):
    return next((item for item in payload["transport_snapshot"]["sections"] if item["key"] == key), None)


def test_hcc_is_read_fresh_on_every_turn_and_always_injected_when_on(tmp_path: Path):
    ctx = assembler(tmp_path)
    replace_hcc_entry(tmp_path, "weather", "Weather snapshot A")
    set_hcc_enabled(tmp_path, True)
    first = ctx.build_prompt_payload("Do I need a coat?", "her-v2")
    assert "Weather snapshot A" in section(first, "hcc")["text"]
    assert HCC_SYSTEM_GUIDANCE in section(first, "hcc")["text"]
    assert first["transport_snapshot"]["removed_section_keys"] == []

    replace_hcc_entry(tmp_path, "weather", "Weather snapshot B")
    second = ctx.build_prompt_payload("Anything happening?", "her-v2", incremental=True)
    assert "Weather snapshot B" in section(second, "hcc")["text"]
    assert "Weather snapshot A" not in section(second, "hcc")["text"]


def test_hcc_off_explicitly_revokes_fixed_snapshot(tmp_path: Path):
    ctx = assembler(tmp_path)
    replace_hcc_entry(tmp_path, "news", "Cached headline")
    set_hcc_enabled(tmp_path, True)
    on = ctx.build_prompt_payload("hello", "her-v2")
    assert section(on, "hcc") is not None

    set_hcc_enabled(tmp_path, False)
    off = ctx.build_prompt_payload("hello again", "her-v2", incremental=True)
    assert section(off, "hcc") is None
    assert off["transport_snapshot"]["removed_section_keys"] == ["hcc"]


def test_empty_hcc_does_not_inject_but_requests_removal(tmp_path: Path):
    ctx = assembler(tmp_path)
    set_hcc_enabled(tmp_path, True)
    payload = ctx.build_prompt_payload("hello", "her-v2")
    assert section(payload, "hcc") is None
    assert payload["transport_snapshot"]["removed_section_keys"] == ["hcc"]
