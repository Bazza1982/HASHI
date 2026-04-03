import type { DraftStep } from "../lib/yamlCodec";

type StepConfigPanelProps = {
  step: DraftStep | null;
  availableStepIds: string[];
  onChange: (step: DraftStep) => void;
};

export function StepConfigPanel({ step, availableStepIds, onChange }: StepConfigPanelProps) {
  if (!step) {
    return (
      <section className="panel">
        <h2>Step</h2>
        <p className="muted">Select a node to edit supported fields.</p>
      </section>
    );
  }

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
