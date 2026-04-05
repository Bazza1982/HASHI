# ETE (Epistula Tegami Exchange) Protocol Specification

> **Owner:** HASHI
> **Version:** 1.0.0
> **Date:** 2026-04-03

---

## 1. Overview

ETE is a versioned, directory-based interchange format owned by HASHI for exchanging project data between HASHI-ecosystem applications. A conforming ETE bundle contains a manifest, optional project metadata, task records, artefacts, workflows, and knowledge blocks. Any compliant producer can write a bundle and any compliant consumer can read it without coordination beyond schema conformance. ETE is a data format, not a runtime protocol; no HASHI library dependency is required for compliance.

---

## 2. Design Principles

| Principle | Rule |
|-----------|------|
| **Reader be liberal** | Unknown fields are silently ignored; they never cause errors. |
| **Writer be strict** | Only write fields defined in the current ETE version. |
| **Append-only evolution** | New fields are always optional. Existing fields are never removed or renamed within a major version. |
| **Correlation ID always present** | Systems that lack correlation IDs must generate one on export. |
| **Nagare is canonical workflow** | Non-Nagare workflow dialects are preserved verbatim with an `x-ete-source-dialect` annotation. |

---

## 3. Bundle Directory Structure

```
{bundle_name}.ete/
  ete.json                          # REQUIRED  manifest
  project.json                      # optional  project metadata
  tasks/
    {task_id}.json                  # optional  task records
  artefacts/
    {artefact_id}/
      meta.json                     # artefact metadata
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

The only **required** file is `ete.json`. All other sections are optional and declared in the manifest's `contents` array.

---

## 4. ete.json (Manifest)

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
| `ete_version` | string (semver) | yes | ETE spec version used to produce this bundle. |
| `producer.system` | string | yes | Producing system identifier. |
| `producer.version` | string | yes | Producing system version. |
| `produced_at` | string (ISO 8601) | yes | Timestamp of export. |
| `contents` | string[] | yes | Sections present in this bundle. Valid values: `project`, `tasks`, `artefacts`, `workflows`, `knowledge_blocks`. |

JSON Schema: [`v1/ete-manifest.schema.json`](v1/ete-manifest.schema.json)

---

## 5. Artefact meta.json

Each artefact directory `artefacts/{artefact_id}/` contains a `meta.json` and at least one `payload.*` file.

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

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `artefact_id` | string | yes | Unique artefact identifier. |
| `title` | string | yes | Human-readable name. |
| `mime_type` | string | yes | MIME type of the payload. Use canonical Kasumi MIME types (see section 9). |
| `artefact_type` | string | yes | Short type key (e.g., `nexcel`, `wordo`, `pdf`, `markdown`). |
| `content_hash` | string | no | `sha256:{hex}` hash of the payload file. Used for deduplication and integrity. |
| `size_bytes` | integer | yes | Payload file size in bytes. |
| `envelope_version` | integer | yes | Kasumi envelope version. `0` for legacy/non-Kasumi payloads. |
| `correlation_id` | string | no | Cross-system correlation identifier. |
| `source` | object | yes | Origin system metadata. Must contain `system` (string). Other keys are system-specific. |
| `created_at` | string (ISO 8601) | yes | Creation timestamp. |
| `updated_at` | string (ISO 8601) | yes | Last modification timestamp. |

JSON Schema: [`v1/artefact-meta.schema.json`](v1/artefact-meta.schema.json)

---

## 6. project.json

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

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `project_id` | string | yes | Unique project identifier. |
| `name` | string | yes | Human-readable project name. |
| `description` | string | no | Project description. |
| `status` | string | yes | Current status (free-form; consumers decide which values they support). |
| `priority` | string | no | Priority level. |
| `owner` | string | no | Owner identifier. |
| `created_at` | string (ISO 8601) | yes | Creation timestamp. |
| `updated_at` | string (ISO 8601) | yes | Last modification timestamp. |
| `tags` | string[] | no | Free-form tags. |
| `extra` | object | no | System-specific metadata. ETE preserves but does not interpret this field. |

JSON Schema: [`v1/project.schema.json`](v1/project.schema.json)

---

## 7. Task Record

Each file in `tasks/` is named `{task_id}.json`.

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

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task_id` | string | yes | Unique task identifier. |
| `project_id` | string | yes | Parent project identifier. |
| `name` | string | yes | Human-readable task name. |
| `status` | string | yes | Current status. |
| `priority` | string | no | Priority level. |
| `assignee` | string | no | Assigned agent or user. |
| `created_at` | string (ISO 8601) | yes | Creation timestamp. |
| `completed_at` | string (ISO 8601) | no | Completion timestamp. |
| `depends_on` | string[] | no | List of `task_id` values this task depends on. |
| `extra` | object | no | System-specific metadata. |

JSON Schema: [`v1/task.schema.json`](v1/task.schema.json)

---

## 8. Knowledge Block Constraints

### Allowed File Types

| Extension | Purpose |
|-----------|---------|
| `.md` | Markdown notes with frontmatter |
| `.json` | Structured chunk data |
| `.pdf` | Source documents |
| `.png`, `.jpg`, `.jpeg`, `.svg` | Embedded images referenced in markdown |

