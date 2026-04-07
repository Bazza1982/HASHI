import { useState } from "react";
import type { DraftStep } from "../lib/yamlCodec";

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
  onBatchStepChange: (field: "agent" | "timeoutSeconds", value: string | number | null) => void;
  onBatchPromptChange: (text: string, append: boolean) => void;
  onBatchWorkerChange: (field: "backend" | "model", value: string) => void;
};

// HASHI available backends and their models
const BACKEND_MODELS: Record<string, string[]> = {
  "callable": [],
  "claude-cli": [
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
  ],
  "openrouter-api": [
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "gpt-4o",
    "gpt-4o-mini",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "deepseek-chat",
    "deepseek-reasoner",
  ],
  "deepseek-api": [
    "deepseek-chat",
    "deepseek-reasoner",
  ],
  "gemini-cli": [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
  ],
  "codex-cli": [
    "codex-mini",
  ],
};

const ALL_BACKENDS = Object.keys(BACKEND_MODELS);

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
    const availableModels = BACKEND_MODELS[currentBackend] ?? [];

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
                {!ALL_BACKENDS.includes(currentBackend) && (
                  <option value={currentBackend}>{currentBackend}</option>
                )}
                {ALL_BACKENDS.map((b) => (
                  <option key={b} value={b}>{b}</option>
                ))}
              </select>
            </label>
            {currentBackend !== "callable" && (
              <label className="field">
                <span>Model</span>
                <select
                  value={currentModel}
                  onChange={(event) => onWorkerChange(worker.id, "model", event.target.value)}
                >
                  {currentModel && !availableModels.includes(currentModel) && (
                    <option value={currentModel}>{currentModel} (current)</option>
                  )}
                  <option value="">— select model —</option>
                  {availableModels.map((m) => (
                    <option key={m} value={m}>{m}</option>
                  ))}
                </select>
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
          <span>Timeout Seconds</span>
          <input
            type="number"
            min="0"
            value={step.timeoutSeconds ?? ""}
            onChange={(event) =>
              onChange({
                ...step,
                timeoutSeconds:
                  event.target.value.trim().length === 0 ? null : Number(event.target.value),
              })
            }
          />
        </label>
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
  const availableModels = BACKEND_MODELS[batchBackend] ?? [];

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
              <option value="">— {uniqueBackends.length > 1 ? "mixed" : "select"} —</option>
              {ALL_BACKENDS.map((b) => (
                <option key={b} value={b}>{b}</option>
              ))}
            </select>
          </label>
          {batchBackend !== "callable" && (
            <label className="field">
              <span>Model</span>
              <select
                value={sharedModel}
                onChange={(event) => onBatchWorkerChange("model", event.target.value)}
              >
                <option value="">— {uniqueModels.length > 1 ? "mixed" : "select"} —</option>
                {sharedModel && !availableModels.includes(sharedModel) && (
                  <option value={sharedModel}>{sharedModel} (current)</option>
                )}
                {availableModels.map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
            </label>
          )}
        </>
      )}

      <label className="field">
        <span>Timeout Seconds</span>
        <input
          type="number"
          min="0"
          placeholder="— set for all —"
          defaultValue=""
          onBlur={(event) => {
            const v = event.target.value.trim();
            if (v.length > 0) onBatchStepChange("timeoutSeconds", Number(v));
          }}
        />
      </label>

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
