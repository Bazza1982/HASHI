import { dump, load } from "js-yaml";
import { validateWorkflowGraph, type GraphValidationResult } from "./dagValidator";

const CANONICAL_TOP_LEVEL_KEYS = new Set([
  "workflow",
  "meta",
  "pre_flight",
  "agents",
  "steps",
  "error_handling",
  "success_criteria",
  "evaluation",
  "output",
  "x-nagare-viz",
]);

const TOP_LEVEL_BLOCK_PATTERN = /^(?<key>[A-Za-z0-9_-]+):(?!\S)/gm;

export type Severity = "warning" | "error";
export type CompatibilityClass = "A" | "B" | "C";

export type FidelityWarning = {
  code: string;
  message: string;
  severity: Severity;
};

export type DraftStep = {
  id: string;
  name: string;
  agent: string;
  depends: string[];
  prompt: string;
  timeoutSeconds: number | null;
  raw: Record<string, unknown>;
  unsupportedFields: Record<string, unknown>;
};

export type WorkflowDocument = {
  data: Record<string, unknown>;
  source: string;
  compatibilityClass: CompatibilityClass;
  warnings: FidelityWarning[];
  unknownTopLevelKeys: string[];
  graphValidation: GraphValidationResult;
};

export type WorkflowDraft = {
  data: Record<string, unknown>;
  steps: DraftStep[];
  editorMetadata: Record<string, unknown>;
};

export type ExportIntent = {
  structuralEdits: boolean;
};

export function parseWorkflowDocument(source: string): WorkflowDocument {
  const parsed = load(source);
  if (!isRecord(parsed)) {
    throw new Error("Workflow YAML must deserialize to a mapping at the top level.");
  }

  const warnings: FidelityWarning[] = [];
  let compatibilityClass: CompatibilityClass = "A";
  const unknownTopLevelKeys = Object.keys(parsed).filter((key) => !CANONICAL_TOP_LEVEL_KEYS.has(key));

  if (unknownTopLevelKeys.length > 0) {
    warnings.push({
      code: "unknown-top-level-fields",
      message:
        "Unknown top-level fields require preservation-aware export and are not yet safe for arbitrary visual edits.",
      severity: "warning",
    });
    compatibilityClass = "B";
  }

  if (sourceHasComments(source)) {
    warnings.push({
      code: "comments-present",
      message:
        "The workflow contains comments. Metadata-only export preserves them, but structural reserialize does not.",
      severity: "warning",
    });
    compatibilityClass = maxCompatibilityClass(compatibilityClass, "B");
  }

  if (isLegacyWorkflowShape(parsed)) {
    warnings.push({
      code: "legacy-dialect",
      message: "This workflow uses the legacy HASHI dialect and should stay in raw YAML mode.",
      severity: "warning",
    });
    compatibilityClass = "C";
  }

  const graphValidation = validateWorkflowGraph(parsed);
  if (graphValidation.duplicateStepIds.length > 0) {
    warnings.push({
      code: "duplicate-step-ids",
      message: "Duplicate step ids block safe export.",
      severity: "error",
    });
  }
  if (graphValidation.missingDependencies.length > 0) {
    warnings.push({
      code: "missing-dependencies",
      message: "Some step dependencies do not resolve to a known step id.",
      severity: "error",
    });
  }
  if (graphValidation.missingAgents.length > 0) {
    warnings.push({
      code: "missing-agents",
      message: "Some steps reference agents that are not declared.",
      severity: "error",
    });
  }
  if (graphValidation.cycles.length > 0) {
    warnings.push({
      code: "cycles-detected",
      message: "The workflow graph contains dependency cycles.",
      severity: "error",
    });
  }

  return {
    data: parsed,
    source,
    compatibilityClass,
    warnings,
    unknownTopLevelKeys,
    graphValidation,
  };
}

export function createDraftFromDocument(document: WorkflowDocument): WorkflowDraft {
  const data = deepClone(document.data);
  const steps = Array.isArray(data.steps) ? data.steps.filter(isRecord).map(normalizeStep) : [];
  const editorMetadata = normalizeEditorMetadata(
    isRecord(data["x-nagare-viz"]) ? deepClone(data["x-nagare-viz"]) : {},
    steps.map((step) => step.id),
  );

  return {
    data,
    steps,
    editorMetadata,
  };
}

