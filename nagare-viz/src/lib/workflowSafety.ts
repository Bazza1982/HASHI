import type { WorkflowDocument } from "./yamlCodec";

export type ValidationSeverity = "blocking" | "warning" | "info";

export type ValidationIssue = {
  id: string;
  severity: ValidationSeverity;
  title: string;
  detail: string;
  source: "compatibility" | "fidelity" | "graph" | "parser" | "layout" | "export";
};

export type RecoveryNotice = {
  severity: "warning" | "info";
  title: string;
  detail: string;
  suggestedAction: string;
  forceRawMode: boolean;
};

export function buildValidationIssues(
  document: WorkflowDocument,
  unsupportedScopesCount: number,
): ValidationIssue[] {
  const issues: ValidationIssue[] = [];

  issues.push({
    id: `compatibility-${document.compatibilityClass}`,
    severity: document.compatibilityClass === "C" ? "blocking" : document.compatibilityClass === "B" ? "warning" : "info",
    title: `Compatibility class ${document.compatibilityClass}`,
    detail: describeCompatibility(document.compatibilityClass),
    source: "compatibility",
  });

  for (const warning of document.warnings) {
    issues.push({
      id: warning.code,
      severity: warning.severity === "error" ? "blocking" : "warning",
      title: humanizeCode(warning.code),
      detail: warning.message,
      source: "fidelity",
    });
  }

  for (const stepId of document.graphValidation.duplicateStepIds) {
    issues.push({
      id: `duplicate-${stepId}`,
      severity: "blocking",
      title: "Duplicate step id",
      detail: `Step id \`${stepId}\` appears more than once.`,
      source: "graph",
    });
  }

  for (const dependency of document.graphValidation.missingDependencies) {
    issues.push({
      id: `missing-dependency-${dependency}`,
      severity: "blocking",
      title: "Missing dependency",
      detail: `Dependency reference \`${dependency}\` does not resolve to a known step.`,
      source: "graph",
    });
  }

  for (const agent of document.graphValidation.missingAgents) {
    issues.push({
      id: `missing-agent-${agent}`,
      severity: "blocking",
      title: "Missing agent",
      detail: `Agent reference \`${agent}\` does not resolve to a declared worker.`,
      source: "graph",
    });
  }

  for (const cycle of document.graphValidation.cycles) {
    issues.push({
      id: `cycle-${cycle.join(">")}`,
      severity: "blocking",
      title: "Dependency cycle",
      detail: cycle.join(" -> "),
      source: "graph",
    });
  }

  issues.push({
    id: "layout-non-semantic",
    severity: "info",
    title: "Canvas layout is non-semantic",
    detail: "Node positions are stored under x-nagare-viz and do not change workflow execution order.",
    source: "layout",
  });

  if (unsupportedScopesCount > 0) {
    issues.push({
      id: "unsupported-scopes-present",
      severity: "warning",
      title: "Unsupported fields preserved",
      detail: `${unsupportedScopesCount} unsupported scope${unsupportedScopesCount === 1 ? "" : "s"} remain inspectable but not form-editable.`,
      source: "fidelity",
    });
  }

  return rankIssues(issues);
}

export function buildExportIssues(
  document: WorkflowDocument,
  structuralEdits: boolean,
  blockedReason: string | null,
): ValidationIssue[] {
  const issues: ValidationIssue[] = [];

  if (blockedReason) {
    issues.push({
      id: "export-blocked",
      severity: "blocking",
      title: "Export blocked",
      detail: blockedReason,
      source: "export",
    });
  } else if (structuralEdits) {
    issues.push({
      id: "export-structural",
      severity: "warning",
      title: "Structural export path",
      detail: "This export rewrites supported workflow fields from form state instead of preserving the original YAML text.",
      source: "export",
    });
  } else {
    issues.push({
      id: "export-metadata-only",
      severity: "info",
      title: "Metadata-preserving export path",
      detail: "Only x-nagare-viz metadata will be rewritten; the original workflow text stays intact elsewhere.",
      source: "export",
    });
  }

  if (document.compatibilityClass === "C") {
    issues.push({
      id: "export-class-c",
      severity: "blocking",
      title: "Raw YAML required",
      detail: "Class C workflows should be inspected and edited in raw YAML mode only.",
      source: "export",
    });
  }

  return rankIssues(issues);
}

export function getImportRecoveryNotice(document: WorkflowDocument): RecoveryNotice | null {
  if (document.compatibilityClass === "C") {
    return {
      severity: "warning",
      title: "Imported in raw-mode recovery",
      detail: "This workflow uses a legacy or non-canonical shape. The canvas stays available for inspection, but safe editing is limited to raw YAML.",
      suggestedAction: "Inspect warnings, review unsupported fields, and use raw YAML for any semantic changes.",
      forceRawMode: true,
    };
  }

  if (document.compatibilityClass === "B") {
    return {
      severity: "warning",
      title: "Imported with fidelity warnings",
      detail: "This workflow is partially representable. Visual inspection is available, but structural exports may be blocked or warned.",
      suggestedAction: "Use Validate before export and prefer metadata-only export unless the workflow is class A.",
      forceRawMode: false,
    };
  }

  return {
    severity: "info",
    title: "Imported for visual editing",
    detail: "This workflow is currently safe for standard canvas editing.",
    suggestedAction: "Canvas positions remain non-semantic; use Validate to re-check execution integrity after edits.",
    forceRawMode: false,
  };
}

export function getParseFailureRecoveryNotice(message: string): RecoveryNotice {
  return {
    severity: "warning",
    title: "Import recovery opened raw YAML",
    detail: `The YAML could not be parsed: ${message}`,
    suggestedAction: "Fix the YAML syntax in raw mode, then apply it again to rebuild the draft.",
    forceRawMode: true,
  };
}

export function makeCorrelationId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `corr-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function describeCompatibility(compatibilityClass: WorkflowDocument["compatibilityClass"]): string {
  if (compatibilityClass === "A") {
    return "Visual edits are considered safe for export when no blocking issues remain.";
  }
  if (compatibilityClass === "B") {
    return "The workflow is inspectable and partially editable, but fidelity risks remain visible.";
  }
  return "The workflow should be treated as inspect-and-raw-edit only until a compatibility layer exists.";
}

function humanizeCode(code: string): string {
  return code
    .split("-")
    .map((chunk) => chunk.charAt(0).toUpperCase() + chunk.slice(1))
    .join(" ");
}

function rankIssues(issues: ValidationIssue[]): ValidationIssue[] {
  const order: Record<ValidationSeverity, number> = {
    blocking: 0,
    warning: 1,
    info: 2,
  };
  return [...issues].sort((left, right) => order[left.severity] - order[right.severity]);
}
