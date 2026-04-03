import type { DraftStep, WorkflowDraft } from "./yamlCodec";

export type NodePosition = { x: number; y: number };

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
  for (const level of sortedLevels) {
    const steps = grouped.get(level)!;
    steps.sort((left, right) => left.id.localeCompare(right.id));
    steps.forEach((step, index) => {
      metadata[step.id] = {
        position: {
          x: 120 + level * 320,
          y: 96 + index * 168,
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
