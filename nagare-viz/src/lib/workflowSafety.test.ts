import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { createDraftFromDocument, getUnsupportedScopes, normalizeEditorMetadata, parseWorkflowDocument } from "./yamlCodec";
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
});
