import { useEffect, useMemo, useRef, useState } from "react";
import type { Edge, Node, NodeMouseHandler } from "@xyflow/react";
import {
  fetchRunArtifacts,
  fetchRunEvents,
  fetchRunSnapshot,
  type ApiRunArtifactsResponse,
  type ApiRunEventsResponse,
  type ApiRunSnapshotResponse,
} from "./api/nagareApi";
import { RunStatusOverlay } from "./components/RunStatusOverlay";
import { WorkflowCanvas } from "./components/WorkflowCanvas";
import { StepConfigPanel } from "./components/StepConfigPanel";
import { UnsupportedFieldsPanel } from "./components/UnsupportedFieldsPanel";
import { ValidationPanel } from "./components/ValidationPanel";
import { DiagnosticsPanel } from "./components/DiagnosticsPanel";
import { YamlEditor } from "./components/YamlEditor";
import { Toolbar } from "./components/Toolbar";
import { autoLayout } from "./lib/layout";
import { createLog, type DiagnosticLog } from "./lib/logger";
import { mapRuntimeToDraft } from "./lib/runtimeMapper";
import {
  buildExportIssues,
  buildValidationIssues,
  getImportRecoveryNotice,
  getParseFailureRecoveryNotice,
  makeCorrelationId,
  type RecoveryNotice,
} from "./lib/workflowSafety";
import {
  applyDraftToDocument,
  createDraftFromDocument,
  exportWorkflowDocument,
  getUnsupportedScopes,
  parseWorkflowDocument,
  type DraftStep,
  type WorkflowDocument,
  type WorkflowDraft,
} from "./lib/yamlCodec";

const DEFAULT_WORKFLOW = `workflow:
  id: smoke-test
  name: "Smoke Test"
  version: "1.0.0"
  description: "Minimal workflow for nagare-viz."

meta:
  created_by: baymax
  created_at: "2026-04-03T00:00:00Z"

pre_flight:
  collect_from_human: []

agents:
  orchestrator:
    id: akane
  workers:
    - id: writer_01
      role: "Writer"
      agent_md: "flow/agents/analyst/AGENT.md"
      backend: claude-cli
      model: claude-sonnet-4-6
    - id: checker_01
      role: "Checker"
      agent_md: "flow/agents/analyst/AGENT.md"
      backend: claude-cli
      model: claude-sonnet-4-6

steps:
  - id: step_write
    name: "Write"
    agent: writer_01
    depends: []
    prompt: |
      Write one sentence.
    timeout_seconds: 120
  - id: step_check
    name: "Check"
    agent: checker_01
    depends: [step_write]
    prompt: |
      Review the output.
    timeout_seconds: 120

x-nagare-viz:
  version: 1
  nodes:
    step_write:
      position:
        x: 120
        y: 96
    step_check:
      position:
        x: 440
        y: 96
`;

function bootstrapDocument(): WorkflowDocument {
  return parseWorkflowDocument(DEFAULT_WORKFLOW);
}

