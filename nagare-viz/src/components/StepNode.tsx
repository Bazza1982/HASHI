import type { Node, NodeProps } from "@xyflow/react";

type StepNodeData = {
  label: string;
  agent: string;
  dependsCount: number;
  selected: boolean;
  runtimeStatus?: "idle" | "running" | "completed" | "failed" | "waiting_human";
  runtimeAttempt?: number;
};

type StepFlowNode = Node<StepNodeData, "step">;

export function StepNode({ data }: NodeProps<StepFlowNode>) {
  return (
    <div className={`step-node ${data.selected ? "is-selected" : ""} ${data.runtimeStatus ? `is-${data.runtimeStatus}` : ""}`}>
      <div className="step-node__header">
        <div className="step-node__title">{data.label}</div>
        {data.runtimeStatus && data.runtimeStatus !== "idle" ? (
          <span className={`step-node__status status-${data.runtimeStatus}`}>
            {data.runtimeStatus.replace("_", " ")}
          </span>
        ) : null}
      </div>
      <div className="step-node__meta">{data.agent || "unassigned agent"}</div>
      <div className="step-node__meta">{data.dependsCount} dependencies</div>
      {data.runtimeAttempt && data.runtimeAttempt > 1 ? (
        <div className="step-node__meta">Attempt {data.runtimeAttempt}</div>
      ) : null}
    </div>
  );
}