All other file types are rejected on import.

### Forbidden Content

- No executable files (`.exe`, `.sh`, `.bat`, `.py`, `.js`, etc.)
- No symlinks (resolved or rejected, never followed)
- No hidden files (names starting with `.`)

### Path Constraints

- Maximum one level of subdirectory within each `{correlation_id}/` block.
- File names must be valid on Windows, macOS, and Linux: no `<>:"/\|?*` characters, maximum 200 characters.
- No path traversal (`..` segments are rejected).

### Size Constraints

| Scope | Limit |
|-------|-------|
| Single file | 200 MB |
| Single knowledge block (all files under one `{correlation_id}/`) | 500 MB |
| Total `knowledge_blocks/` section | 5 GB |

### Import Behaviour

Files that violate these constraints are skipped with a **Warning**, not a fatal error. The knowledge block is imported with the valid files only.

---

## 9. Canonical Kasumi MIME Types

### Canonical Types (writers must use)

| Kasumi Type | MIME Type |
|-------------|-----------|
| Nexcel | `application/vnd.hashi.kasumi.nexcel+json` |
| Wordo | `application/vnd.hashi.kasumi.wordo+json` |

### Legacy Aliases (readers must accept)

| Legacy MIME Type | Canonical Equivalent |
|------------------|---------------------|
| `application/vnd.minato.nexcel+json` | `application/vnd.hashi.kasumi.nexcel+json` |
| `application/x-kasumi-nexcel+json` | `application/vnd.hashi.kasumi.nexcel+json` |
| `application/vnd.minato.wordo+json` | `application/vnd.hashi.kasumi.wordo+json` |
| `application/x-kasumi-wordo+json` | `application/vnd.hashi.kasumi.wordo+json` |

Writers must always emit canonical types. Readers must normalize legacy aliases to canonical form on import.

---

## 10. Kasumi Versioned Envelope Format

ETE adopts Veritas's versioned envelope as the canonical Kasumi payload structure:

```json
{
  "kasumi_type": "nexcel",
  "envelope_version": 1,
  "kasumi_version": "1.2.0",
  "created_at": "2026-04-03T10:00:00Z",
  "updated_at": "2026-04-03T12:30:00Z",
  "table": { ... }
}
```

- Systems that previously used flat payloads (e.g., Minato) must wrap content in this structure on ETE export.
- On import, if `envelope_version` is missing, treat the payload as `envelope_version: 0` (legacy format).
- The `kasumi_type` field must match the `artefact_type` in the corresponding `meta.json`.

---

## 11. Workflow YAML

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

Workflows originating from non-Nagare systems are preserved verbatim with an `x-ete-source-dialect` annotation:

```yaml
x-ete-source-dialect: veritas
workflow_id: inject_quantitative
steps:
  - step_id: classify
    adapter_name: prompt_contract
    config: { ... }
```

Readers that do not understand a given dialect must skip the workflow gracefully (Recoverable severity). The workflow file is preserved in the bundle but marked as non-executable.

---

## 12. Import Semantics

### 12.1 Duplicate Handling

| Entity | Duplicate Detection Key | Default Behaviour |
|--------|------------------------|-------------------|
| Project | `project_id` match | **Skip** -- existing project not overwritten |
| Task | `task_id` match within same `project_id` | **Skip** -- existing task preserved |
| Artefact | `artefact_id` match OR `content_hash` match | **Skip** -- existing artefact preserved |
| Knowledge Block | `correlation_id` match | **Skip** -- existing block preserved |
| Workflow | `workflow_id` match | **Replace** -- workflows are declarative; latest wins |

Consumers may offer a `conflict_policy` parameter (`skip | replace | fail`) to override the defaults. When `fail` is chosen, the import aborts at the first duplicate.

### 12.2 Idempotency

Importing the same ETE bundle twice with default conflict policy must produce the same end state as importing it once. No duplicate records, no side effects.

### 12.3 Section-Atomic Rollback

ETE import is section-atomic: each top-level section (`project`, `tasks`, `artefacts`, `workflows`, `knowledge_blocks`) is imported as a unit.

- If a section fails, it is fully rolled back (no partial artefacts, no partial task lists).
- Previously completed sections are kept (not rolled back).
- The import returns a result object listing per-section status:

```
ETEImportResult:
  success: bool               # True only if ALL sections succeeded
  sections: dict[str, SectionResult]
  errors: list[ETEImportError]

SectionResult:
  status: "ok" | "skipped" | "failed"
  imported_count: int
  skipped_count: int           # duplicates skipped
  error: optional string
```

### 12.4 Data Integrity

Importers must never silently lose data. Every skipped record must appear in `skipped_count`. Every section failure must surface in `errors`. The caller decides whether partial success is acceptable.

---

## 13. Bundle Integrity Rules

The `contents` array in `ete.json` declares which sections are present. Each declared section must meet minimum structural requirements:

