import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  SUPPORTED_WORKER_BACKENDS,
  createDraftFromDocument,
  exportWorkflowDocument,
  getUnsupportedScopes,
  normalizeEditorMetadata,
  parseWorkflowDocument,
  validateRuntimeContract,
} from "./yamlCodec";
import { buildExportIssues, buildValidationIssues, getImportRecoveryNotice } from "./workflowSafety";

const FIXTURES_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "../../../tests/fixtures");

function readFixture(fileName: string) {
  return readFileSync(resolve(FIXTURES_DIR, fileName), "utf8");
}

describe("workflow safety helpers", () => {
  it("surfaces preserved unsupported fields and comment risk for the unknown-fields fixture", () => {
    const document = parseWorkflowDocument(readFixture("unknown_fields_workflow.yaml"));
    const draft = createDraftFromDocument(document);
    const unsupportedScopes = getUnsupportedScopes(document, draft);
    const issues = buildValidationIssues(document, unsupportedScopes.length);

    expect(document.compatibilityClass).toBe("B");
    expect(unsupportedScopes.map((entry) => entry.scope)).toEqual(
      expect.arrayContaining(["x-team-note", "agents.workers.writer_01", "steps.draft"]),
    );
    expect(issues.some((issue) => issue.id === "comments-present" && issue.severity === "warning")).toBe(true);
    expect(issues.some((issue) => issue.id === "unsupported-scopes-present")).toBe(true);
    expect(
      issues.some(
        (issue) =>
          issue.id === "runtime-contract-invalid" &&
          issue.severity === "blocking" &&
          issue.detail.includes("timeout_seconds"),
      ),
    ).toBe(true);
  });

  it("forces raw-mode recovery for the legacy fixture", () => {
    const document = parseWorkflowDocument(readFixture("legacy_english_news_to_chinese_markdown.yaml"));
    const notice = getImportRecoveryNotice(document);
    const exportIssues = buildExportIssues(document, true, "Form edits are blocked for legacy workflows.");

    expect(document.compatibilityClass).toBe("C");
    expect(notice?.forceRawMode).toBe(true);
    expect(exportIssues.some((issue) => issue.id === "export-class-c" && issue.severity === "blocking")).toBe(true);
  });

  it("keeps layout metadata scoped to current step ids", () => {
    const metadata = normalizeEditorMetadata(
      {
        version: 1,
        nodes: {
          draft: { position: { x: 120, y: 80 } },
          stale: { position: { x: 999, y: 999 } },
        },
        theme: "amber",
      },
      ["draft"],
    );

    expect(metadata).toEqual({
      version: 1,
      nodes: {
        draft: { position: { x: 120, y: 80 } },
      },
      theme: "amber",
    });
  });

  it("uses only runtime-supported backends and does not hard-code model catalogues", () => {
    expect([...SUPPORTED_WORKER_BACKENDS]).toEqual(["claude-cli", "codex-cli", "callable"]);

    const source = `workflow:
  id: editor-contract
  name: Editor contract
  version: 1.0.0
agents:
  orchestrator:
    id: flow-runner
  workers:
    - id: writer
      role: Writer
      agent_md: writer.md
      backend: openrouter-api
steps:
  - id: draft
    name: Draft
    agent: writer
    prompt: Write.
`;
    const document = parseWorkflowDocument(source);
    const problems = validateRuntimeContract(document.data);

    expect(problems).toContain("worker writer has unsupported backend: openrouter-api");
  });

  it("persists supported worker edits and blocks retired timeout controls", () => {
    const source = `workflow:
  id: editor-contract
  name: Editor contract
  version: 1.0.0
agents:
  orchestrator:
    id: flow-runner
  workers:
    - id: writer
      role: Writer
      agent_md: writer.md
      backend: claude-cli
steps:
  - id: draft
    name: Draft
    agent: writer
    prompt: Write.
`;
    const document = parseWorkflowDocument(source);
    const draft = createDraftFromDocument(document);
    const agents = draft.data.agents as { workers: Array<Record<string, unknown>> };
    agents.workers[0].backend = "codex-cli";
    agents.workers[0].model = "configured-model";

    const exported = exportWorkflowDocument(document, draft, { structuralEdits: true });
    const reloaded = parseWorkflowDocument(exported);
    const reloadedAgents = reloaded.data.agents as { workers: Array<Record<string, unknown>> };
    expect(reloadedAgents.workers[0].backend).toBe("codex-cli");
    expect(reloadedAgents.workers[0].model).toBe("configured-model");

    draft.steps[0].raw.timeout_seconds = 30;
    draft.steps[0].unsupportedFields.timeout_seconds = 30;
    expect(() => exportWorkflowDocument(document, draft, { structuralEdits: true })).toThrow(
      /retired workflow field: steps\[0\]\.timeout_seconds/,
    );
  });
});
