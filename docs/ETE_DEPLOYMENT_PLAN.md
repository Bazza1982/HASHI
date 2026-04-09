# ETE (Epistula Tegami Exchange) Protocol Deployment Plan

> **Owner:** HASHI (platform layer)
> **Version:** v1.0-draft
> **Date:** 2026-04-03
> **Author:** Lily (CoS)

---

## 1. What is ETE?

ETE is a **versioned, directory-based interchange format** that allows any HASHI-ecosystem application to export a project bundle and any other compliant application to import it without errors.

Think of it like `.docx` for the HASHI ecosystem. Minato, Veritas, and future systems all have completely different internal architectures, but they all read and write the same ETE format.

**ETE is owned by HASHI, not by any consumer.** Schema changes go through the HASHI repo. This ensures no single consumer controls the interchange standard.

---

## 2. ETE v1.0 Specification

### 2.1 Bundle Structure

An ETE bundle is a directory (optionally zipped as `.ete`):

```
my-project.ete/
  ete.json                          # REQUIRED: manifest
  project.json                      # optional: project metadata
  tasks/
    {task_id}.json                  # optional: task records
  artefacts/
    {artefact_id}/
      meta.json                     # artefact metadata (ETE schema)
      payload.*                     # actual file (nexcel, wordo, pdf, md, etc.)
  workflows/
    {workflow_id}.yaml              # Nagare-format YAML
  knowledge_blocks/
    {correlation_id}/
      bundle_manifest.json          # knowledge block metadata
      *.md                          # markdown with frontmatter
      *.json                        # structured chunks
      *.pdf                         # source PDF
```

### 2.2 ete.json (Manifest)

The only **required** file in an ETE bundle.

```json
{
  "ete_version": "1.0.0",
  "producer": {
    "system": "veritas",
    "version": "3.0.0"
  },
  "produced_at": "2026-04-03T15:32:00Z",
  "contents": ["project", "artefacts", "workflows", "knowledge_blocks"]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `ete_version` | string (semver) | yes | ETE spec version used to produce this bundle |
| `producer.system` | string | yes | Producing system identifier |
| `producer.version` | string | yes | Producing system version |
| `produced_at` | string (ISO 8601) | yes | Timestamp of export |
| `contents` | string[] | yes | Which sections are present in this bundle |

### 2.3 Artefact meta.json

Each artefact directory contains a `meta.json` describing the payload:

```json
{
  "artefact_id": "a1b2c3d4",
  "title": "Budget Tracker",
  "mime_type": "application/vnd.hashi.kasumi.nexcel+json",
  "artefact_type": "nexcel",
  "content_hash": "sha256:abcdef...",
  "size_bytes": 12345,
  "envelope_version": 1,
  "correlation_id": "sess_01:run_01:a1b2c3d4",
  "source": {
    "system": "minato",
    "session_id": "sess_01",
    "run_id": "run_01"
  },
  "created_at": "2026-04-03T10:00:00Z",
  "updated_at": "2026-04-03T12:30:00Z"
}
```

### 2.4 project.json

```json
{
  "project_id": "proj_001",
  "name": "Q2 Research Sprint",
  "description": "...",
  "status": "active",
  "priority": "high",
  "owner": "lily",
  "created_at": "2026-04-01T00:00:00Z",
  "updated_at": "2026-04-03T15:00:00Z",
  "tags": ["research", "q2"],
  "extra": {}
}
```

The `extra` field is a dict for system-specific metadata that ETE preserves but does not interpret.

### 2.5 Task Records

Each file in `tasks/` follows:

```json
{
  "task_id": "task_042",
  "project_id": "proj_001",
  "name": "Literature review",
  "status": "completed",
  "priority": "medium",
  "assignee": "ying",
  "created_at": "2026-04-01T00:00:00Z",
  "completed_at": "2026-04-02T18:00:00Z",
  "depends_on": ["task_041"],
  "extra": {}
}
```

### 2.6 Design Principles

| Principle | Rule |
|-----------|------|
| **Reader be liberal** | Unknown fields are silently ignored, never cause errors |
| **Writer be strict** | Only write fields defined in the current ETE version |
| **Append-only evolution** | New fields are optional; existing fields never removed in minor versions |
| **Correlation ID always present** | Systems that lack correlation IDs generate one on export |
| **Nagare is canonical workflow** | Non-Nagare workflows are preserved with `x-ete-source-dialect` annotation |

### 2.7 Import Semantics

ETE defines clear rules for what happens when a bundle is imported into a system.

#### 2.7.1 Duplicate ID Handling

| Entity | Duplicate Detection | Default Behaviour |
|--------|--------------------|--------------------|
| Project | `project_id` match | **Skip** — existing project is not overwritten |
| Task | `task_id` match within same `project_id` | **Skip** — existing task preserved |
| Artefact | `artefact_id` match OR `content_hash` match | **Skip** — existing artefact preserved |
| Knowledge Block | `correlation_id` match | **Skip** — existing block preserved |
| Workflow | `workflow_id` match | **Replace** — workflows are declarative definitions, latest wins |

Consumers **may** offer a `conflict_policy` parameter (`skip | replace | fail`) to override the default. When `fail` is chosen, the import aborts at the first duplicate.

#### 2.7.2 Idempotency

Importing the same ETE bundle twice with default conflict policy **must** produce the same end state as importing it once. No duplicate records, no side effects.

#### 2.7.3 Partial Failure & Rollback

ETE import is **section-atomic**: each top-level section (`project`, `tasks`, `artefacts`, `workflows`, `knowledge_blocks`) is imported as a unit. If a section fails:

- The failing section is fully rolled back (no partial artefacts, no partial task lists)
- Previously completed sections are **kept** (not rolled back)
- The import returns a result object listing per-section status:

```python
class ETEImportResult:
    success: bool                              # True only if ALL sections succeeded
    sections: dict[str, SectionResult]         # per-section status
    errors: list[ETEImportError]               # all errors encountered

