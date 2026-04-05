"""Tests for the ETE v1 codec — validation path."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from specs.ete.v1.codec import validate_ete

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# ---------------------------------------------------------------------------
# Positive: valid fixtures pass validation
# ---------------------------------------------------------------------------


def test_validate_minimal_fixture():
    errors = validate_ete(FIXTURES / "minimal.ete")
    assert errors == [], f"unexpected errors: {errors}"


def test_validate_full_project_fixture():
    errors = validate_ete(FIXTURES / "full-project.ete")
    assert errors == [], f"unexpected errors: {errors}"


def test_validate_kasumi_nexcel_fixture():
    errors = validate_ete(FIXTURES / "kasumi-nexcel.ete")
    assert errors == [], f"unexpected errors: {errors}"


# ---------------------------------------------------------------------------
# Negative: invalid bundles produce expected errors
# ---------------------------------------------------------------------------


def test_validate_missing_manifest(tmp_path: Path):
    """A directory with no ete.json must produce E_MANIFEST_INVALID."""
    (tmp_path / "dummy.txt").write_text("not a bundle")
    errors = validate_ete(tmp_path)
    assert len(errors) == 1
    assert errors[0].code == "E_MANIFEST_INVALID"
    assert errors[0].severity == "fatal"


def test_validate_missing_artefact_meta(tmp_path: Path):
    """An artefact sub-directory with no meta.json must produce E_ARTEFACT_NO_META."""
    manifest = {
        "ete_version": "1.0.0",
        "producer": {"system": "test", "version": "0.0.1"},
        "produced_at": "2026-04-03T00:00:00Z",
        "contents": ["artefacts"],
    }
    (tmp_path / "ete.json").write_text(json.dumps(manifest))
    art_dir = tmp_path / "artefacts" / "art_broken"
    art_dir.mkdir(parents=True)
    (art_dir / "payload.txt").write_text("data")

    errors = validate_ete(tmp_path)
    codes = [e.code for e in errors]
    assert "E_ARTEFACT_NO_META" in codes


def test_validate_missing_payload(tmp_path: Path):
    """An artefact with meta.json but no payload must produce E_ARTEFACT_NO_PAYLOAD."""
    manifest = {
        "ete_version": "1.0.0",
        "producer": {"system": "test", "version": "0.0.1"},
        "produced_at": "2026-04-03T00:00:00Z",
        "contents": ["artefacts"],
    }
    (tmp_path / "ete.json").write_text(json.dumps(manifest))
    art_dir = tmp_path / "artefacts" / "art_no_payload"
    art_dir.mkdir(parents=True)
    meta = {
        "artefact_id": "art_no_payload",
        "title": "Missing Payload",
        "mime_type": "text/plain",
        "artefact_type": "file",
        "size_bytes": 0,
        "source": {"system": "test"},
        "created_at": "2026-04-03T00:00:00Z",
        "updated_at": "2026-04-03T00:00:00Z",
    }
    (art_dir / "meta.json").write_text(json.dumps(meta))

    errors = validate_ete(tmp_path)
    codes = [e.code for e in errors]
    assert "E_ARTEFACT_NO_PAYLOAD" in codes


def test_validate_section_declared_but_missing(tmp_path: Path):
    """ete.json lists 'tasks' in contents but no tasks/ dir exists."""
    manifest = {
        "ete_version": "1.0.0",
        "producer": {"system": "test", "version": "0.0.1"},
        "produced_at": "2026-04-03T00:00:00Z",
        "contents": ["tasks"],
    }
    (tmp_path / "ete.json").write_text(json.dumps(manifest))

    errors = validate_ete(tmp_path)
    codes = [e.code for e in errors]
    assert "E_SECTION_DECLARED_BUT_MISSING" in codes
    assert any(e.section == "tasks" for e in errors)
