import { useCallback, useEffect, useRef } from "react";
import {
  Background,
  Controls,
  MarkerType,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type NodeMouseHandler,
  type Edge,
  type Node,
} from "@xyflow/react";
import { StepNode } from "./StepNode";

const nodeTypes = { step: StepNode };

type WorkflowCanvasProps = {
  nodes: Node[];
  edges: Edge[];
  onSelectStep: (stepId: string | null) => void;
  onNodeDragStop: NodeMouseHandler<Node>;
  onAutoLayout?: () => void;
};

function InnerCanvas({
  nodes,
  edges,
  onSelectStep,
  onNodeDragStop,
}: WorkflowCanvasProps) {
  const { fitView } = useReactFlow();
  const prevNodesKey = useRef("");

  useEffect(() => {
    // Build a key from node ids + positions to detect any change
    const key = nodes.map((n) => `${n.id}:${n.position.x}:${n.position.y}`).join("|");
    if (key !== prevNodesKey.current) {
      prevNodesKey.current = key;
      const timer = setTimeout(() => fitView({ padding: 0.05, duration: 300 }), 300);
      return () => clearTimeout(timer);
    }
  }, [nodes, fitView]);

  const handleInit = useCallback(() => {
    setTimeout(() => fitView({ padding: 0.05, duration: 300 }), 300);
  }, [fitView]);

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges.map((edge) => ({
        ...edge,
        type: "smoothstep",
        markerEnd: { type: MarkerType.ArrowClosed, width: 20, height: 20, color: "#0d6b5f" },
        style: { stroke: "#0d6b5f", strokeWidth: 2.5 },
      }))}
      nodeTypes={nodeTypes}
      onNodeClick={(_event, node) => onSelectStep(node.id)}
      onPaneClick={() => onSelectStep(null)}
      onNodeDragStop={onNodeDragStop}
      onInit={handleInit}
      minZoom={0.15}
      fitView
      fitViewOptions={{ padding: 0.05 }}
    >
      <MiniMap
        pannable
        zoomable
        nodeColor={(node) => {
          const status = (node.data as any)?.runtimeStatus;
          if (status === "running") return "#f59e0b";
          if (status === "completed") return "#22c55e";
          if (status === "failed") return "#ef4444";
          if (status === "waiting_human") return "#f59e0b";
          return "#5eead4";
        }}
        nodeStrokeColor="#0d6b5f"
        nodeStrokeWidth={2}
        maskColor="rgba(15, 40, 35, 0.55)"
        style={{ background: "#1a2e2b" }}
      />
      <Controls />
      <Background gap={24} size={1} />
    </ReactFlow>
  );
}

export function WorkflowCanvas(props: WorkflowCanvasProps) {
  return (
    <div className="canvas-shell">
      <ReactFlowProvider>
        <InnerCanvas {...props} />
      </ReactFlowProvider>
      {props.onAutoLayout && (
        <button className="canvas-auto-layout-btn" onClick={props.onAutoLayout} title="重新排列工作流 — Auto-layout">
          ⇲ Auto-layout
        </button>
      )}
      <div className="canvas-legend">
        <strong>▼</strong> arrows = dependency flow (prerequisite → dependent)
      </div>
    </div>
  );
}