class SectionResult:
    status: Literal["ok", "skipped", "failed"]
    imported_count: int
    skipped_count: int                         # duplicates skipped
    error: Optional[str]
```

#### 2.7.4 Summary

Importers must never silently lose data. If a record is skipped due to conflict, it must appear in `skipped_count`. If a section fails, the error must be surfaced. The caller decides whether partial success is acceptable.

### 2.8 Bundle Integrity Rules

The `contents` field in `ete.json` declares which sections are present. Each declared section must meet minimum structural requirements:

| Section | Minimum Requirements |
|---------|---------------------|
| `project` | `project.json` exists and validates against schema |
| `tasks` | `tasks/` directory exists with at least one `{task_id}.json` that validates |
| `artefacts` | `artefacts/` directory exists; each subdirectory contains a valid `meta.json` and at least one `payload.*` file |
| `workflows` | `workflows/` directory exists with at least one `.yaml` file |
| `knowledge_blocks` | `knowledge_blocks/` directory exists; each subdirectory contains a `bundle_manifest.json` |

**Validation error codes:**

| Code | Meaning |
|------|---------|
| `E_MANIFEST_INVALID` | `ete.json` missing or fails schema validation |
| `E_SECTION_DECLARED_BUT_MISSING` | Section listed in `contents` but directory/file absent |
| `E_SECTION_UNDECLARED_BUT_PRESENT` | Directory exists but not listed in `contents` (warning, not fatal) |
| `E_ARTEFACT_NO_META` | Artefact directory exists but has no `meta.json` |
| `E_ARTEFACT_NO_PAYLOAD` | Artefact has `meta.json` but no payload file |
| `E_SCHEMA_VALIDATION` | A JSON file fails its schema check |
| `E_KB_NO_MANIFEST` | Knowledge block directory has no `bundle_manifest.json` |

`validate_ete()` returns a list of these errors. Import should refuse to proceed if any `E_*` (non-warning) errors are present.

### 2.9 Knowledge Block Constraints

Knowledge blocks are the least structured section and require explicit safety boundaries:

**Allowed file types:**

| Extension | Purpose |
|-----------|---------|
| `.md` | Markdown notes with frontmatter |
| `.json` | Structured chunk data |
| `.pdf` | Source documents |
| `.png`, `.jpg`, `.jpeg`, `.svg` | Embedded images referenced in markdown |

All other file types are **rejected** on import. In particular:
- No executable files (`.exe`, `.sh`, `.bat`, `.py`, `.js`, etc.)
- No symlinks (resolved or rejected, never followed)
- No hidden files (starting with `.`)

**Path constraints:**
- Maximum one level of subdirectory within each `{correlation_id}/` block
- File names must be valid on Windows, macOS, and Linux (no `<>:"/\|?*`, max 200 characters)
- No path traversal (`..` segments rejected)

