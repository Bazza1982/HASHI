import {
  Background,
  Controls,
  MarkerType,
  MiniMap,
  ReactFlow,
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
};

export function WorkflowCanvas({
  nodes,
  edges,
  onSelectStep,
  onNodeDragStop,
}: WorkflowCanvasProps) {
  return (
    <div className="canvas-shell">
      <ReactFlow
        fitView
        nodes={nodes}
        edges={edges.map((edge) => ({
          ...edge,
          markerEnd: { type: MarkerType.ArrowClosed, width: 18, height: 18 },
        }))}
        nodeTypes={nodeTypes}
        onNodeClick={(_event, node) => onSelectStep(node.id)}
        onPaneClick={() => onSelectStep(null)}
        onNodeDragStop={onNodeDragStop}
      >
        <MiniMap pannable zoomable />
        <Controls />
        <Background gap={24} size={1} />
      </ReactFlow>
      <div className="canvas-legend">
        <strong>Dependency direction:</strong> arrows run from prerequisite step to dependent step. Layout is visual only.
      </div>
    </div>
  );
}
