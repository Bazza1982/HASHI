# HASHI Office Toolkit (HOT) — Function design and implementation handoff

Status: **initial scaffold only** on feature/office-toolkit. The five tools are NOT registered or executable yet. No live runtime adoption, deployment, or claim of user-visible Office capability. Follow AGENTS.md, ARCHITECTURE.md, the layered runtime boundaries, and the testing policy.

## Single owner and boundaries

- Functional owner: **PAO** (authorized tool execution, file acquisition, artifact provenance and delivery orchestration). Engineering layer: **Functions**, with platform/instance-owned Office-sidecar configuration.
- HERV3 is an Engine consumer and owns model-facing tool selection; it does **not** own a second Office executor, path policy or audit ledger.
- The Frontend Connector is a delivery consumer. Workbench is a file preview/library consumer, **not** the owner of Office computation.
- The immutable Core-host runtime, protected Core source/protocol, Functions role lock and existing installed environments must remain untouched. No global pip/uv sync, no new Python dependencies in sealed Core or Functions, and no changes to installed selector/manifest.
- Every Agent can opt into the same installed tool set through the existing ToolRegistry authorization and Agent allowed-tools policy. This is **availability**, not automatically unrestricted filesystem access. Preserve the present permission gates and Workzone scoping for each Agent.

## Five canonical model-facing tools

The sole initial definition owner is tools/office/contract.py. It is intentionally not yet imported by tools/schemas.py. The future registered JSON tools are:

1. office_inspect(path OR source=last_attachment OR file_id) — inspect file identity, capabilities and structural summary; return opaque file_id and a concrete next-step example.
2. office_read(file_id, target?, view?) — read a bounded Excel cell range, Word section, PPTX slide or supported PDF part. Return compact data, row/page counts, truncation and continuation.
3. office_create(format=xlsx|docx|pptx, output, content?|source_id?|template?|style?) — create editable file from Markdown, data reference or template. No arbitrary Python source execution.
4. office_edit(file_id, action, target?, content?|source_id?) — atomic incremental edit of an authorized working copy. Start with cells/formulas, add rows/charts, Word sections/style/replace and slide addition. Keep complex transformations in named deterministic recipes, not a giant model schema.
5. office_finish(file_id, output?, preview?, deliver?) — save into a new version, reopen and validate, render when supported and optionally deliver to the originating conversation with confirmed receipt.

Do not make a full nested Office object model available as hundreds of function calls. Prefer the five compact tools, a small set of deterministic recipes and detailed Skills loaded on demand. No silent formula recalculation claim: openpyxl stores formulas but does not calculate them. A save, structural validation, recalculation, visual render and actual delivery are distinct statuses.

Response envelope (all tools):

    {
      "status": "ok | error | partial",
      "code": "STABLE_ERROR_CODE",
      "file_id": "opaque reference when available",
      "summary": "one line",
      "data": "bounded relevant facts, not entire workbook",
      "next": {"tool": "office_read", "arguments": {"file_id": "of_1", "target": "Sheet1!A1:D20"}},
      "checks": {"saved": true, "structure": "passed", "recalculation": "not_performed", "visual": "not_checked", "delivered": false}
    }

For errors, return a specific correction example only when retrying has no uncertain effect. Unknown execution outcomes and repeated edits require reconciliation and idempotency, not blind retry.

## Current starter implementation

- tools/office/contract.py — the five JSON tool definitions, read-only classification and narrow input validation; **validation is not authorization**.
- tools/office/task_signal.py — deterministic, conservative Office attachment/intent hint producing office_basic. It does not grant rights or select an execution environment.
- tools/office/package_probe.py — read-only, bounded OOXML bytes preflight (basic Excel sheet names and PPTX slide-part count) without installing Office libraries; this is **not** complete Office document validation.
- tests/test_office_toolkit_basics.py — focused contract and preflight test cases.
- skills/office-basics/SKILL.md — short, model-friendly operating instructions.

These files must not be mistaken for a working tool gateway. Finish the actual runtime, policy, dispatch, integration and live non-production acceptance before advertising the tools to any model.

## Implementation roadmap

### A. Isolated Office Runtime — one per host, available to authorized Agents

- Implement an isolated Office worker/sidecar launched by an **operator-qualified absolute executable**, not PATH python, model-provided arguments, or an Agent-selected interpreter.
- Select runtime identity through existing platform/instance configuration, never through the protected Core selector.
- Build a separate, independently pinned and hash-locked Office dependency closure in a fresh directory. Candidate packages: openpyxl (verify the locally available 3.1.5), python-docx, python-pptx, Pillow, and optional pandas/xlsxwriter/PyMuPDF. Install LibreOffice headless or native Windows Office automation only as independently configured *render/calculation adapters*, not mandatory baseline.
- Use the existing deployment/runtime qualification conventions, prepare/health-check/activate/rollback. Never modify a live Core or Functions interpreter. Do not make runtime availability contingent on Telegram being connected.
- One shared installed package closure can serve many Agents, but each request must be separately scoped to Agent/Instance/Run/Workzone and backed by isolated temporary files/OS rights. The runtime must not gain a wider read/write root simply because the host is capable of it.
- Define a bounded versioned sidecar IPC contract with request identity, deadline, size limits, file handle/authorized source, policy context, response checksum and effect receipt. No arbitrary import/execution/script injection via tool parameters. Consider large input via trusted staging or file descriptor rather than full file bytes inside JSON.