**Size constraints:**
- Single file: max 200 MB (covers large PDFs)
- Single knowledge block (all files): max 500 MB
- Total `knowledge_blocks/` section: max 5 GB

**Import behaviour:** files that violate these constraints are skipped with a warning, not a fatal error. The knowledge block is imported with the valid files only.

### 2.10 Error Handling & Severity Levels

All ETE operations classify issues into three severity levels:

| Severity | Behaviour | Examples |
|----------|-----------|---------|
| **Fatal** | Operation aborts, section rolled back | Invalid `ete.json`, version mismatch, schema validation failure, corrupt payload |
| **Recoverable** | Item skipped, operation continues | Unknown MIME type, unsupported workflow dialect, single artefact missing payload |
| **Warning** | Logged, no action taken | Undeclared section present, unknown extra fields, file permission differences |

Importers must return all issues (not just the first fatal). This allows the caller to see the full picture before deciding whether to retry with a different `conflict_policy` or fix the bundle.

**Specific error classification:**

| Situation | Severity |
|-----------|----------|
| `ete_version` major mismatch | Fatal |
| `ete_version` minor too new (reader is older) | Fatal |
| Unknown field in any JSON | Warning (ignored per "reader be liberal") |
| Unknown MIME type in artefact | Recoverable (artefact skipped) |
| Unknown workflow dialect | Recoverable (workflow stored but marked non-executable) |
| Knowledge block file type not allowed | Recoverable (file skipped, rest of block imported) |
| Duplicate ID (default policy) | Recoverable (skipped) |
| Duplicate ID (policy = `fail`) | Fatal |
| Payload `content_hash` mismatch | Fatal (data integrity) |

### 2.11 Packaging (.ete ZIP Format)

When a bundle directory is zipped for transport, the following rules apply:

**Structure:**
- The zip file extension is `.ete`
- The zip must contain a **single root directory** named `{bundle_name}.ete/`
- `ete.json` must be at `{bundle_name}.ete/ete.json` (not at zip root, not nested deeper)

**Encoding:**
- File names: UTF-8
- Path separator: forward slash `/` (even on Windows)
- No absolute paths

**Compression:**
- Algorithm: DEFLATE (standard zip)
- No encryption (ETE bundles are not a security boundary; use transport-level encryption)

**Size:**
- No hard limit on zip size, but importers may reject bundles exceeding their configured maximum
- Importers must extract to a temporary directory and validate before committing

**Forbidden:**
- Zip bombs (ratio > 100:1 triggers rejection)
- Symlinks within zip
- Files outside the root directory (path traversal via `../`)

---

## 3. Canonical Standards (Resolving Historical Splits)

### 3.1 Kasumi MIME Types

| Role | MIME Type |
|------|-----------|
| **Canonical (ETE writers must use)** | `application/vnd.hashi.kasumi.nexcel+json` |
| **Canonical (ETE writers must use)** | `application/vnd.hashi.kasumi.wordo+json` |
| Legacy alias (ETE readers must accept) | `application/vnd.minato.nexcel+json` |
| Legacy alias (ETE readers must accept) | `application/x-kasumi-nexcel+json` |
| Legacy alias (ETE readers must accept) | `application/vnd.minato.wordo+json` |
| Legacy alias (ETE readers must accept) | `application/x-kasumi-wordo+json` |

### 3.2 Kasumi Envelope Format

ETE adopts **Veritas's versioned envelope** as the canonical format:

```json
{
  "kasumi_type": "nexcel",
  "envelope_version": 1,
  "kasumi_version": "1.2.0",
  "created_at": "...",
  "updated_at": "...",
  "table": { ... }
}
```

Systems that previously used flat payloads (Minato) must wrap content in this structure on ETE export. On import, if `envelope_version` is missing, treat it as `envelope_version: 0` (legacy).

### 3.3 Workflow YAML

Nagare's workflow schema is the canonical ETE workflow format:

```yaml
workflow_id: my_workflow
steps:
  - step_id: step_1
    agent: agent_name
    handler: handler_type
    config: { ... }
    depends_on: []
```

