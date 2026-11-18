# HASHI Office Toolkit — Function contract

Status: implemented on `feature/office-toolkit`. Adoption is explicit and instance-scoped. Source, qualified artifacts, running Workers, rendered previews and channel delivery are separate facts. Production adoption is outside this change.

## Ownership and boundaries

**PAO** owns authorization, source acquisition, working-file handles, transactional operations and execution evidence in **Functions**. Platform/instance configuration selects a shared, isolated Office Runtime. HERV3 and CLI Engines consume the existing ToolRegistry; the Frontend Connector owns publication and delivery. Workbench remains a file-library/preview consumer.

Core, its protocols/selectors, existing Functions locks and installed sealed Python environments are unchanged. Office libraries are imported only in the isolated worker. No model-selected interpreter, package installation, arbitrary code, second MCP server, second inference service, recipient selector or Office-specific delivery ledger exists.

Ordinary/L0 execution retains allowed-tools, read-only invocation policy, authorized Workzone roots, enterprise path gates and live Run fencing. Opaque handles are scoped by instance, Agent and Session; calls bind request/call identity and arguments. L1/L2 managed Office channels are **not qualified**: their catalogues hide Office and existing managed dispatch fails closed. There is no fallback to ordinary execution. Process/environment isolation is not a new same-SID OS sandbox; do not claim it supplies L1/L2 isolation.

## Five canonical tools

The sole public schema owner is `tools/office/contract.py`, imported by `tools/schemas.py`.

| Tool | Inputs and behavior |
| --- | --- |
| office_inspect | Exactly one of path, source=last_attachment, source=list_attachments, attachment_id or file_id. List mode returns visible Office attachment IDs with a continuation cursor; selection and listing use canonical history for the same owner/Agent and current message boundary. An explicit ID must occur in a visible user message. |
| office_read | file_id, optional target/view or returned cursor. Cells, paragraphs/headings, native Word tables, slide shapes/tables/charts, or PDF text. Long or wide content can use cell_fragments, paragraph_fragments, table_fragments, page_fragments or slide_fragments; follow next and assemble fragments using their location and character offset. A cursor is bound to the original request, session, and working-file revision. Structural summaries disclose truncation and suggest next; paragraph results with more tables provide tables_next for the remaining table summaries. |
| office_create | xlsx/docx/pptx, new output, content/source_id/template, optional style. Create a working copy; the output does not exist until finish. |
| office_edit | file_id, one action, optional target/content/source_id. Stage and validate a new immutable version, compare the prior revision, then commit the handle. Originals are retained. |
| office_finish | file_id, optional new output, preview/deliver. Reopen, publish verified bytes without overwriting existing files, optionally render and use the existing originating-conversation publication service. |

Content is bounded CSV, Markdown or JSON **text**, never executable Python. Rich recipes use small JSON content instead of adding nested public schemas.

Responses share status, code, file_id, summary, data, next and checks. Checks independently report saved, structure, recalculation, visual and delivered. A successful write is not recalculation; a rendered preview is not visual approval; persisted/queued publication is not confirmed delivery. Read responses identify cached values versus formula text. Errors retain valid handles only, and give an actionable correction or reconciliation step.

Representative targets: `Sales!A1:D20`, `paragraph:1-20`, `table:1!1:30`, `slide:1`, `slide:1/shape:3` (edits), `page:1-3`.

## Isolated, locked runtime

- CPython 3.12, Windows x64 is the currently qualified platform. `requirements/office-runtime.in` declares the independent profile; `office-runtime-windows-py312.lock` pins every binary distribution and SHA256 in its complete dependency closure.
- Install the lock into a **new** explicit environment using binary wheels and `--require-hashes`. Never synchronize a repository/Core/Functions environment. Changing dependencies requires a new qualification directory, not in-place upgrades.
- Operator command `scripts/office_runtime.py prepare` verifies installed versions/RECORD hashes, copies worker code into a new generation, hashes its libraries, Python launcher and configured converter closure, then performs real isolated imports/health. The manifest binds protocol 1, platform, Python version and lock digest.
- `health` and `activate` require an explicit manifest, state_root and absolute work_root. The runtime uses paths supported by the actual platform and Office libraries. Activation uses `orchestrator.config_json` and its observed revision; conflicts/durability errors are not blindly retried. Rollback activates a previously qualified manifest through the same operation.
- Ordinary shared selection is `tool_options.office`, or an absolute `HASHI_OFFICE_RUNTIME_CONFIG` pointing to that configuration. CLI Gateway context freezes these operator options. Runtime availability requires the matching qualified health receipt; execution rechecks the hashed closure.
- Each call gets a private temporary transport directory under work_root. Durable candidates, handles and receipts stay under state_root. Large requests and responses use identity-bound, hash-checked files in that directory; the IPC envelope remains bounded. Older qualified generations need requalification for large transport. No C/PATH fallback.
- Child environment contains no inherited provider/Bot credentials or PYTHONPATH. Explicit temporary, profile and cache paths remain in the configured work directory. Request identity and response checksums are checked. The positive configured timeout is honored without a fixed upper clamp; timeout/cancellation cleans the owned process tree before returning.
- Rendering/calculation is optional and independent. The locally tested adapter is official LibreOffice 26.2.7.3, separately extracted to D and hash-qualified, using its console entry and per-call UserInstallation. No COM, MSI registration or native Microsoft Office process is required. Record the installer source, version and official checksum when provisioning.
- Model-facing tools cannot activate environments or alter runtime configuration. Operators validate the storage policy, volume identity/space and peak budget before provisioning. Shared installed packages do not grant shared document access.

