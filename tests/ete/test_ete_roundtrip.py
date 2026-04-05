"""Tests for ETE v1 codec — roundtrip (import → export → re-import)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from specs.ete.v1.codec import export_ete, import_ete, validate_ete
from specs.ete.v1.ete_types import (
    ETEArtefactMeta,
    ETEArtefactSource,
    ETEBundle,
    ETEManifest,
    ETEProducer,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _roundtrip(fixture_name: str, tmp_path: Path) -> tuple[ETEBundle, ETEBundle]:
    """Import a fixture, export to tmp_path, re-import, and return both bundles."""
    bundle1 = import_ete(FIXTURES / fixture_name)
    export_dir = tmp_path / "exported"
    export_ete(
        export_dir,
        manifest=bundle1.manifest,
        project=bundle1.project,
        tasks=bundle1.tasks if bundle1.tasks else None,
        artefacts=(
            [(meta, bundle1.artefact_payloads[meta.artefact_id]) for meta in bundle1.artefacts]
            if bundle1.artefacts
            else None
        ),
        workflows=bundle1.workflow_paths if bundle1.workflow_paths else None,
        knowledge_blocks=bundle1.knowledge_block_dirs if bundle1.knowledge_block_dirs else None,
    )
    bundle2 = import_ete(export_dir)
    return bundle1, bundle2


# ---------------------------------------------------------------------------
# Roundtrip tests
# ---------------------------------------------------------------------------


def test_self_roundtrip_full_project(tmp_path: Path):
    b1, b2 = _roundtrip("full-project.ete", tmp_path)

    # project_id preserved
    assert b1.project is not None and b2.project is not None
    assert b1.project.project_id == b2.project.project_id

    # task count and IDs preserved
    assert len(b1.tasks) == len(b2.tasks)
    ids1 = {t.task_id for t in b1.tasks}
    ids2 = {t.task_id for t in b2.tasks}
    assert ids1 == ids2

    # artefact count and IDs preserved
    assert len(b1.artefacts) == len(b2.artefacts)
    art_ids1 = {a.artefact_id for a in b1.artefacts}
    art_ids2 = {a.artefact_id for a in b2.artefacts}
    assert art_ids1 == art_ids2

    # content_hash values preserved
    hashes1 = {a.artefact_id: a.content_hash for a in b1.artefacts}
    hashes2 = {a.artefact_id: a.content_hash for a in b2.artefacts}
    assert hashes1 == hashes2


def test_self_roundtrip_kasumi_nexcel(tmp_path: Path):
    b1, b2 = _roundtrip("kasumi-nexcel.ete", tmp_path)

    # artefact meta preserved
    assert len(b1.artefacts) == len(b2.artefacts)
    for a1, a2 in zip(b1.artefacts, b2.artefacts):
        assert a1.artefact_id == a2.artefact_id
        assert a1.mime_type == a2.mime_type
        assert a1.content_hash == a2.content_hash
        # envelope_version preserved
        assert a1.envelope_version == a2.envelope_version


def test_self_roundtrip_minimal(tmp_path: Path):
    b1, b2 = _roundtrip("minimal.ete", tmp_path)

    assert len(b1.artefacts) == len(b2.artefacts)
    assert b1.artefacts[0].artefact_id == b2.artefacts[0].artefact_id
    assert b1.artefacts[0].content_hash == b2.artefacts[0].content_hash


def test_export_then_validate(tmp_path: Path):
    """Export a programmatically created bundle and verify it validates cleanly."""
    manifest = ETEManifest(
        ete_version="1.0.0",
        producer=ETEProducer(system="test-harness", version="0.0.1"),
        produced_at="2026-04-03T00:00:00Z",
        contents=["artefacts"],
    )
    # Create a temporary payload file
    payload = tmp_path / "source_payload.txt"
    payload.write_text("hello world")

    meta = ETEArtefactMeta(
        artefact_id="art_synth_001",
        title="Synthetic Test",
        mime_type="text/plain",
        artefact_type="file",
        size_bytes=11,
        source=ETEArtefactSource(system="test-harness"),
        created_at="2026-04-03T00:00:00Z",
        updated_at="2026-04-03T00:00:00Z",
    )

    out = tmp_path / "bundle_out"
    export_ete(out, manifest=manifest, artefacts=[(meta, payload)])

    errors = validate_ete(out)
    assert errors == [], f"unexpected errors: {errors}"


def test_semantic_equivalence(tmp_path: Path):
    """Export, reimport, re-export — core fields must match across cycles."""
    b1 = import_ete(FIXTURES / "full-project.ete")

    # First export
    dir1 = tmp_path / "cycle1"
    export_ete(
        dir1,
        manifest=b1.manifest,
        project=b1.project,
        tasks=b1.tasks if b1.tasks else None,
        artefacts=(
            [(m, b1.artefact_payloads[m.artefact_id]) for m in b1.artefacts]
            if b1.artefacts
            else None
        ),
        workflows=b1.workflow_paths if b1.workflow_paths else None,
        knowledge_blocks=b1.knowledge_block_dirs if b1.knowledge_block_dirs else None,
    )

    # Re-import and second export
    b2 = import_ete(dir1)
    dir2 = tmp_path / "cycle2"
    export_ete(
        dir2,
        manifest=b2.manifest,
        project=b2.project,
        tasks=b2.tasks if b2.tasks else None,
        artefacts=(
            [(m, b2.artefact_payloads[m.artefact_id]) for m in b2.artefacts]
            if b2.artefacts
            else None
        ),
        workflows=b2.workflow_paths if b2.workflow_paths else None,
        knowledge_blocks=b2.knowledge_block_dirs if b2.knowledge_block_dirs else None,
    )

    # Re-import cycle 2
    b3 = import_ete(dir2)

    # Semantic equivalence: core identifiers and hashes match across all three
    assert b1.project.project_id == b2.project.project_id == b3.project.project_id

    task_ids_1 = {t.task_id for t in b1.tasks}
    task_ids_2 = {t.task_id for t in b2.tasks}
    task_ids_3 = {t.task_id for t in b3.tasks}
    assert task_ids_1 == task_ids_2 == task_ids_3

    art_ids_1 = {a.artefact_id for a in b1.artefacts}
    art_ids_2 = {a.artefact_id for a in b2.artefacts}
    art_ids_3 = {a.artefact_id for a in b3.artefacts}
    assert art_ids_1 == art_ids_2 == art_ids_3

    hashes_1 = {a.artefact_id: a.content_hash for a in b1.artefacts}
    hashes_2 = {a.artefact_id: a.content_hash for a in b2.artefacts}
    hashes_3 = {a.artefact_id: a.content_hash for a in b3.artefacts}
    assert hashes_1 == hashes_2 == hashes_3