export default function App() {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [document, setDocument] = useState<WorkflowDocument>(() => bootstrapDocument());
  const [draft, setDraft] = useState<WorkflowDraft>(() => createDraftFromDocument(bootstrapDocument()));
  const [selectedStepId, setSelectedStepId] = useState<string | null>(null);
  const [rawMode, setRawMode] = useState(false);
  const [rawYaml, setRawYaml] = useState(DEFAULT_WORKFLOW);
  const [rawError, setRawError] = useState<string | null>(null);
  const [logs, setLogs] = useState<DiagnosticLog[]>([]);
  const [dirty, setDirty] = useState(false);
  const [correlationId, setCorrelationId] = useState(() => makeCorrelationId());
  const [recoveryNotice, setRecoveryNotice] = useState<RecoveryNotice | null>(getImportRecoveryNotice(document));
  const [apiBaseUrl, setApiBaseUrl] = useState("http://127.0.0.1:8787");
  const [runIdInput, setRunIdInput] = useState("");
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [runSnapshot, setRunSnapshot] = useState<ApiRunSnapshotResponse | null>(null);
  const [runEvents, setRunEvents] = useState<ApiRunEventsResponse | null>(null);
  const [runArtifacts, setRunArtifacts] = useState<ApiRunArtifactsResponse | null>(null);
  const [runOverlayError, setRunOverlayError] = useState<string | null>(null);
  const [runOverlayLoading, setRunOverlayLoading] = useState(false);

  const selectedStep = useMemo(
    () => draft.steps.find((step) => step.id === selectedStepId) ?? null,
    [draft.steps, selectedStepId],
  );

  const nodes = useMemo<Node[]>(
    () =>
      draft.steps.map((step) => {
        const nodeMetadata = getNodeMetadata(draft, step.id);
        const runtimeStep = runSnapshot?.run.step_status[step.id];
        return {
          id: step.id,
          type: "step",
          position: nodeMetadata.position,
          data: {
            label: step.name || step.id,
            agent: step.agent,
            dependsCount: step.depends.length,
            selected: selectedStepId === step.id,
            runtimeStatus: runtimeStep ? normalizeRuntimeStatus(runtimeStep.status) : "idle",
            runtimeAttempt: runtimeStep?.attempt ?? 1,
          },
        };
      }),
    [draft, runSnapshot, selectedStepId],
  );

  const edges = useMemo<Edge[]>(
    () =>
      draft.steps.flatMap((step) =>
        step.depends.map((dependency) => ({
          id: `${dependency}->${step.id}`,
          source: dependency,
          target: step.id,
          animated: false,
        })),
      ),
    [draft.steps],
  );

  const unsupportedScopes = useMemo(() => getUnsupportedScopes(document, draft), [document, draft]);
  const validationIssues = useMemo(
    () => buildValidationIssues(document, unsupportedScopes.length),
    [document, unsupportedScopes.length],
  );

  const structuralEdits = useMemo(() => {
    const current = JSON.stringify({
      steps: draft.steps.map(({ id, name, agent, depends, prompt, timeoutSeconds }) => ({
        id,
        name,
        agent,
        depends,
        prompt,
        timeoutSeconds,
      })),
    });
    const originalDraft = createDraftFromDocument(document);
    const original = JSON.stringify({
      steps: originalDraft.steps.map(({ id, name, agent, depends, prompt, timeoutSeconds }) => ({
        id,
        name,
        agent,
        depends,
        prompt,
        timeoutSeconds,
      })),
    });
    return current !== original;
  }, [document, draft.steps]);

  const hasBlockingWorkflowErrors = document.warnings.some((warning) => warning.severity === "error");
  const blockedReason = !structuralEdits
    ? null
    : document.compatibilityClass !== "A"
      ? "Form edits are blocked from export for class B/C workflows. Use raw YAML or metadata-only export."
      : hasBlockingWorkflowErrors
        ? "Form edits are blocked until duplicate ids, missing references, and cycle errors are resolved."
        : null;
  const exportIssues = useMemo(
    () => buildExportIssues(document, structuralEdits, blockedReason),
    [blockedReason, document, structuralEdits],
  );
  const runtimeOverlay = useMemo(() => {
    if (!runSnapshot || !runEvents || !runArtifacts) {
      return null;
    }
    return mapRuntimeToDraft(draft.steps.map((step) => step.id), runSnapshot.run, runEvents.events, runArtifacts.artifacts);
  }, [draft.steps, runArtifacts, runEvents, runSnapshot]);

  const pushLog = (entry: DiagnosticLog) => {
    setLogs((current) => [entry, ...current].slice(0, 30));
  };

  const refreshObservedRun = async (runId: string) => {
    setRunOverlayLoading(true);
    try {
      const [snapshot, events, artifacts] = await Promise.all([
        fetchRunSnapshot(apiBaseUrl, runId),
        fetchRunEvents(apiBaseUrl, runId),
        fetchRunArtifacts(apiBaseUrl, runId),
      ]);
      setRunSnapshot(snapshot);
      setRunEvents(events);
      setRunArtifacts(artifacts);
      setRunOverlayError(
        snapshot.snapshot_version !== 1 || events.snapshot_version !== 1 || artifacts.snapshot_version !== 1
          ? "Snapshot version mismatch between client and API."
          : null,
      );
      pushLog(createLog("info", "run.snapshot_loaded", `Observed ${runId} via ${snapshot.request_id}.`));
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown API failure.";
      setRunOverlayError(message);
      pushLog(createLog("warning", "run.snapshot_failed", message));
    } finally {
      setRunOverlayLoading(false);
    }
  };

  useEffect(() => {
    if (!activeRunId) {
      return undefined;
    }

    void refreshObservedRun(activeRunId);
    const timer = window.setInterval(() => {
      void refreshObservedRun(activeRunId);
    }, 4000);
    return () => window.clearInterval(timer);
  }, [activeRunId, apiBaseUrl]);

  const replaceDocument = (nextDocument: WorkflowDocument, options?: { rawMode?: boolean; notice?: RecoveryNotice | null }) => {
    setDocument(nextDocument);
    setDraft(createDraftFromDocument(nextDocument));
    setRawYaml(nextDocument.source);
    setRawError(null);
    setDirty(false);
    setSelectedStepId(null);
    setCorrelationId(makeCorrelationId());
    setRecoveryNotice(options?.notice ?? getImportRecoveryNotice(nextDocument));
    if (typeof options?.rawMode === "boolean") {
      setRawMode(options.rawMode);
    }
  };

  const handleImportRequest = () => {
    fileInputRef.current?.click();
  };

  const handleImportFile = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) {
      return;
    }

    try {
      const source = await file.text();
      const nextDocument = parseWorkflowDocument(source);
      const notice = getImportRecoveryNotice(nextDocument);
      replaceDocument(nextDocument, { rawMode: notice?.forceRawMode ?? false, notice });
      pushLog(
        createLog(
          notice?.severity === "warning" ? "warning" : "info",
          "workflow.imported",
          notice ? `${file.name}: ${notice.title}` : `Imported ${file.name}`,
        ),
      );
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown import failure.";
      const notice = getParseFailureRecoveryNotice(message);
      setCorrelationId(makeCorrelationId());
      setRecoveryNotice(notice);
      setRawYaml(await file.text());
      setRawMode(true);
      setDirty(true);
      setRawError(message);
      pushLog(createLog("warning", "workflow.import_recovered", notice.detail));
    } finally {
      event.target.value = "";
    }
  };

  const handleValidate = () => {
    try {
      const validated = applyDraftToDocument(document, draft);
      setDocument(validated);
      setRawYaml(validated.source);
      setRawError(null);
      setRecoveryNotice(getImportRecoveryNotice(validated));
      pushLog(createLog("info", "workflow.validated", "Validation completed against current draft."));
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown validation failure.";
      setRawError(message);
      pushLog(createLog("error", "workflow.validation_failed", message));
    }
  };

  const handleAutoLayout = () => {
    const nodesMetadata = autoLayout(draft);
    setDraft((current) => ({
      ...current,
      editorMetadata: {
        ...current.editorMetadata,
        version: 1,
        nodes: nodesMetadata,
      },
    }));
    setDirty(true);
    pushLog(createLog("info", "workflow.layout_updated", "Auto-layout refreshed x-nagare-viz node positions."));
  };

  const handleExport = () => {
    try {
      const yaml = exportWorkflowDocument(document, draft, { structuralEdits });
      downloadFile("workflow.yaml", yaml);
      pushLog(
        createLog(
          "info",
          "workflow.exported",
          structuralEdits
            ? "Exported with structural reserialize for a class A workflow."
            : "Exported with metadata-preserving path.",
        ),
      );
      setRawYaml(yaml);
      setDirty(false);
      setRecoveryNotice(getImportRecoveryNotice(parseWorkflowDocument(yaml)));
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown export failure.";
      pushLog(createLog("error", "workflow.export_failed", message));
      setRawError(message);
    }
  };

  const handleToggleMode = () => {
    setRawMode((current) => !current);
  };

  const handleApplyRawYaml = () => {
    try {
      const nextDocument = parseWorkflowDocument(rawYaml);
      replaceDocument(nextDocument, { notice: getImportRecoveryNotice(nextDocument) });
      pushLog(createLog("info", "workflow.raw_applied", "Replaced draft from raw YAML."));
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown YAML parse failure.";
      setRawError(message);
      setRecoveryNotice(getParseFailureRecoveryNotice(message));
      pushLog(createLog("error", "workflow.raw_apply_failed", message));
    }
  };

  const handleStepChange = (nextStep: DraftStep) => {
    setDraft((current) => ({
      ...current,
      steps: current.steps.map((step) => (step.id === nextStep.id ? nextStep : step)),
    }));
    setDirty(true);
    setRecoveryNotice(null);
  };

  const handleNodeDragStop: NodeMouseHandler<Node> = (_event, node) => {
    setDraft((current) => ({
      ...current,
      editorMetadata: {
        ...current.editorMetadata,
        version: 1,
        nodes: {
          ...getNodesRecord(current.editorMetadata),
          [node.id]: {
            position: {
              x: Math.round(node.position.x),
              y: Math.round(node.position.y),
            },
          },
        },
      },
    }));
    setDirty(true);
    setRecoveryNotice(null);
  };

  const handleObserveRun = () => {
    const trimmedRunId = runIdInput.trim();
    if (!trimmedRunId) {
      setRunOverlayError("Run ID is required before observation starts.");
      return;
    }
    setActiveRunId(trimmedRunId);
  };

  const handleDisconnectRun = () => {
    setActiveRunId(null);
    setRunSnapshot(null);
    setRunEvents(null);
    setRunArtifacts(null);
    setRunOverlayError(null);
    pushLog(createLog("info", "run.snapshot_disconnected", "Cleared observed runtime overlay."));
  };

  return (
    <div className="app-shell">
      <Toolbar
        dirty={dirty}
        rawMode={rawMode}
        onImport={handleImportRequest}
        onValidate={handleValidate}
        onAutoLayout={handleAutoLayout}
        onExport={handleExport}
        onToggleMode={handleToggleMode}
      />
      <input
        ref={fileInputRef}
        hidden
        accept=".yaml,.yml,text/yaml,text/x-yaml"
        type="file"
        onChange={handleImportFile}
      />
      {recoveryNotice ? (
        <section className={`recovery-banner ${recoveryNotice.severity}`}>
          <div>
            <strong>{recoveryNotice.title}</strong>
            <p>{recoveryNotice.detail}</p>
          </div>
          <span>{recoveryNotice.suggestedAction}</span>
        </section>
      ) : null}
      <main className="workspace">
        <section className="workspace-main">
          <div className="workspace-main-stack">
            <RunStatusOverlay
              apiBaseUrl={apiBaseUrl}
              runIdInput={runIdInput}
              activeRun={runSnapshot}
              overlay={runtimeOverlay}
              loading={runOverlayLoading}
              error={runOverlayError}
              onApiBaseUrlChange={setApiBaseUrl}
              onRunIdInputChange={setRunIdInput}
              onConnect={handleObserveRun}
              onDisconnect={handleDisconnectRun}
              onRefresh={() => {
                if (activeRunId) {
                  void refreshObservedRun(activeRunId);
                }
              }}
            />
            {rawMode ? (
              <YamlEditor
                value={rawYaml}
                parseError={rawError}
                blockedReason={blockedReason}
                onChange={(value) => {
                  setRawYaml(value);
                  setDirty(true);
                }}
                onApply={handleApplyRawYaml}
              />
            ) : (
              <WorkflowCanvas
                nodes={nodes}
                edges={edges}
                onSelectStep={setSelectedStepId}
                onNodeDragStop={handleNodeDragStop}
              />
            )}
          </div>
        </section>
        <aside className="workspace-side">
          <StepConfigPanel
            step={selectedStep}
            availableStepIds={draft.steps.map((step) => step.id)}
            onChange={handleStepChange}
          />
          <ValidationPanel
            compatibilityClass={document.compatibilityClass}
            issues={validationIssues}
          />
          <UnsupportedFieldsPanel scopes={unsupportedScopes} />
          <DiagnosticsPanel
            correlationId={correlationId}
            parserIssue={rawError}
            validationIssues={validationIssues}
            exportIssues={exportIssues}
            logs={logs}
          />
        </aside>
      </main>
    </div>
  );
}

