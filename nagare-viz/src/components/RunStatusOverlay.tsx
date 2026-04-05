import { useState } from "react";
import type { ApiRunSnapshotResponse } from "../api/nagareApi";
import type { RuntimeOverlayModel } from "../lib/runtimeMapper";

type RunStatusOverlayProps = {
  apiBaseUrl: string;
  runIdInput: string;
  activeRun: ApiRunSnapshotResponse | null;
  overlay: RuntimeOverlayModel | null;
  loading: boolean;
  error: string | null;
  onApiBaseUrlChange: (value: string) => void;
  onRunIdInputChange: (value: string) => void;
  onConnect: () => void;
  onDisconnect: () => void;
  onRefresh: () => void;
  inline?: boolean;
};

export function RunStatusOverlay({
  apiBaseUrl,
  runIdInput,
  activeRun,
  overlay,
  loading,
  error,
  onApiBaseUrlChange,
  onRunIdInputChange,
  onConnect,
  onDisconnect,
  onRefresh,
  inline = false,
}: RunStatusOverlayProps) {
  const run = activeRun?.run ?? null;
  const [collapsed, setCollapsed] = useState(true);

  return (
    <section className={`run-overlay panel ${collapsed ? "run-overlay--collapsed" : ""} ${inline ? "run-overlay--inline" : ""}`}>
      <div className="panel-row">
        <button
          className="run-overlay__collapse-toggle"
          onClick={() => setCollapsed(!collapsed)}
          type="button"
          title={collapsed ? "Expand Live Run panel" : "Collapse"}
        >
          {collapsed ? "▸" : "▾"} <span className="eyebrow" style={{ margin: 0 }}>Live Run</span>
          {run && <span className={`status-chip status-${run.status.toLowerCase()}`} style={{ marginLeft: "0.5rem", fontSize: "0.7rem" }}>{run.status}</span>}
        </button>
        {!collapsed && (
          <div className="run-overlay__actions">
            <button onClick={onRefresh} type="button">
              Refresh
            </button>
            {run ? (
              <button onClick={onDisconnect} type="button">
                Disconnect
              </button>
            ) : (
              <button onClick={onConnect} type="button">
                Observe
              </button>
            )}
          </div>
        )}
      </div>
      {!collapsed && (
        <>
          <div className="run-overlay__controls">
            <label className="field">
              <span>API Base URL</span>
              <input value={apiBaseUrl} onChange={(event) => onApiBaseUrlChange(event.target.value)} />
            </label>
            <label className="field">
              <span>Run ID</span>
              <input
                placeholder="run-20260403-123456"
                value={runIdInput}
                onChange={(event) => onRunIdInputChange(event.target.value)}
              />
            </label>
          </div>
          {loading ? <p className="muted">Refreshing snapshot...</p> : null}
          {error ? <p className="run-overlay__error">{error}</p> : null}
          {run ? (
            <>
              <div className="run-overlay__status">
                <span className={`status-chip status-${run.status.toLowerCase()}`}>{run.status}</span>
                <span>Workflow: {run.workflow_id ?? "unknown"}</span>
                <span>Request: {activeRun?.request_id}</span>
              </div>
              <div className="diagnostics-grid">
                <div className="diagnostic-meta">
                  <strong>Running</strong>
                  <p>{overlay?.summary.runningSteps ?? 0}</p>
                </div>
                <div className="diagnostic-meta">
                  <strong>Completed</strong>
                  <p>{overlay?.summary.completedSteps ?? 0}</p>
                </div>
                <div className="diagnostic-meta">
                  <strong>Failed</strong>
                  <p>{overlay?.summary.failedSteps ?? 0}</p>
                </div>
                <div className="diagnostic-meta">
                  <strong>Artifacts</strong>
                  <p>{overlay?.artifactCount ?? 0}</p>
                </div>
              </div>
              <div className="warning-list">
                {overlay?.runOnlyStepIds.length ? (
                  <div className="warning-item warning">
                    <strong>Run-only steps</strong>
                    <span>{overlay.runOnlyStepIds.join(", ")}</span>
                  </div>
                ) : null}
                {overlay?.draftOnlyStepIds.length ? (
                  <div className="warning-item info">
                    <strong>Draft-only steps</strong>
                    <span>{overlay.draftOnlyStepIds.join(", ")}</span>
                  </div>
                ) : null}
                {!overlay?.runOnlyStepIds.length && !overlay?.draftOnlyStepIds.length ? (
                  <p className="ok">Draft and observed run align by stable step id.</p>
                ) : null}
              </div>
              <div className="run-overlay__footer">
                <span>Updated: {run.updated_at ?? "unknown"}</span>
                <span>Latest event: {overlay?.latestEvent?.event ?? "none"}</span>
                <span>Events loaded: {overlay?.eventCount ?? 0}</span>
              </div>
            </>
          ) : (
            <p className="muted">
              Attach a run snapshot to overlay runtime state on the current draft without mutating editor data.
            </p>
          )}
        </>
      )}
    </section>
  );
}