Workflows from other dialects (e.g., Veritas's `adapter_name`-based format) are preserved verbatim with:

```yaml
x-ete-source-dialect: veritas
workflow_id: inject_quantitative
steps:
  - step_id: classify
    adapter_name: prompt_contract
    config: { ... }
```

Readers that don't understand a dialect skip the workflow gracefully.

---

## 4. Deployment Phases

### Phase 0 -- Spec Authoring (Week 1)

**Goal:** Publish the ETE v1.0 spec as machine-readable JSON Schema + this document.

| Deliverable | Location |
|-------------|----------|
| Human-readable spec | `hashi/specs/ete/README.md` |
| Manifest JSON Schema | `hashi/specs/ete/v1/ete-manifest.schema.json` |
| Artefact meta JSON Schema | `hashi/specs/ete/v1/artefact-meta.schema.json` |
| Project JSON Schema | `hashi/specs/ete/v1/project.schema.json` |
| Task JSON Schema | `hashi/specs/ete/v1/task.schema.json` |
| Shared Pydantic types | `hashi/specs/ete/v1/ete_types.py` |
| Changelog | `hashi/specs/ete/CHANGELOG.md` |

The JSON Schemas serve double duty: spec definition and runtime validation. All consumers can reference them directly.

### Phase 1 -- Historical Alignment (Weeks 2-3)

**Goal:** Fix existing incompatibilities so current data can be exchanged.

#### 1.1 Kasumi MIME Types

**Minato** -- Add canonical MIME as primary, keep legacy as accepted alias:

| File | Change |
|------|--------|
| `minato/backend/app/minato/plugins/kasumi/nexcel_adapter.py` | `mime_types = ["application/vnd.hashi.kasumi.nexcel+json", "application/vnd.minato.nexcel+json"]` |
| `minato/backend/app/minato/plugins/kasumi/wordo_adapter.py` | Same pattern for wordo |

**Veritas** -- Update Kasumi integration frontend to use canonical MIME on creation; accept legacy on read.

#### 1.2 Kasumi Envelope Structure

**Minato** -- `minato/backend/app/minato/plugins/kasumi/mcp_client.py`: wrap flat payloads in versioned envelope structure (`envelope_version: 1`) when creating Kasumi artefacts.

#### 1.3 Correlation ID in Veritas

**Veritas** -- `Veritas/backend/app/models.py` (Artifact model): add optional `correlation_id` column. On ETE export, generate deterministic ID from `f"{session_id}:{run_id}:{id}"` if none exists. Existing rows remain null internally.

#### 1.4 Shared Artefact Metadata

Create `hashi/specs/ete/v1/ete_types.py` with Pydantic models:

```python
class ETEArtefactMeta(BaseModel):
    artefact_id: str
    title: str
    mime_type: str
    artefact_type: str
    content_hash: Optional[str]
    size_bytes: int
    envelope_version: int = 0
    correlation_id: Optional[str]
    source: dict
    created_at: str
    updated_at: str
    extra: dict = {}
```

Both Minato and Veritas import this type for their ETE adapters (they already depend on HASHI for Nagare).

### Phase 2 -- Minimal Implementation (Weeks 3-5)

**Goal:** Each system gets `ete_export()` and `ete_import()`.

#### 2.1 HASHI -- Reference Codec

Location: `hashi/specs/ete/v1/codec.py`

```python
def export_ete(output_dir: Path, *,
               project=None, tasks=None, artefacts=None,
               workflows=None, knowledge_blocks=None) -> Path:
    """Write an ETE bundle directory."""

def import_ete(ete_path: Path) -> ETEBundle:
    """Read an ETE bundle, validating against schema."""

def validate_ete(ete_path: Path) -> list[ValidationError]:
    """Validate without importing. Returns empty list if valid."""
```

**Important: Schema vs. Codec separation.** The JSON Schemas in `specs/ete/v1/*.schema.json` are the **standard** — any language, any system can validate against them without depending on HASHI's Python package. The `codec.py` and `ete_types.py` are a **reference implementation** — convenient for Python consumers, but not mandatory. Consumers may:

1. **Depend on HASHI codec** (easiest, recommended for Python consumers already using HASHI)
2. **Vendor the schemas only** (copy `*.schema.json` into their repo, validate with any JSON Schema library)
3. **Implement from spec** (use `specs/ete/README.md` + schemas as the contract, write their own codec)

This ensures ETE's evolution is not bottlenecked by HASHI's release cycle. A consumer pinning schemas at v1.0.0 is not forced to upgrade HASHI when HASHI ships unrelated changes.

#### 2.2 Minato -- ETE Adapter

Location: `minato/backend/app/minato/plugins/ete_adapter.py`

Mappings:
- Shimanto `Project` / `Task` -> `ETEProject` / `ETETask`
- Kasumi `ArtefactRecord` -> `ETEArtefactMeta` (with canonical MIME)
- Nagare `WorkflowSnapshot` -> ETE workflow YAML (pass-through, already Nagare format)

#### 2.3 Veritas -- ETE Adapter

Location: `Veritas/backend/app/ete_adapter.py`

Mappings:
- `Artifact` model -> `ETEArtefactMeta` (id, content_hash, mime_type, artifact_type, artifact_meta)
- Kasumi Nexcel/Wordo -> artefacts with canonical MIME + versioned envelope
- Obsidian knowledge blocks (`{correlation_id}/` directories) -> ETE `knowledge_blocks/` (copy structure as-is)

#### 2.4 Golden Test Fixtures

Location: `hashi/tests/ete/fixtures/`

| Fixture | Contents | Tests |
|---------|----------|-------|
| `minimal.ete/` | ete.json + 1 text artefact | Minimum viable bundle |
| `full-project.ete/` | project + tasks + artefacts + workflow + knowledge block | All sections |
| `kasumi-nexcel.ete/` | Nexcel artefact with versioned envelope | Kasumi round-trip |

#### 2.5 Roundtrip Tests

| Test | Description |
|------|-------------|
| Schema validation | Every fixture validates against JSON Schema |
| Self-roundtrip | export -> import -> export produces **semantically equivalent** output (see below) |
| Cross-system roundtrip | Minato export -> Veritas import -> Veritas export -> diff (core fields survive) |

**Semantic equivalence** (not byte-identical): Two ETE bundles are equivalent if:
- All core fields have the same values (JSON key order irrelevant)
- All `content_hash` values match (payload integrity preserved)
- All `extra` / unknown fields are preserved (not dropped)
- Timestamps may differ by formatting (e.g., `Z` vs `+00:00`) but represent the same instant
- YAML key order and formatting may differ
- File names within artefact directories may differ (e.g., `payload.json` vs `payload.nexcel.json`), but `meta.json` content matches

### Phase 3 -- Drift Prevention (Ongoing from Week 5)

**Goal:** Ensure independent development of Minato and Veritas never breaks ETE compatibility.

#### 3.1 CI Validation

**HASHI CI:**
- JSON Schema lint (schemas are valid)
- Fixture validation (all golden fixtures pass schema)
- Codec self-roundtrip test

**Minato / Veritas CI (mandatory for first-party consumers):**
- `validate_ete()` against golden fixtures from HASHI
- Export a sample project -> validate against schema
- **Cross-roundtrip test** (mandatory): export from own system -> validate against schema -> import into own system -> verify no data loss on core fields. For Minato and Veritas specifically, a shared integration test also runs: Minato export -> Veritas import + Veritas export -> Minato import

#### 3.2 Semver Rules

| Version bump | When | Reader impact |
|-------------|------|---------------|
| **Patch** (1.0.x) | Documentation fixes only | None |
| **Minor** (1.x.0) | New optional fields, new content types | Old readers work (ignore unknown fields) |
| **Major** (x.0.0) | Removed/renamed fields, structural changes | Old readers may break |

**Import version check:** `major must match; minor >= bundle minor`.

#### 3.3 Schema Change Protocol

Any change to `specs/ete/v1/*.schema.json`:

1. **PR in HASHI** with updated schema + updated fixtures + CHANGELOG entry
2. **Companion PRs** in Minato and Veritas updating their adapters
3. **All three CI pipelines green** before any PR merges
4. **Major version bumps** require explicit sign-off from all active consumers

### Phase 4 -- Future System Onboarding

Any new system joining the HASHI ecosystem:

1. Obtain the ETE schema (one of: depend on HASHI Python package, vendor `*.schema.json` files, or implement from spec)
2. Implement `ete_export()` mapping internal models to ETE types
3. Implement `ete_import()` mapping ETE types to internal models (respecting import semantics in §2.7)
4. Add golden fixture validation to CI (fixtures available at `hashi/tests/ete/fixtures/`)
5. Register system name in ETE producer convention list (documented in `specs/ete/README.md`)

No plugin architecture unification required. ETE is a **data format**, not a runtime protocol. No HASHI Python dependency required — only schema compliance.

---

## 5. Governance

| Role | Owner | Responsibility |
|------|-------|----------------|
| ETE spec & schema | HASHI repo maintainer | Approve all schema changes |
| Minato adapter | Minato team | Keep adapter passing ETE CI |
| Veritas adapter | Veritas team | Keep adapter passing ETE CI |
| Version classification | HASHI maintainer | Decide major/minor/patch for each change |
| New content types | Any team (proposer) | File RFC as HASHI issue; HASHI maintainer approves |

**Rule:** Breaking changes (major bumps) require sign-off from **all active consumers** before merge.

### 5.1 RFC Process for Schema Changes

To prevent governance bottlenecks as consumer count grows:

| Change Level | Process | SLA |
|-------------|---------|-----|
| **Patch** (docs only) | Direct PR to HASHI, single reviewer | 2 business days |
| **Minor** (new optional fields) | RFC issue in HASHI repo describing the field, rationale, and which consumers need it. HASHI maintainer approves. | 5 business days |
| **Major** (breaking change) | RFC issue + design document. All active consumers review and sign off. Migration guide required. | 10 business days |

**Who can propose:** Any team with an active ETE adapter (currently: Minato, Veritas).

**Escalation:** If SLA is exceeded, the proposer can tag the RFC as `ete-blocked` and escalate to project owner (Father).

---

## 6. Concrete Example: Veritas Export -> Minato Import

To illustrate how ETE works end-to-end:

**Veritas exports an academic project:**

```
q2-lit-review.ete/
  ete.json                          # producer: veritas 3.0.0
  project.json                      # name, status, tags
  artefacts/
    art_001/
      meta.json                     # mime: vnd.hashi.kasumi.nexcel+json, envelope_version: 1
      payload.nexcel.json           # versioned Kasumi envelope
    art_002/
      meta.json                     # mime: application/pdf
      payload.pdf                   # source paper
  workflows/
    inject_quantitative.yaml        # x-ete-source-dialect: veritas
  knowledge_blocks/
    corr_abc123/
      bundle_manifest.json
      Smith_2023_Climate_Analysis.md
      Smith_2023_Climate_Analysis.json
      Smith_2023_Climate_Analysis.pdf
```

**Minato imports it:**

1. Reads `ete.json` -> confirms `ete_version` major = 1 (compatible)
2. Reads `project.json` -> creates Shimanto Project (maps `extra` to internal metadata)
3. Reads each `artefacts/{id}/meta.json`:
   - `nexcel` -> registers as Kasumi artefact via ArtefactPlugin
   - `pdf` -> stores in local warehouse via FileSystemPlugin
4. Reads `workflows/` -> sees `x-ete-source-dialect: veritas`, stores but does not execute (unknown dialect)
5. Reads `knowledge_blocks/` -> stores markdown + JSON in warehouse for RAG context

**Result:** Project appears in Minato with all artefacts accessible. The Veritas-dialect workflow is preserved but marked as non-executable. No errors.

---

## 7. Files to Create / Modify

### New files (HASHI)

```
hashi/specs/ete/
  README.md
  CHANGELOG.md
  v1/
    ete-manifest.schema.json
    artefact-meta.schema.json
    project.schema.json
    task.schema.json
    ete_types.py
    codec.py
hashi/tests/ete/
  fixtures/
    minimal.ete/
    full-project.ete/
    kasumi-nexcel.ete/
  test_ete_codec.py
  test_ete_roundtrip.py
```

### New files (Minato)

```
minato/backend/app/minato/plugins/ete_adapter.py
minato/backend/tests/test_ete_adapter.py
```

### New files (Veritas)

```
Veritas/backend/app/ete_adapter.py
Veritas/backend/tests/test_ete_adapter.py
```

### Modified files

| File | Change |
|------|--------|
| `minato/.../kasumi/nexcel_adapter.py` | Canonical MIME type |
| `minato/.../kasumi/wordo_adapter.py` | Canonical MIME type |
| `minato/.../kasumi/mcp_client.py` | Versioned envelope wrapping |
| `Veritas/backend/app/models.py` | Add `correlation_id` to Artifact |

---

## Appendix: Relationship to Existing HASHI Specs

- **ROUND_TRIP_CONTRACT.md** -- ETE follows the same "reader be liberal, writer be strict" philosophy
- **NAGARE_FLOW_SYSTEM.md** -- ETE workflow YAML is Nagare's canonical format
- **HABIT_BASED_SELF_IMPROVEMENT_PLAN.md** -- No direct overlap; habits are agent-internal, not exchanged via ETE

---

_This document will be updated as ETE evolves. All schema changes tracked in `specs/ete/CHANGELOG.md`._