function normalizeRuntimeStatus(status: string): "idle" | "running" | "completed" | "failed" | "waiting_human" {
  switch (status.toUpperCase()) {
    case "RUNNING":
      return "running";
    case "COMPLETED":
      return "completed";
    case "FAILED":
      return "failed";
    case "WAITING_HUMAN":
      return "waiting_human";
    default:
      return "idle";
  }
}

function getNodeMetadata(draft: WorkflowDraft, stepId: string) {
  const nodes = getNodesRecord(draft.editorMetadata);
  const stepMetadata = nodes[stepId];
  const positionSource = isRecord(stepMetadata) && isRecord(stepMetadata.position)
    ? stepMetadata.position
    : null;
  const position = positionSource
    ? {
        x: asNumber(positionSource.x, 80),
        y: asNumber(positionSource.y, 80),
      }
    : { x: 80, y: 80 };
  return { position };
}

function getNodesRecord(editorMetadata: Record<string, unknown>) {
  const nodesValue = editorMetadata.nodes;
  return isRecord(nodesValue) ? nodesValue : {};
}

function downloadFile(fileName: string, content: string) {
  const blob = new Blob([content], { type: "text/yaml;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = fileName;
  anchor.click();
  URL.revokeObjectURL(url);
}

function asNumber(value: unknown, fallback: number) {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