Example operator arguments (replace explicit qualified paths; never run against a live environment):

```text
prepare --python <new-office-env>\Scripts\python.exe --destination <new-generation> --converter <converter>\program\soffice.com
health --manifest <generation>\manifest.json --state-root <nonproduction-state> --work-root D:\AI\Temp\HASHI-Office
activate --manifest <generation>\manifest.json --state-root <nonproduction-state> --work-root D:\AI\Temp\HASHI-Office --config <instance-office.json> --minimum-free-bytes <policy-reserve>
```

## Supported operations and recipes

| Area | Implemented behavior |
| --- | --- |
| Excel / UTF-8 CSV | Inspect sheets; bounded values/formula reads; create multiple sheets; set cells/formulas; append; fonts, fills, number formats and wrapping; native bar/line/pie charts; clean, grouped sum/count/average, keyed reconciliation. Long numeric identifiers stay text. |
| Word | Markdown headings, bullets and native pipe tables; JSON sections/tables/PNG/JPEG images; exact run-preserving text replacement; add/set paragraphs, tables and images; professional/academic/clean presets; clone or append to authorized templates. |
| PowerPoint | Markdown outlines or JSON slides; native text, tables, images and charts; indexed text/table/chart edits; append slides; presets and aspect-fit/grid layout. Template-only cloning preserves bytes; edited packages must retain existing parts/media. |
| PDF / conversion | Inspect page count, bounded text extraction, render; Office-to-PDF through the qualified adapter; XLSX/CSV conversion; explicit XLSX conversion for converter recalculation; PDF-to-DOCX text extraction. PDF extraction does not preserve original layout. |

Named actions are `excel_summary`, `excel_reconcile`, `excel_clean`, `excel_chart`, `word_report`, `word_format`, `slides_from_outline`, `office_convert`. Full examples and target syntax live in active `skills/office-basics/SKILL.md` and `references/recipes.md`. The staged documentation location redirects there. Normal SkillManager discovery validates the active package.

## Validation, fidelity and recovery