| Section | Minimum Requirements |
|---------|---------------------|
| `project` | `project.json` exists and validates against schema. |
| `tasks` | `tasks/` directory exists with at least one `{task_id}.json` that validates. |
| `artefacts` | `artefacts/` directory exists; each subdirectory contains a valid `meta.json` and at least one `payload.*` file. |
| `workflows` | `workflows/` directory exists with at least one `.yaml` file. |
| `knowledge_blocks` | `knowledge_blocks/` directory exists; each subdirectory contains a `bundle_manifest.json`. |

### Error Codes

| Code | Meaning |
|------|---------|
| `E_MANIFEST_INVALID` | `ete.json` missing or fails schema validation. |
| `E_SECTION_DECLARED_BUT_MISSING` | Section listed in `contents` but directory/file absent. |
| `E_SECTION_UNDECLARED_BUT_PRESENT` | Directory exists but not listed in `contents`. Warning only. |
| `E_ARTEFACT_NO_META` | Artefact directory exists but has no `meta.json`. |
| `E_ARTEFACT_NO_PAYLOAD` | Artefact has `meta.json` but no payload file. |
| `E_SCHEMA_VALIDATION` | A JSON file fails its schema check. |
| `E_KB_NO_MANIFEST` | Knowledge block directory has no `bundle_manifest.json`. |

`validate_ete()` returns all errors. Import must refuse to proceed if any Fatal-severity error is present.

---

## 14. Error Severity Levels

| Severity | Behaviour | Examples |
|----------|-----------|---------|
| **Fatal** | Operation aborts, section rolled back. | Invalid `ete.json`, version mismatch, schema validation failure, corrupt payload, `content_hash` mismatch. |
| **Recoverable** | Item skipped, operation continues. | Unknown MIME type, unsupported workflow dialect, single artefact missing payload, disallowed knowledge block file type, duplicate ID (default policy). |
| **Warning** | Logged, no action taken. | Undeclared section present (`E_SECTION_UNDECLARED_BUT_PRESENT`), unknown extra fields, file permission differences. |

### Specific Error Classification

| Situation | Severity |
|-----------|----------|
| `ete_version` major mismatch | Fatal |
| `ete_version` minor too new (reader older than bundle) | Fatal |
| Unknown field in any JSON | Warning |
| Unknown MIME type in artefact | Recoverable |
| Unknown workflow dialect | Recoverable |
| Knowledge block file type not allowed | Recoverable |
| Duplicate ID (default policy) | Recoverable |
| Duplicate ID (`conflict_policy: fail`) | Fatal |
| Payload `content_hash` mismatch | Fatal |

Importers must collect and return all issues, not just the first fatal. This allows callers to see the full picture before deciding on next steps.

---

## 15. ZIP Packaging Rules

When a bundle directory is packaged for transport, the following rules apply.

### File Extension

`.ete`

### Structure

- The ZIP must contain a **single root directory** named `{bundle_name}.ete/`.
- `ete.json` must be at `{bundle_name}.ete/ete.json` (not at ZIP root, not nested deeper).

### Encoding

- File names: UTF-8.
- Path separator: forward slash `/` (even on Windows).
- No absolute paths.

### Compression

- Algorithm: DEFLATE (standard ZIP).
- No encryption. ETE bundles are not a security boundary; use transport-level encryption.

### Forbidden

- Symlinks within ZIP.
- Files outside the root directory (path traversal via `../`).
- ZIP bombs: compression ratio exceeding 100:1 triggers rejection.

### Size

No hard limit on ZIP size. Importers may reject bundles exceeding their configured maximum. Importers must extract to a temporary directory and validate before committing.

---

## 16. Semver Rules for ETE Version

| Version Bump | When | Reader Impact |
|-------------|------|---------------|
| **Patch** (1.0.x) | Documentation fixes only. | None. |
| **Minor** (1.x.0) | New optional fields, new content types. | Old readers work (unknown fields ignored per design principle). |
| **Major** (x.0.0) | Removed/renamed fields, structural changes. | Old readers may break. |

### Import Version Check

```
major must match AND reader_minor >= bundle_minor
```

A reader supporting ETE 1.2.0 can read bundles at 1.0.0 through 1.2.x. It must reject bundles at 1.3.0 or 2.0.0.

---

## 17. Producer Registration

Known producer system names for the `producer.system` field:

| System Name | Description |
|-------------|-------------|
| `minato` | Minato agentic AI OS |
| `veritas` | Veritas research platform |
| `hashi` | HASHI platform layer (reference codec, test fixtures) |

New systems joining the HASHI ecosystem should register their system name by submitting a PR to this specification.

---

## 18. JSON Schemas

Machine-readable JSON Schemas for all ETE data types are in the [`v1/`](v1/) directory:

| Schema | File |
|--------|------|
| Manifest | `v1/ete-manifest.schema.json` |
| Artefact metadata | `v1/artefact-meta.schema.json` |
| Project | `v1/project.schema.json` |
| Task | `v1/task.schema.json` |

These schemas are the normative definition. Any language or system can validate against them without depending on HASHI's Python package. The Pydantic types in `v1/ete_types.py` and the reference codec in `v1/codec.py` are convenience implementations, not required for compliance.
