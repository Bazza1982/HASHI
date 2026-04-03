import type { ValidationIssue } from "../lib/workflowSafety";

type ValidationPanelProps = {
  compatibilityClass: string;
  issues: ValidationIssue[];
};

export function ValidationPanel({ compatibilityClass, issues }: ValidationPanelProps) {
  const blocking = issues.filter((issue) => issue.severity === "blocking");
  const warnings = issues.filter((issue) => issue.severity === "warning");
  const informational = issues.filter((issue) => issue.severity === "info");

  return (
    <section className="panel">
      <h2>Validation</h2>
      <p className="muted">Compatibility class: {compatibilityClass}</p>
      <div className="validation-groups">
        <ValidationGroup emptyLabel="No blocking issues." issues={blocking} title="Blocking" />
        <ValidationGroup emptyLabel="No active warnings." issues={warnings} title="Warnings" />
        <ValidationGroup emptyLabel="No informational notes." issues={informational} title="Info" />
      </div>
    </section>
  );
}

type ValidationGroupProps = {
  title: string;
  emptyLabel: string;
  issues: ValidationIssue[];
};

function ValidationGroup({ title, emptyLabel, issues }: ValidationGroupProps) {
  return (
    <section className="validation-group">
      <div className="panel-row">
        <strong>{title}</strong>
        <span className="muted">{issues.length}</span>
      </div>
      <div className="warning-list">
        {issues.length === 0 ? <p className="ok">{emptyLabel}</p> : null}
        {issues.map((issue) => (
          <div className={`warning-item ${issue.severity}`} key={issue.id}>
            <strong>{issue.title}</strong>
            <span>{issue.detail}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
