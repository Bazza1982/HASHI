# ETE Changelog

All notable changes to the ETE specification are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/). ETE uses [Semantic Versioning](https://semver.org/).

---

## [1.0.0] -- 2026-04-03

Initial release of the Epistula Tegami Exchange protocol.

### Added

- **Bundle format:** directory-based interchange with `ete.json` manifest, optional `project.json`, `tasks/`, `artefacts/`, `workflows/`, and `knowledge_blocks/` sections.
- **Manifest specification:** `ete.json` with `ete_version`, `producer`, `produced_at`, and `contents` fields.
- **Artefact metadata:** `meta.json` per artefact with `artefact_id`, `mime_type`, `content_hash`, `envelope_version`, `correlation_id`, and source provenance.
- **Project and task record formats** with `extra` field for system-specific metadata passthrough.
- **Canonical Kasumi MIME namespace:** `application/vnd.hashi.kasumi.nexcel+json` and `application/vnd.hashi.kasumi.wordo+json`, with legacy alias acceptance for `application/vnd.minato.*` and `application/x-kasumi-*` forms.
- **Kasumi versioned envelope format** (`envelope_version: 1`) as canonical payload structure.
- **Workflow YAML:** Nagare schema as canonical format; non-Nagare dialects preserved with `x-ete-source-dialect` annotation.
- **Knowledge block constraints:** allowed file types (`.md`, `.json`, `.pdf`, `.png`, `.jpg`, `.jpeg`, `.svg`), size limits (200 MB per file, 500 MB per block, 5 GB total), path safety rules.
- **Import semantics:** skip-by-default duplicate handling for projects, tasks, artefacts, and knowledge blocks; replace-by-default for workflows. Optional `conflict_policy` override (`skip | replace | fail`). Idempotent import guarantee.
- **Section-atomic rollback:** each top-level section imported as a unit; failing sections are fully rolled back while completed sections are retained.
- **Bundle integrity validation:** `contents`-declared sections must meet structural requirements. Seven error codes defined (`E_MANIFEST_INVALID`, `E_SECTION_DECLARED_BUT_MISSING`, `E_SECTION_UNDECLARED_BUT_PRESENT`, `E_ARTEFACT_NO_META`, `E_ARTEFACT_NO_PAYLOAD`, `E_SCHEMA_VALIDATION`, `E_KB_NO_MANIFEST`).
- **Error severity classification:** Fatal (abort and rollback), Recoverable (skip item, continue), Warning (log only). Specific classification table for version mismatches, unknown fields, unknown MIME types, hash mismatches, and duplicate handling.
- **ZIP packaging specification:** `.ete` extension, single root directory, UTF-8 file names, DEFLATE compression, no symlinks, no ZIP bombs (100:1 ratio limit), no path traversal.
- **Semver rules:** patch for docs, minor for new optional fields, major for breaking changes. Import version check: major must match, reader minor >= bundle minor.
- **Design principles:** reader be liberal, writer be strict, append-only evolution.
- **Producer registration:** `minato`, `veritas`, `hashi` as initial known system names.
- **JSON Schemas:** `v1/ete-manifest.schema.json`, `v1/artefact-meta.schema.json`, `v1/project.schema.json`, `v1/task.schema.json`.
