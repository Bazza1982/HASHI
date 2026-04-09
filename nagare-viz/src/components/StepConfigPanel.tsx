import type { DraftStep } from "../lib/yamlCodec";

export type WorkerInfo = {
  id: string;
  role: string;
  backend: string;
  model: string;
};

type StepConfigPanelProps = {
  step: DraftStep | null;
  availableStepIds: string[];
  workers: WorkerInfo[];
  onChange: (step: DraftStep) => void;
  onWorkerChange: (workerId: string, field: "backend" | "model", value: string) => void;
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

export function StepConfigPanel({ step, availableStepIds, workers, onChange, onWorkerChange }: StepConfigPanelProps) {
  if (!step) {
    return (
      <section className="panel">
        <h2>Step</h2>
        <p className="muted">Select a node to edit supported fields.</p>
      </section>
    );
  }

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