export function applyDraftToDocument(_document: WorkflowDocument, draft: WorkflowDraft): WorkflowDocument {
  const nextData = deepClone(draft.data);
  nextData.steps = draft.steps.map(denormalizeStep);
  const normalizedEditorMetadata = normalizeEditorMetadata(
    draft.editorMetadata,
    draft.steps.map((step) => step.id),
  );
  if (Object.keys(normalizedEditorMetadata).length > 0) {
    nextData["x-nagare-viz"] = normalizedEditorMetadata;
  } else {
    delete nextData["x-nagare-viz"];
  }

  const nextSource = dump(nextData, {
    noRefs: true,
    lineWidth: 100,
    sortKeys: false,
  });
  return parseWorkflowDocument(nextSource);
}

export function exportWorkflowDocument(
  document: WorkflowDocument,
  draft: WorkflowDraft,
  intent: ExportIntent,
): string {
  const nextData = deepClone(draft.data);
  nextData.steps = draft.steps.map(denormalizeStep);
  const normalizedEditorMetadata = normalizeEditorMetadata(
    draft.editorMetadata,
    draft.steps.map((step) => step.id),
  );

  if (Object.keys(normalizedEditorMetadata).length > 0) {
    nextData["x-nagare-viz"] = normalizedEditorMetadata;
  } else {
    delete nextData["x-nagare-viz"];
  }

  const safeForStructuralEdits =
    document.compatibilityClass === "A" &&
    !document.warnings.some((warning) => warning.severity === "error");

  if (intent.structuralEdits) {
    if (!safeForStructuralEdits) {
      throw new Error(
        "This workflow is not safe for structural export from form mode. Use raw YAML or metadata-only edits.",
      );
    }
    return dump(nextData, {
      noRefs: true,
      lineWidth: 100,
      sortKeys: false,
    });
  }

  return replaceOrAppendTopLevelBlock(document.source, "x-nagare-viz", {
    "x-nagare-viz": normalizedEditorMetadata,
  });
}

export function getUnsupportedScopes(document: WorkflowDocument, draft: WorkflowDraft) {
  const topLevel = document.unknownTopLevelKeys.map((key) => ({
    scope: key,
    value: document.data[key],
  }));

  const steps = draft.steps
    .filter((step) => Object.keys(step.unsupportedFields).length > 0)
    .map((step) => ({
      scope: `steps.${step.id}`,
      value: step.unsupportedFields,
    }));

  const workers = collectWorkerUnsupportedFields(document.data);
  return [...topLevel, ...workers, ...steps];
}

function collectWorkerUnsupportedFields(
  data: Record<string, unknown>,
): Array<{ scope: string; value: unknown }> {
  const agents = isRecord(data.agents) ? data.agents : {};
  const workers = Array.isArray(agents.workers) ? agents.workers : [];
  return workers
    .filter(isRecord)
    .map((worker) => {
      const unsupportedEntries = Object.entries(worker).filter(([key]) => {
        return !["id", "role", "agent_md", "backend", "model", "workspace"].includes(key);
      });
      if (unsupportedEntries.length === 0 || typeof worker.id !== "string") {
        return null;
      }
      return {
        scope: `agents.workers.${worker.id}`,
        value: Object.fromEntries(unsupportedEntries),
      };
    })
    .filter((value): value is { scope: string; value: Record<string, unknown> } => value !== null);
}

function normalizeStep(step: Record<string, unknown>): DraftStep {
  const supportedKeys = new Set(["id", "name", "agent", "depends", "prompt", "timeout_seconds"]);
  const unsupportedFields = Object.fromEntries(
    Object.entries(step).filter(([key]) => !supportedKeys.has(key)),
  );

  return {
    id: typeof step.id === "string" ? step.id : "",
    name: typeof step.name === "string" ? step.name : "",
    agent: typeof step.agent === "string" ? step.agent : "",
    depends: Array.isArray(step.depends) ? step.depends.filter((value): value is string => typeof value === "string") : [],
    prompt: typeof step.prompt === "string" ? step.prompt : "",
    timeoutSeconds: typeof step.timeout_seconds === "number" ? step.timeout_seconds : null,
    raw: deepClone(step),
    unsupportedFields,
  };
}