- Office Runtime derives file bytes, expanded archive bytes, ZIP entry and materialized-cell budgets from available host memory (and a Linux container limit where present). Operators may override any of these via tool_options.office.resource_budgets using positive integer keys file_bytes, expanded_bytes, archive_entries, and materialized_cells. When memory cannot be determined, fallback budgets are 32 MiB, 128 MiB, 2048 entries and 200,000 cells. Budget errors identify the measured amount and configured limit. OOXML preflight keeps archive path, structural, encryption and DTD/entity checks; repeated data is not rejected merely for its compression ratio. Targeted reads return bounded pages; current file staging and workbook parsing still materialize whole files/workbooks, so increased budgets require sufficient memory. The new worker must be requalified to use these budgets.
- Reads and ordinary edits preserve Excel formula text as data, including table references, hyperlinks and quoted pipes. Openpyxl does not recalculate these formulas. A later spreadsheet application may calculate a saved formula when it opens the file.
- The optional converter can evaluate formulas. Until a scoped, isolated evaluation route exists, conversion/preview rejects identified external or dynamic evaluation, connections and formula representations it cannot classify. These documents remain readable/editable without recalculation. This is a conservative conversion limitation, not a claim that the converter is fully sandboxed.
- A simple plain-text Word append uses a package-preserving path: existing hyperlinks, comments, revisions and other parts remain unchanged and external content is not executed. Exact template cloning copies the original bytes. Signed documents require a signature-aware editing route because edits invalidate signatures. Other edit actions can still reject macros, external relationships, tracked changes, comments, ActiveX, signatures, pivot caches and arbitrary embedded objects where fidelity cannot yet be proved. Native PPT chart XLSX parts are allowed only with chart-package relationships and independently validated workbook contents; embedded workbook formulas are checked before conversion. Macro execution is never supported.
- Untouched media/custom XML bytes and existing package parts are checked after ordinary edits. Unsupported fidelity returns a specific error and keeps the prior working version and original. This is a conservative package-preservation check, not general Microsoft Office rendering equivalence.
- Every use checks the scoped working copy and authorized roots. Changed or unavailable original/template/asset provenance is reported separately and does not block an independent working copy; original overwrite remains protected. A save uses a flushed temporary sibling and atomic no-replace publication (hardlink or a supported platform no-replace rename), followed by byte hash readback.
- For input reads, an in-Workzone file or directory alias may resolve to a real object inside an authorized read root. The existing path guard applies to that canonical target, and the gateway opens it through a no-follow bound handle; a replaced parent or an outside-root target cannot redirect the read. Output publication still rejects linked paths until its own exact-target binding is implemented; private runtime/state paths keep their separate checks.
- A configured `minimum_free_bytes` is a write reserve; it does not block inspect/read. New activation defaults to no fixed reserve. Existing configured values remain effective until intentionally changed; actual disk exhaustion reports a storage error and does not publish a partial output.
- SQLite stores immutable version references, revision, provenance, calculation facts and operation receipts. Repeated identical calls replay the receipt; different arguments under the same call ID conflict. Concurrent mutations compare revisions and cannot poison another receipt.
- Cancelled uncommitted operations retire safely. Running intents left by process loss expire after at least five minutes; retired intent fencing prevents late candidate commits. Prepared saves are reconciled by their actual path/hash and never blindly overwritten or duplicated. An uncertain delivery is reported separately from saved bytes.
- Audit/effect receipts and originating-conversation delivery reuse existing HASHI owners. `office_finish(deliver=true)` uses the durable publication API and reports each destination's actual state; model arguments cannot select another recipient.

## Discovery and validation routes

`office_basic` is derived from the canonical tool list. Deterministic attachment/intent hints augment the existing Ollama core catalogue and HERV3 stage catalogue; later tool turns retain relevance. Runtime availability and authorization filter the result. CLI uses the existing Tool Gateway and same executor/authority.

Focused tests: `tests/test_office_toolkit_basics.py`, `test_office_runtime.py`, `test_office_regressions.py`, `test_office_integration.py`. Set an explicit `HASHI_TEST_OFFICE_PYTHON` to a separate locked environment; library-backed tests otherwise skip honestly. Use a D basetemp. Tests cover real generated packages, exact content, formula text, corruption, external formulas, template/media/chart preservation, path/source changes, handle scope, concurrent calls, cancellation, recovery, bounded reads, canonical attachments/audit and an actual CLI MCP subprocess.

Shared registry/gateway changes also run the curated local gate from `docs/TESTING_POLICY.md` and affected direct consumers. Run `scripts/check_protected_core_changes.py --base origin/main` and `git diff --check` from the actual D checkout. No extra hosted full-suite CI or production restart is part of this feature.

Manual `scripts/office_acceptance.py` is never started by pytest/CI. Explicit files mode performs real chart/analysis/template/conversion/rendering workflows and independently checks package XML and cached formula values. Model mode uses real HERV3/provider code, captures offered schemas/tool receipts, verifies native Word table/content bytes and forbids shell/Python tool use. Pass exact existing model/service targets; no downloads or second inference server.

## Current acceptance boundaries

Windows x64, ordinary/L0 Registry, real CLI Gateway and the isolated worker are the supported surface. Local Qwen 9B and Gemma 31B are tested separately; the latter is a stronger control, not evidence for a small Gemma. These are scoped examples, not a broad model success-rate claim. Initial failures and corrected runs remain in local evidence.

L1/L2 execution qualification, WSL/Linux/macOS, native Office COM, general PDF editing/OCR, complex/macro Office fidelity and native Microsoft Office visual equivalence are not claimed. Converter output can change pagination/fonts/formula semantics; unsupported formulas or PDF text ordering need review. Automatic previews render the first page; users may inspect the complete PDF. Existing nonproduction/prod services do not adopt a branch merely because local tests or a PR pass. Telegram/Workbench delivery and external preview require real per-channel evidence; persist/queue/transport acknowledgement/user observation must remain distinct.