### B. PAO ToolRegistry and CLI Gateway integration

- Import tool definitions from tools.office.contract into tools/schemas.py **only when the real backend is implemented**; add one office_basic tier in tools/registry.py, with the five tool names derived from OFFICE_TOOL_NAMES.
- Implement an Office Function Gateway as the single dispatcher: authorize every read/write, verify opaque file handles, bind to Agent/Session/Workzone, call the isolated sidecar, reconcile timeouts, and publish durable effects through existing Canonical Audit / effect receipts / File delivery.
- Place office_inspect/read in existing read-only policy classification only after checking all side effects and indirect assets. Treat create/edit/finish as mutations; a read-only planning/delegation context must not be able to invoke them. office_finish may publish to a recipient, so verify origin authority and no accidental owner-mirror delivery.
- Share the exact registered public contract with isolated CLI Engines through the existing Tool Gateway. Do not build a second Office MCP server with independent access control.
- Prefer preserving source by default; output paths are explicitly within authorized write roots, transactional/temp then atomic commit when possible. Detect changed original content and symlinks at commit, never silently overwrite someone else's update.
- The /getfile feature has a separate feature/getfile branch. Office completion can emit ordinary canonical file-modification evidence and a deliverable; do not duplicate its recent-file index or make Office depend on merging that branch.

### C. HERV3 small-model optimisation — mandatory

- OllamaAdapter currently advertises only the core tier by default. Merely adding office_basic to TOOL_TIERS does not make the tools visible. Wire a **deterministic per-turn Office hint** (attachment type + clear request signals), through the current HERV3 tool-catalogue and backend payload paths. Keep the current singleloop. No extra planning model or unconditional all-tools inflation.
- Make output schemas concise and shallow. Emit bounded samples, clear next-tool recommendations, supported actions, real file IDs, and actionable schema errors.
- Include a skill hint in the existing HERV3 skills catalogue; load full guide on demand and preserve the compact FYI/catalogue approach. Do not inject an Office manual into every prompt.
- Native HERV3 tools, provider fallbacks, Ollama/OpenAI-compatible adapters and CLI Tool Gateway must all see only the same **authorized** tools, filtered by task relevance and runtime availability, without silently dropping Office on local small models.
- Test real configured small models (e.g. the available Gemma/Qwen variant): no hidden intermediary LLM, no shell scripts needed for basic use. Record tool-choice accuracy, argument validity, end-to-end task success and number of turns. Targets are engineering goals, not current claims.

### D. Actual Office scope

- Excel: workbook/sheets, bounded range read, cell writes/formulas, append, filtering/summarising, reconciliation, simple chart and formatting; formula update and cached results always distinguished.
- Word: headings, paragraphs, tables, image, styles, template-based reports and precise replacement preserving unaffected styles as far as engine supports.
- PowerPoint: slides, editable text, tables/images/charts, template/theme preservation, slide count/text/object parity and visual validation.
- PDF: inspect/read/render/convert as capabilities allow; do not claim general PDF editing. Support macro-enabled input only with an explicit preservation policy. Never execute macros; fail/report where preserving them is unsupported.
- For complex existing Office files, do not silently discard macros, external links, comments, change tracking, template assets or content. Report a precise limitation or select a high-fidelity configured engine.
- Build recipes for excel_summary, excel_reconcile, excel_clean, excel_chart, word_report, word_format, slides_from_outline and office_convert after the baseline operations actually work.

## Focused acceptance and release evidence

1. Static/test baseline: python -m pytest -q tests/test_office_toolkit_basics.py ; python scripts/check_protected_core_changes.py --base main ; git diff --check. Record focused red/green evidence when adding real gateway behavior.
2. Isolated worker: verify actual selected interpreter and installed package versions; run real XLSX/DOCX/PPTX read/create/edit with reopened byte-level and structural checks; corrupted/macro-heavy/external-link inputs and size limits must fail truthfully.
3. Permissions and recovery: Agent A cannot read B's private files; Workzone access cannot expand; no arbitrary host paths or model-chosen Python; duplicated tool calls, failed output commit, timeout, crash and uncertain receipt are recoverable without duplicate mutation or misleading success.
4. HERV3: actual Ollama schema selection and end-to-end tasks with available small Gemma and Qwen models, compare a stronger model. Require correct tool invocation, error correction, exact data and usable final document. Do not infer that tests of JSON schema alone prove small-model proficiency.
5. CLI Engines: same tool names/authority via existing Gateway, with real file output.
6. Frontend: one verified originating-session attachment via Telegram or Workbench when requested, re-entry/download/Office preview where supported. No double-publish, owner mirror or production context.
7. Only select affected tests per docs/TESTING_POLICY.md; no unnecessary hosted GitHub CI. Validate in independent non-production Windows/WSL and report tested/untested surfaces separately. Do not merge or deploy production as part of this handoff.

## Definition of done

A normally authorized Agent, regardless of HERV3 or CLI Engine, can inspect an uploaded or authorized XLSX/DOCX/PPTX, read selected content, create/edit a working copy, validate/finish it and deliver the actual file without choosing a Python interpreter or writing an Office Python script. Small models can discover and use the same tools. Runtime/permissions/validation are real, not documentation or successful mocks. Open a PR with scoped evidence and noted fidelity limits; do not claim production acceptance.
