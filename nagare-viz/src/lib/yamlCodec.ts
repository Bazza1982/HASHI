import { dump, load } from "js-yaml";
import { validateWorkflowGraph, type GraphValidationResult } from "./dagValidator";

const CANONICAL_TOP_LEVEL_KEYS = new Set([
  "workflow",
  "meta",
  "changelog",
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
export const SUPPORTED_WORKER_BACKENDS = ["claude-cli", "codex-cli", "callable"] as const;

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
  const nextData = materializeDraftData(draft);

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
  const nextData = materializeDraftData(draft);
  const normalizedEditorMetadata = normalizeEditorMetadata(
    draft.editorMetadata,
    draft.steps.map((step) => step.id),
  );

  const safeForStructuralEdits =
    document.compatibilityClass === "A" &&
    !document.warnings.some((warning) => warning.severity === "error");

  if (intent.structuralEdits) {
    const runtimeProblems = validateRuntimeContract(nextData);
    if (runtimeProblems.length > 0) {
      throw new Error(`Runtime contract validation failed: ${runtimeProblems.join("; ")}`);
    }
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

export function materializeDraftData(draft: WorkflowDraft): Record<string, unknown> {
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
  return nextData;
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

  const workers = collectWorkerUnsupportedFields(draft.data);
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
        return !["id", "role", "agent_md", "backend", "model"].includes(key);
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
  const supportedKeys = new Set(["id", "name", "agent", "depends", "prompt"]);
  const unsupportedFields = Object.fromEntries(
    Object.entries(step).filter(([key]) => !supportedKeys.has(key)),
  );

  return {
    id: typeof step.id === "string" ? step.id : "",
    name: typeof step.name === "string" ? step.name : "",
    agent: typeof step.agent === "string" ? step.agent : "",
    depends: Array.isArray(step.depends) ? step.depends.filter((value): value is string => typeof value === "string") : [],
    prompt: typeof step.prompt === "string" ? step.prompt : "",
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

export function validateRuntimeContract(data: Record<string, unknown>): string[] {
  const problems: string[] = [];
  const safeComponent = /^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$/;
  const supportedBackends = new Set<string>(SUPPORTED_WORKER_BACKENDS);
  const supportedArtifactTypes = new Set([
    "json",
    "file",
    "directory",
    "text",
    "markdown",
    "binary",
  ]);

  const workflow = isRecord(data.workflow) ? data.workflow : null;
  if (!workflow) {
    problems.push("missing workflow mapping");
  } else {
    if (typeof workflow.id !== "string" || !safeComponent.test(workflow.id)) {
      problems.push("workflow.id must be a safe identifier");
    }
    if (typeof workflow.name !== "string" || workflow.name.length === 0) {
      problems.push("workflow.name must be a non-empty string");
    }
    if (typeof workflow.version !== "string" || !/^\d+\.\d+\.\d+$/.test(workflow.version)) {
      problems.push("workflow.version must use numeric MAJOR.MINOR.PATCH format");
    }
  }

  const agents = isRecord(data.agents) ? data.agents : null;
  const orchestrator = agents && isRecord(agents.orchestrator) ? agents.orchestrator : null;
  if (!orchestrator || typeof orchestrator.id !== "string" || !safeComponent.test(orchestrator.id)) {
    problems.push("agents.orchestrator.id must be a safe identifier");
  }

  const workers = agents && Array.isArray(agents.workers) ? agents.workers : [];
  const workerIds = new Set<string>();
  for (const value of workers) {
    if (!isRecord(value)) {
      problems.push("worker entries must be mappings");
      continue;
    }
    const workerId = typeof value.id === "string" ? value.id : "";
    if (!safeComponent.test(workerId)) {
      problems.push("worker.id must be a safe identifier");
      continue;
    }
    if (workerIds.has(workerId)) problems.push(`duplicate worker id: ${workerId}`);
    workerIds.add(workerId);
    if (typeof value.role !== "string" || value.role.length === 0) {
      problems.push(`worker ${workerId} must declare a non-empty role`);
    }
    if (typeof value.backend !== "string" || !supportedBackends.has(value.backend)) {
      problems.push(`worker ${workerId} has unsupported backend: ${String(value.backend)}`);
    }
    if (value.backend !== "callable" && (typeof value.agent_md !== "string" || value.agent_md.length === 0)) {
      problems.push(`worker ${workerId} must declare agent_md`);
    }
    if (value.model !== undefined && (typeof value.model !== "string" || value.model.length === 0)) {
      problems.push(`worker ${workerId} model must be a non-empty string`);
    }
  }

  const steps = Array.isArray(data.steps) ? data.steps : [];
  if (steps.length === 0) problems.push("steps must contain at least one executable step");
  for (const value of steps) {
    if (!isRecord(value)) {
      problems.push("step entries must be mappings");
      continue;
    }
    const stepId = typeof value.id === "string" ? value.id : "";
    if (!safeComponent.test(stepId)) problems.push("step.id must be a safe identifier");
    if (typeof value.name !== "string" || value.name.length === 0) {
      problems.push(`step ${stepId || "<unknown>"} must declare a non-empty name`);
    }
    if (typeof value.prompt !== "string") {
      problems.push(`step ${stepId || "<unknown>"} must declare a string prompt`);
    }
    if (typeof value.agent !== "string" || !workerIds.has(value.agent)) {
      problems.push(`step ${stepId || "<unknown>"} references an unknown worker`);
    }
    const strategy = value.strategy ?? "sequential";
    if (strategy !== "sequential" && strategy !== "parallel") {
      problems.push(`step ${stepId || "<unknown>"} has unsupported strategy`);
    }
    if (value.wait_for_human === true && strategy === "parallel") {
      problems.push(`step ${stepId || "<unknown>"} cannot wait for human in parallel`);
    }

    if (value.output !== undefined && !isRecord(value.output)) {
      problems.push(`step ${stepId || "<unknown>"} output must be a mapping`);
    } else if (isRecord(value.output) && value.output.artifacts !== undefined) {
      if (!Array.isArray(value.output.artifacts)) {
        problems.push(`step ${stepId || "<unknown>"} output.artifacts must be a list`);
      } else {
        for (const artifact of value.output.artifacts) {
          if (!isRecord(artifact)) {
            problems.push(`step ${stepId || "<unknown>"} artifact must be a mapping`);
            continue;
          }
          if (typeof artifact.key !== "string" || !safeComponent.test(artifact.key)) {
            problems.push(`step ${stepId || "<unknown>"} artifact key is invalid`);
          }
          if (typeof artifact.path !== "string" || artifact.path.length === 0) {
            problems.push(`step ${stepId || "<unknown>"} artifact path is required`);
          }
          if (typeof artifact.type !== "string" || !supportedArtifactTypes.has(artifact.type)) {
            problems.push(`step ${stepId || "<unknown>"} artifact type is unsupported`);
          }
        }
      }
    }
  }

  const retired = new Set([
    "auto_apply",
    "improvement_threshold",
    "max_attempts",
    "max_retries",
    "max_total_attempts",
    "on_max_exceeded",
    "retry_strategy",
    "timeout_seconds",
    "wait_for_human_timeout_seconds",
    "workspace",
    "controllable_by",
  ]);
  const walk = (value: unknown, prefix = "") => {
    if (Array.isArray(value)) {
      value.forEach((child, index) => walk(child, `${prefix}[${index}]`));
      return;
    }
    if (!isRecord(value)) return;
    for (const [key, child] of Object.entries(value)) {
      const path = prefix ? `${prefix}.${key}` : key;
      if (retired.has(key)) problems.push(`retired workflow field: ${path}`);
      walk(child, path);
    }
  };
  walk(data);

  return problems;
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
