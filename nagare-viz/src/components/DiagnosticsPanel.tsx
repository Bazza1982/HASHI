import type { DiagnosticLog } from "../lib/logger";
import type { ValidationIssue } from "../lib/workflowSafety";
import { CollapsiblePanel } from "./CollapsiblePanel";

type DiagnosticsPanelProps = {
  correlationId: string;
  parserIssue: string | null;
  validationIssues: ValidationIssue[];
  exportIssues: ValidationIssue[];
  logs: DiagnosticLog[];
};

export function DiagnosticsPanel({
  correlationId,
  parserIssue,
  validationIssues,
  exportIssues,
  logs,
}: DiagnosticsPanelProps) {
  return (
    <CollapsiblePanel title="Diagnostics" defaultCollapsed>
      <div className="diagnostics-grid">
        <div className="diagnostic-meta">
          <strong>Correlation ID</strong>
          <code>{correlationId}</code>
        </div>
        <div className="diagnostic-meta">
          <strong>Parser Issues</strong>
          <p>{parserIssue ?? "None"}</p>
        </div>
        <div className="diagnostic-meta">
          <strong>Validation Issues</strong>
          <p>{validationIssues.length}</p>
        </div>
        <div className="diagnostic-meta">
          <strong>Export Diagnostics</strong>
          <p>{exportIssues.length}</p>
        </div>
      </div>
      <DiagnosticIssueList
        emptyLabel="No parser or export issues."
        issues={[
          ...(parserIssue
            ? [
                {
                  id: "parser-issue",
                  severity: "blocking" as const,
                  title: "Parser issue",
                  detail: parserIssue,
                },
              ]
            : []),
          ...exportIssues,
        ]}
        title="Current Risk Snapshot"
      />
      <DiagnosticIssueList
        emptyLabel="No validation details to report."
        issues={validationIssues}
        title="Validation Detail"
      />
      {logs.length === 0 ? (
        <p className="muted">No front-end events recorded yet.</p>
      ) : (
        <div className="log-list">
          {logs.map((log) => (
            <div className={`log-item ${log.level}`} key={log.id}>
              <div className="log-head">
                <strong>{log.event}</strong>
                <span>{new Date(log.timestamp).toLocaleTimeString()}</span>
              </div>
              <p>{log.message}</p>
            </div>
          ))}
        </div>
      )}
    </CollapsiblePanel>
  );
}

type DiagnosticIssueListProps = {
  title: string;
  emptyLabel: string;
  issues: Array<Pick<ValidationIssue, "id" | "severity" | "title" | "detail">>;
};

function DiagnosticIssueList({ title, emptyLabel, issues }: DiagnosticIssueListProps) {
  return (
    <section className="diagnostic-section">
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
