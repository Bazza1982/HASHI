"""ETE v1.0 — Pydantic models for Epistula Tegami Exchange."""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ETE_VERSION = "1.0.0"

CANONICAL_MIME_NEXCEL = "application/vnd.hashi.kasumi.nexcel+json"
CANONICAL_MIME_WORDO = "application/vnd.hashi.kasumi.wordo+json"

LEGACY_MIME_MAP: dict[str, str] = {
    "application/vnd.minato.nexcel+json": CANONICAL_MIME_NEXCEL,
    "application/x-kasumi-nexcel+json": CANONICAL_MIME_NEXCEL,
    "application/vnd.minato.wordo+json": CANONICAL_MIME_WORDO,
    "application/x-kasumi-wordo+json": CANONICAL_MIME_WORDO,
}

KB_ALLOWED_EXTENSIONS: set[str] = {".md", ".json", ".pdf", ".png", ".jpg", ".jpeg", ".svg"}
KB_MAX_FILE_BYTES: int = 200 * 1024 * 1024
KB_MAX_BLOCK_BYTES: int = 500 * 1024 * 1024
KB_MAX_SECTION_BYTES: int = 5 * 1024 * 1024 * 1024

# ---------------------------------------------------------------------------
# Manifest models
# ---------------------------------------------------------------------------


class ETEProducer(BaseModel):
    """Identifies the system that produced an ETE bundle."""

    system: str
    version: str


class ETEManifest(BaseModel):
    """The ete.json manifest — the only required file in a bundle."""

    ete_version: str
    producer: ETEProducer
    produced_at: str
    contents: list[str]


# ---------------------------------------------------------------------------
# Artefact models
# ---------------------------------------------------------------------------


class ETEArtefactSource(BaseModel):
    """Provenance information for an artefact."""

    system: str
    session_id: Optional[str] = None
    run_id: Optional[str] = None


class ETEArtefactMeta(BaseModel):
    """Metadata envelope for a single artefact (artefacts/{id}/meta.json)."""

    model_config = ConfigDict(extra="allow")

    artefact_id: str
    title: str
    mime_type: str
    artefact_type: str
    content_hash: Optional[str] = None
    size_bytes: int
    envelope_version: int = 0
    correlation_id: Optional[str] = None
    source: ETEArtefactSource
    created_at: str
    updated_at: str
    extra: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Project / Task models
# ---------------------------------------------------------------------------


class ETEProject(BaseModel):
    """Project metadata (project.json)."""

    model_config = ConfigDict(extra="allow")

    project_id: str
    name: str
    description: str = ""
    status: str
    priority: Optional[str] = None
    owner: Optional[str] = None
    created_at: str
    updated_at: str
    tags: list[str] = Field(default_factory=list)
    extra: dict = Field(default_factory=dict)


class ETETask(BaseModel):
    """A single task record (tasks/{task_id}.json)."""

    model_config = ConfigDict(extra="allow")

    task_id: str
    project_id: str
    name: str
    status: str
    priority: Optional[str] = None
    assignee: Optional[str] = None
    created_at: str
    completed_at: Optional[str] = None
    depends_on: list[str] = Field(default_factory=list)
    extra: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Import result models
# ---------------------------------------------------------------------------


class ETESectionResult(BaseModel):
    """Per-section outcome of an ETE import."""

    status: Literal["ok", "skipped", "failed"]
    imported_count: int = 0
    skipped_count: int = 0
    error: Optional[str] = None


class ETEImportError(BaseModel):
    """A single validation or import error."""

    code: str
    message: str
    severity: Literal["fatal", "recoverable", "warning"]
    section: Optional[str] = None
    entity_id: Optional[str] = None


class ETEImportResult(BaseModel):
    """Aggregate result of an ETE import operation."""

    success: bool
    sections: dict[str, ETESectionResult]
    errors: list[ETEImportError]


# ---------------------------------------------------------------------------
# Bundle (in-memory representation of an imported ETE)
# ---------------------------------------------------------------------------


class ETEBundle(BaseModel):
    """In-memory representation of a fully-parsed ETE bundle."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    manifest: ETEManifest
    project: Optional[ETEProject] = None
    tasks: list[ETETask] = Field(default_factory=list)
    artefacts: list[ETEArtefactMeta] = Field(default_factory=list)
    artefact_payloads: dict[str, Path] = Field(default_factory=dict)
    workflow_paths: list[Path] = Field(default_factory=list)
    knowledge_block_dirs: list[Path] = Field(default_factory=list)
