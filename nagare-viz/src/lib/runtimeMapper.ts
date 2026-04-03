import type { ApiRunArtifact, ApiRunEvent, ApiRunSnapshot } from "../api/nagareApi";

export type RuntimeNodeStatus = "idle" | "running" | "completed" | "failed" | "waiting_human";

export type RuntimeNodeView = {
  status: RuntimeNodeStatus;
  attempt: number;
  error: string | null;
  artifactCount: number;
};

export type RuntimeOverlayModel = {
  runtimeByStepId: Record<string, RuntimeNodeView>;
  runOnlyStepIds: string[];
  draftOnlyStepIds: string[];
  summary: {
    totalSteps: number;
    completedSteps: number;
    failedSteps: number;
    runningSteps: number;
    waitingHumanSteps: number;
  };
  eventCount: number;
  artifactCount: number;
  latestEvent: ApiRunEvent | null;
};

export function mapRuntimeToDraft(
  draftStepIds: string[],
  run: ApiRunSnapshot,
  events: ApiRunEvent[],
  artifacts: ApiRunArtifact[],
): RuntimeOverlayModel {
  const draftStepSet = new Set(draftStepIds);
  const runStepIds = Object.keys(run.step_status);
  const runtimeByStepId: Record<string, RuntimeNodeView> = {};

  for (const [stepId, step] of Object.entries(run.step_status)) {
    if (!draftStepSet.has(stepId)) {
      continue;
    }
    runtimeByStepId[stepId] = {
      status: normalizeStatus(step.status),
      attempt: step.attempt,
      error: step.error,
      artifactCount: Object.keys(step.artifacts ?? {}).length,
    };
  }

  return {
    runtimeByStepId,
    runOnlyStepIds: runStepIds.filter((stepId) => !draftStepSet.has(stepId)).sort(),
    draftOnlyStepIds: draftStepIds.filter((stepId) => !run.step_status[stepId]).sort(),
    summary: {
      totalSteps: runStepIds.length,
      completedSteps: run.completed_steps.length,
      failedSteps: run.failed_steps.length,
      runningSteps: run.current_steps.length,
      waitingHumanSteps: run.waiting_human_steps.length,
    },
    eventCount: events.length,
    artifactCount: artifacts.length,
    latestEvent: events.length > 0 ? events[events.length - 1] : null,
  };
}

function normalizeStatus(status: string): RuntimeNodeStatus {
  switch (status.toUpperCase()) {
    case "RUNNING":
      return "running";
    case "COMPLETED":
      return "completed";
    case "FAILED":
      return "failed";
    case "WAITING_HUMAN":
      return "waiting_human";
    default:
      return "idle";
  }
}
