"""HERv3 live-path catalogue projection (Phase 1b).

The flex prompt path drops the PCM text catalogues when native function
schemas are present, but the fixed (session) path sends the PCM snapshot to
the model verbatim. These tests pin the live projection: HERV3 keeps a cheap
name index plus skill locations, while CLI Engines and tool-less models keep
the full text catalogue.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.bridge_memory import (
    NATIVE_TOOL_INDEX_NOTE,
    BridgeContextAssembler,
    BridgeMemoryStore,
)
from orchestrator.pcm import render_pcm_document

LONG_TOOL_DESCRIPTION = "LONG-DESCRIPTION-MARKER " * 12
LONG_SKILL_DESCRIPTION = "skill-description-marker " * 20


def _tools(count: int = 40) -> list[tuple[str, str]]:
    rows = [("file_read", "Read a file from disk.")]
    rows.extend(
        (f"tool_{index:02d}", f"{LONG_TOOL_DESCRIPTION} #{index}")
        for index in range(count)
    )
    return rows


def _skills(root: Path) -> list[dict[str, str]]:
    rows = []
    for name in ("alpha", "beta", "gamma"):
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text("body", encoding="utf-8")
        rows.append(
            {
                "id": name,
                "name": name,
                "description": f"{LONG_SKILL_DESCRIPTION} {name}",
                "skill_md": str(directory / "SKILL.md"),
            }
        )
    return rows


@pytest.fixture()
def assembler_factory(tmp_path: Path):
    def build(*, with_skill_paths: bool = True) -> BridgeContextAssembler:
        pcm = tmp_path / "agent.md"
        pcm.parent.mkdir(parents=True, exist_ok=True)
        pcm.write_text(
            render_pcm_document(persona="Persona", system="System", memory="Facts."),
            encoding="utf-8",
        )
        store = BridgeMemoryStore(tmp_path)
        skills = _skills(tmp_path / "skills")
        if not with_skill_paths:
            skills = [
                {"id": item["id"], "description": item["description"]}
                for item in skills
            ]
        return BridgeContextAssembler(
            store,
            pcm,
            tool_catalog_provider=lambda: _tools(),
            skill_catalog_provider=lambda: skills,
        )

    return build


def _section(payload: dict, key: str) -> dict | None:
    for section in payload["transport_snapshot"]["sections"]:
        if section["key"] == key:
            return section
    return None


def test_herv3_cloud_projects_tool_index_instead_of_full_catalogue(
    assembler_factory,
) -> None:
    payload = assembler_factory().build_prompt_payload("do work", "her-v2")

    tools = _section(payload, "tools_catalogue")
    assert tools is not None
    assert NATIVE_TOOL_INDEX_NOTE in tools["text"]
    assert "- file_read" in tools["text"]
    assert "- tool_00" in tools["text"]
    assert LONG_TOOL_DESCRIPTION not in tools["text"]
    assert tools["metadata"] == {"compact": True}

    # A names-only index is a fraction of the duplicated catalogue.
    full_lines = assembler_factory()._catalogue_lines(lambda: _tools())
    assert len(tools["text"]) < len("\n".join(full_lines)) * 0.35


def test_herv3_cloud_projects_skill_index_with_real_paths(assembler_factory) -> None:
    payload = assembler_factory().build_prompt_payload("do work", "her-v2")

    skills = _section(payload, "skills_catalogue")
    assert skills is not None
    assert "SKILL.md" in skills["text"]
    assert "root" in skills["text"].casefold()
    assert LONG_SKILL_DESCRIPTION not in skills["text"]
    assert skills["metadata"] == {"compact": True}


def test_skill_index_without_paths_never_promises_a_path(assembler_factory) -> None:
    payload = assembler_factory(with_skill_paths=False).build_prompt_payload(
        "do work", "her-v2"
    )

    skills = _section(payload, "skills_catalogue")
    assert skills is not None
    assert "one line per installed skill" in skills["text"]
    assert "[read " not in skills["text"]
    assert "SKILL.md path on each line" not in skills["text"]


def test_non_mapping_skill_items_keep_the_text_form(tmp_path: Path) -> None:
    pcm = tmp_path / "agent.md"
    pcm.parent.mkdir(parents=True, exist_ok=True)
    pcm.write_text(
        render_pcm_document(persona="Persona", system="System", memory="Facts."),
        encoding="utf-8",
    )
    assembler = BridgeContextAssembler(
        BridgeMemoryStore(tmp_path),
        pcm,
        tool_catalog_provider=lambda: _tools(),
        skill_catalog_provider=lambda: [("alpha", "does alpha"), ("beta", "does beta")],
    )

    payload = assembler.build_prompt_payload("do work", "her-v2")

    skills = _section(payload, "skills_catalogue")
    assert skills is not None
    assert "- alpha: does alpha" in skills["text"]
    assert not skills["metadata"]


def test_cli_engine_keeps_full_catalogues(assembler_factory) -> None:
    payload = assembler_factory().build_prompt_payload("do work", "codex-cli")

    tools = _section(payload, "tools_catalogue")
    skills = _section(payload, "skills_catalogue")
    assert tools is not None and LONG_TOOL_DESCRIPTION in tools["text"]
    assert skills is not None and LONG_SKILL_DESCRIPTION in skills["text"]


def test_herv3_with_tools_disabled_keeps_text_fallback(assembler_factory) -> None:
    payload = assembler_factory().build_prompt_payload(
        "do work", "her-v2", retrieval_capable=False
    )

    tools = _section(payload, "tools_catalogue")
    assert tools is not None and LONG_TOOL_DESCRIPTION in tools["text"]


def test_local_profile_still_omits_catalogue_sections(assembler_factory) -> None:
    payload = assembler_factory().build_prompt_payload(
        "do work", "her-v2", pcm_profile="local"
    )

    assert _section(payload, "tools_catalogue") is None
    assert _section(payload, "skills_catalogue") is None
