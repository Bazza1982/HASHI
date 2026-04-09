export type GraphValidationResult = {
  duplicateStepIds: string[];
  missingDependencies: string[];
  missingAgents: string[];
  cycles: string[][];
};

export function validateWorkflowGraph(workflow: Record<string, unknown>): GraphValidationResult {
  const steps = Array.isArray(workflow.steps) ? workflow.steps : [];
  const agentsBlock = isRecord(workflow.agents) ? workflow.agents : {};
  const workers = Array.isArray(agentsBlock.workers) ? agentsBlock.workers : [];
  const workerIds = new Set(
    workers
      .filter(isRecord)
      .map((worker) => worker.id)
      .filter((value): value is string => typeof value === "string"),
  );

  const duplicates: string[] = [];
  const seen = new Set<string>();
  const stepById = new Map<string, Record<string, unknown>>();

  for (const step of steps) {
    if (!isRecord(step) || typeof step.id !== "string") {
      continue;
    }
    if (seen.has(step.id) && !duplicates.includes(step.id)) {
      duplicates.push(step.id);
    }
    seen.add(step.id);
    stepById.set(step.id, step);
  }

  const missingDependencies = new Set<string>();
  const missingAgents = new Set<string>();
  const adjacency = new Map<string, string[]>();

  for (const [stepId, step] of stepById.entries()) {
    adjacency.set(stepId, []);
    const depends = Array.isArray(step.depends) ? step.depends : [];
    for (const dependency of depends) {
      if (typeof dependency !== "string") {
        continue;
      }
      if (!stepById.has(dependency)) {
        missingDependencies.add(`${stepId}->${dependency}`);
      } else {
        adjacency.get(stepId)!.push(dependency);
      }
    }

    if (workerIds.size > 0 && typeof step.agent === "string" && !workerIds.has(step.agent)) {
      missingAgents.add(`${stepId}->${step.agent}`);
    }
  }

  return {
    duplicateStepIds: [...duplicates].sort(),
    missingDependencies: [...missingDependencies].sort(),
    missingAgents: [...missingAgents].sort(),
    cycles: detectCycles(adjacency),
  };
}

function detectCycles(adjacency: Map<string, string[]>): string[][] {
  const visiting = new Set<string>();
  const visited = new Set<string>();
  const path: string[] = [];
  const cycles: string[][] = [];
  const recorded = new Set<string>();

  const visit = (node: string) => {
    if (visited.has(node)) {
      return;
    }
    if (visiting.has(node)) {
      const cycleStart = path.indexOf(node);
      const cycle = [...path.slice(cycleStart), node];
      const signature = cycle.join(">");
      if (!recorded.has(signature)) {
        recorded.add(signature);
        cycles.push(cycle);
      }
      return;
    }

    visiting.add(node);
    path.push(node);
    for (const dependency of adjacency.get(node) ?? []) {
      visit(dependency);
    }
    path.pop();
    visiting.delete(node);
    visited.add(node);
  };

  for (const node of adjacency.keys()) {
    visit(node);
  }

  return cycles;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
