import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";

type StepNodeData = {
  label: string;
  agent: string;
  dependsCount: number;
  selected: boolean;
  runtimeStatus?: "idle" | "running" | "completed" | "failed" | "waiting_human";
  runtimeAttempt?: number;
  backend?: string;
  model?: string;
};

type StepFlowNode = Node<StepNodeData, "step">;

/** Short display name for model IDs */
function shortModel(model: string): string {
  return model
    .replace("claude-opus-4-6", "Opus 4.6")
    .replace("claude-sonnet-4-6", "Sonnet 4.6")
    .replace("claude-haiku-4-5", "Haiku 4.5")
    .replace("gemini-2.5-pro", "Gemini Pro")
    .replace("gemini-2.5-flash", "Gemini Flash")
    .replace("deepseek-reasoner", "DS-R1")
    .replace("deepseek-chat", "DS-V3");
}

export function StepNode({ data }: NodeProps<StepFlowNode>) {
  return (
    <div className={`step-node ${data.selected ? "is-selected" : ""} ${data.runtimeStatus ? `is-${data.runtimeStatus}` : ""}`}>
      <Handle type="target" position={Position.Top} className="step-node__handle" />
      <div className="step-node__header">
        <div className="step-node__title">{data.label}</div>
        {data.runtimeStatus && data.runtimeStatus !== "idle" ? (
          <span className={`step-node__status status-${data.runtimeStatus}`}>
            {data.runtimeStatus.replace("_", " ")}
          </span>
        ) : null}
      </div>
      <div className="step-node__meta">{data.agent || "unassigned agent"}</div>
      {data.backend && data.backend !== "callable" && data.model ? (
        <div className="step-node__model">{shortModel(data.model)}</div>
      ) : data.backend === "callable" ? (
        <div className="step-node__model step-node__model--callable">callable</div>
      ) : null}
      <div className="step-node__meta">{data.dependsCount} dependencies</div>
      {data.runtimeAttempt && data.runtimeAttempt > 1 ? (
        <div className="step-node__meta">Attempt {data.runtimeAttempt}</div>
      ) : null}
      <Handle type="source" position={Position.Bottom} className="step-node__handle" />
    </div>
  );
}
