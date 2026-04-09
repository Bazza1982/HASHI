import type { DraftStep, WorkflowDraft } from "./yamlCodec";

export type NodePosition = { x: number; y: number };

/**
 * Auto-layout: top-to-bottom DAG layout.
 * Steps at the same dependency depth are placed in the same row.
 * Rows flow top → bottom, columns spread left → right, centered.
 */
export function autoLayout(draft: WorkflowDraft): Record<string, { position: NodePosition }> {
  const levels = computeLevels(draft.steps);
  const grouped = new Map<number, DraftStep[]>();

  for (const step of draft.steps) {
    const level = levels.get(step.id) ?? 0;
    const bucket = grouped.get(level) ?? [];
    bucket.push(step);
    grouped.set(level, bucket);
  }

  const metadata: Record<string, { position: NodePosition }> = {};
  const sortedLevels = [...grouped.keys()].sort((left, right) => left - right);

  const NODE_WIDTH = 240;
  const H_GAP = 40;
  const V_GAP = 40;

  for (const level of sortedLevels) {
    const steps = grouped.get(level)!;
    steps.sort((left, right) => left.id.localeCompare(right.id));

    // Center each row horizontally around x=0
    const rowWidth = steps.length * NODE_WIDTH + (steps.length - 1) * H_GAP;
    const startX = -rowWidth / 2;

    steps.forEach((step, index) => {
      metadata[step.id] = {
        position: {
          x: Math.round(startX + index * (NODE_WIDTH + H_GAP)),
          y: Math.round(level * (70 + V_GAP)),
        },
      };
    });
  }

  return metadata;
}

function computeLevels(steps: DraftStep[]): Map<string, number> {
  const byId = new Map(steps.map((step) => [step.id, step]));
  const memo = new Map<string, number>();
  const visiting = new Set<string>();

  const visit = (stepId: string): number => {
    if (memo.has(stepId)) {
      return memo.get(stepId)!;
    }
    if (visiting.has(stepId)) {
      return 0;
    }
    visiting.add(stepId);
    const step = byId.get(stepId);
    const dependencies = step?.depends.filter((dependency) => byId.has(dependency)) ?? [];
    const level =
      dependencies.length === 0
        ? 0
        : Math.max(...dependencies.map((dependency) => visit(dependency))) + 1;
    visiting.delete(stepId);
    memo.set(stepId, level);
    return level;
  };

  for (const step of steps) {
    visit(step.id);
  }
  return memo;
}
