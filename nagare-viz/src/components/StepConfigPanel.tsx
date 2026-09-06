import { useState } from "react";
import { SUPPORTED_WORKER_BACKENDS, type DraftStep } from "../lib/yamlCodec";

export type WorkerInfo = {
  id: string;
  role: string;
  backend: string;
  model: string;
};

type StepConfigPanelProps = {
  selectedSteps: DraftStep[];
  availableStepIds: string[];
  workers: WorkerInfo[];
  onChange: (step: DraftStep) => void;
  onWorkerChange: (workerId: string, field: "backend" | "model", value: string) => void;
  onBatchStepChange: (field: "agent", value: string) => void;
  onBatchPromptChange: (text: string, append: boolean) => void;
  onBatchWorkerChange: (field: "backend" | "model", value: string) => void;
};

export function StepConfigPanel({
  selectedSteps,
  availableStepIds,
  workers,
  onChange,
  onWorkerChange,
  onBatchStepChange,
  onBatchPromptChange,
  onBatchWorkerChange,
}: StepConfigPanelProps) {
  const [batchPromptText, setBatchPromptText] = useState("");
  const [batchPromptMode, setBatchPromptMode] = useState<"overwrite" | "append">("overwrite");

  // No selection
  if (selectedSteps.length === 0) {
    return (
      <section className="panel">
        <h2>Step</h2>
        <p className="muted">Select a node to edit supported fields.</p>
      </section>
    );
  }

  // Single selection — full edit panel
  if (selectedSteps.length === 1) {
    const step = selectedSteps[0];
    const worker = workers.find((w) => w.id === step.agent);
    const currentBackend = worker?.backend ?? "";
    const currentModel = worker?.model ?? "";

    return (
      <section className="panel">
        <h2>Step</h2>
        <label className="field">
          <span>Id</span>
          <input value={step.id} disabled />
        </label>
        <label className="field">
          <span>Name</span>
          <input
            value={step.name}
            onChange={(event) => onChange({ ...step, name: event.target.value })}
          />
        </label>
        <label className="field">
          <span>Agent</span>
          <input
            value={step.agent}
            onChange={(event) => onChange({ ...step, agent: event.target.value })}
          />
        </label>

        {worker && (
          <>
            <label className="field">
              <span>Backend</span>
              <select
                value={currentBackend}
                onChange={(event) => onWorkerChange(worker.id, "backend", event.target.value)}
              >
                {!SUPPORTED_WORKER_BACKENDS.includes(currentBackend as (typeof SUPPORTED_WORKER_BACKENDS)[number]) && (
                  <option value={currentBackend}>{currentBackend}</option>
                )}
                {SUPPORTED_WORKER_BACKENDS.map((b) => (
                  <option key={b} value={b}>{b}</option>
                ))}
              </select>
            </label>
            {currentBackend !== "callable" && (
              <label className="field">
                <span>Model</span>
                <input
                  value={currentModel}
                  placeholder="CLI default when empty"
                  onChange={(event) => onWorkerChange(worker.id, "model", event.target.value)}
                />
              </label>
            )}
          </>
        )}

        {!worker && step.agent && (
          <p className="muted" style={{ fontSize: "0.78rem" }}>
            Worker "{step.agent}" not found in agents.workers
          </p>
        )}

        <label className="field">
          <span>Depends</span>
          <input
            value={step.depends.join(", ")}
            onChange={(event) =>
              onChange({
                ...step,
                depends: event.target.value
                  .split(",")
                  .map((value) => value.trim())
                  .filter((value) => value.length > 0 && value !== step.id),
              })
            }
            list="step-ids"
          />
        </label>
        <datalist id="step-ids">
          {availableStepIds.map((stepId) => (
            <option key={stepId} value={stepId} />
          ))}
        </datalist>
        <label className="field">
          <span>Prompt</span>
          <textarea
            rows={12}
            value={step.prompt}
            onChange={(event) => onChange({ ...step, prompt: event.target.value })}
          />
        </label>
      </section>
    );
  }

  // Multi-selection — batch edit panel
  const count = selectedSteps.length;

  // Derive shared backend/model across all selected steps' workers
  const selectedAgentIds = new Set(selectedSteps.map((s) => s.agent).filter(Boolean));
  const selectedWorkers = workers.filter((w) => selectedAgentIds.has(w.id));
  const uniqueBackends = [...new Set(selectedWorkers.map((w) => w.backend).filter(Boolean))];
  const sharedBackend = uniqueBackends.length === 1 ? uniqueBackends[0] : "";
  const uniqueModels = [...new Set(selectedWorkers.map((w) => w.model).filter(Boolean))];
  const sharedModel = uniqueModels.length === 1 ? uniqueModels[0] : "";

  const batchBackend = sharedBackend;

  return (
    <section className="panel">
      <h2>
        Step{" "}
        <span style={{ fontSize: "0.75rem", fontWeight: "normal", opacity: 0.7 }}>
          {count} selected
        </span>
      </h2>
      <p className="muted" style={{ fontSize: "0.78rem", marginBottom: "0.5rem" }}>
        Batch editing — changes apply to all {count} steps.
        <br />
        ID, Name, Depends are not editable in group mode.
      </p>

      <label className="field">
        <span>Agent</span>
        <input
          placeholder="— set agent for all —"
          defaultValue=""
          onBlur={(event) => {
            const v = event.target.value.trim();
            if (v) onBatchStepChange("agent", v);
          }}
        />
      </label>

      {selectedWorkers.length > 0 && (
        <>
          <label className="field">
            <span>Backend</span>
            <select
              value={batchBackend}
              onChange={(event) => onBatchWorkerChange("backend", event.target.value)}
            >
              <option value="" disabled>— {uniqueBackends.length > 1 ? "mixed" : "select"} —</option>
              {batchBackend && !SUPPORTED_WORKER_BACKENDS.includes(batchBackend as (typeof SUPPORTED_WORKER_BACKENDS)[number]) && (
                <option value={batchBackend}>{batchBackend} (unsupported)</option>
              )}
              {SUPPORTED_WORKER_BACKENDS.map((b) => (
                <option key={b} value={b}>{b}</option>
              ))}
            </select>
          </label>
          {batchBackend !== "callable" && (
            <label className="field">
              <span>Model</span>
              <input
                value={sharedModel}
                placeholder={uniqueModels.length > 1 ? "mixed models" : "CLI default when empty"}
                onChange={(event) => onBatchWorkerChange("model", event.target.value)}
              />
            </label>
          )}
        </>
      )}

      <div className="field" style={{ flexDirection: "column", alignItems: "flex-start", gap: "0.4rem" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", width: "100%" }}>
          <span>Prompt</span>
          <span style={{ display: "flex", gap: "0.25rem" }}>
            <button
              type="button"
              style={{
                fontSize: "0.7rem",
                padding: "2px 8px",
                borderRadius: 4,
                border: "1px solid var(--border, #333)",
                background: batchPromptMode === "overwrite" ? "var(--accent, #0d6b5f)" : "transparent",
                color: batchPromptMode === "overwrite" ? "#fff" : "inherit",
                cursor: "pointer",
              }}
              onClick={() => setBatchPromptMode("overwrite")}
            >
              Overwrite
            </button>
            <button
              type="button"
              style={{
                fontSize: "0.7rem",
                padding: "2px 8px",
                borderRadius: 4,
                border: "1px solid var(--border, #333)",
                background: batchPromptMode === "append" ? "var(--accent, #0d6b5f)" : "transparent",
                color: batchPromptMode === "append" ? "#fff" : "inherit",
                cursor: "pointer",
              }}
              onClick={() => setBatchPromptMode("append")}
            >
              Append
            </button>
          </span>
        </div>
        <textarea
          rows={8}
          style={{ width: "100%", boxSizing: "border-box" }}
          placeholder={batchPromptMode === "append" ? "Text to append to all prompts…" : "New prompt for all selected steps…"}
          value={batchPromptText}
          onChange={(e) => setBatchPromptText(e.target.value)}
        />
        <button
          type="button"
          style={{
            width: "100%",
            padding: "6px",
            borderRadius: 4,
            border: "1px solid var(--accent, #0d6b5f)",
            background: "var(--accent, #0d6b5f)",
            color: "#fff",
            cursor: batchPromptText.trim() ? "pointer" : "not-allowed",
            opacity: batchPromptText.trim() ? 1 : 0.5,
            fontSize: "0.82rem",
          }}
          disabled={!batchPromptText.trim()}
          onClick={() => {
            onBatchPromptChange(batchPromptText, batchPromptMode === "append");
            setBatchPromptText("");
          }}
        >
          {batchPromptMode === "append" ? "Append" : "Overwrite"} Prompt → {count} Steps
        </button>
      </div>
    </section>
  );
}