function denormalizeStep(step: DraftStep): Record<string, unknown> {
  const nextStep = deepClone(step.raw);
  nextStep.id = step.id;
  nextStep.name = step.name;
  nextStep.agent = step.agent;
  nextStep.depends = [...step.depends];
  nextStep.prompt = step.prompt;
  if (step.timeoutSeconds === null || Number.isNaN(step.timeoutSeconds)) {
    delete nextStep.timeout_seconds;
  } else {
    nextStep.timeout_seconds = step.timeoutSeconds;
  }
  for (const [key, value] of Object.entries(step.unsupportedFields)) {
    nextStep[key] = deepClone(value);
  }
  return nextStep;
}

export function normalizeEditorMetadata(
  editorMetadata: Record<string, unknown>,
  stepIds: string[],
): Record<string, unknown> {
  const nextMetadata = deepClone(editorMetadata);
  const allowedStepIds = new Set(stepIds.filter((stepId) => stepId.length > 0));
  const nodesValue = nextMetadata.nodes;

  if (!isRecord(nodesValue)) {
    delete nextMetadata.nodes;
    return nextMetadata;
  }

  const filteredNodes = Object.fromEntries(
    Object.entries(nodesValue).filter(([stepId]) => allowedStepIds.has(stepId)),
  );

  if (Object.keys(filteredNodes).length === 0) {
    delete nextMetadata.nodes;
  } else {
    nextMetadata.nodes = filteredNodes;
  }

  return nextMetadata;
}

function replaceOrAppendTopLevelBlock(
  source: string,
  key: string,
  value: Record<string, unknown>,
): string {
  const rendered = dump(value, {
    noRefs: true,
    lineWidth: 100,
    sortKeys: false,
  }).trimEnd() + "\n";

  const blockRange = findTopLevelBlockRange(source, key);
  if (!blockRange) {
    if (Object.keys(value[key] as Record<string, unknown> | undefined ?? {}).length === 0) {
      return source;
    }
    if (source.endsWith("\n") || source.length === 0) {
      const separator = source.length === 0 || source.endsWith("\n\n") ? "" : "\n";
      return `${source}${separator}${rendered}`;
    }
    return `${source}\n\n${rendered}`;
  }

  const [start, end] = blockRange;
  const prefix = source.slice(0, start);
  const suffix = source.slice(end).replace(/^\n+/, "");
  if (Object.keys(value[key] as Record<string, unknown> | undefined ?? {}).length === 0) {
    return prefix.trimEnd() + (suffix ? `\n\n${suffix}` : "\n");
  }
  return `${prefix}${rendered}${suffix}`;
}

function findTopLevelBlockRange(source: string, key: string): [number, number] | null {
  const matches = [...source.matchAll(TOP_LEVEL_BLOCK_PATTERN)];
  for (let index = 0; index < matches.length; index += 1) {
    const match = matches[index];
    if (match.groups?.key !== key || typeof match.index !== "number") {
      continue;
    }
    const start = match.index;
    const end = index + 1 < matches.length && typeof matches[index + 1].index === "number"
      ? matches[index + 1].index!
      : source.length;
    return [start, end];
  }
  return null;
}

function sourceHasComments(source: string): boolean {
  return source
    .split("\n")
    .filter((line) => line.trim().length > 0)
    .some((line) => line.trimStart().startsWith("#"));
}

function isLegacyWorkflowShape(parsed: Record<string, unknown>): boolean {
  return !("workflow" in parsed) && ("tasks" in parsed || "workers" in parsed);
}

function maxCompatibilityClass(left: CompatibilityClass, right: CompatibilityClass): CompatibilityClass {
  const order: Record<CompatibilityClass, number> = { A: 0, B: 1, C: 2 };
  return order[left] >= order[right] ? left : right;
}

function deepClone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
