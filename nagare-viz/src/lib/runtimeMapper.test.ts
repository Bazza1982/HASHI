import { describe, expect, it } from "vitest";
import { mapRuntimeToDraft } from "./runtimeMapper";

describe("mapRuntimeToDraft", () => {
  it("maps step state by stable step id and surfaces mismatches", () => {
    const overlay = mapRuntimeToDraft(
      ["step_write", "step_check", "step_extra"],
      {
        run_id: "run-123",
        workflow_id: "smoke-test",
        workflow_version: "1.0.0",
        workflow_path: "/tmp/workflow.yaml",
        status: "RUNNING",
        created_at: "2026-04-03T00:00:00Z",
        updated_at: "2026-04-03T00:01:00Z",
        current_steps: ["step_check"],
        completed_steps: ["step_write"],
        failed_steps: [],
        waiting_human_steps: [],
        step_status: {
          step_write: {
            status: "COMPLETED",
            attempt: 1,
            started_at: null,
            ended_at: null,
            artifacts: { draft: "draft.txt" },
            error: null,
          },
          step_check: {
            status: "RUNNING",
            attempt: 2,
            started_at: null,
            ended_at: null,
            artifacts: {},
            error: null,
          },
          step_legacy: {
            status: "FAILED",
            attempt: 1,
            started_at: null,
            ended_at: null,
            artifacts: {},
            error: "legacy mismatch",
          },
        },
        error_count: 0,
        human_intervention_count: 0,
      },
      [
        {
          timestamp: "2026-04-03T00:01:00Z",
          level: "INFO",
          component: "engine.runner",
          event: "step.started",
          message: "Running step_check",
          run_id: "run-123",
          trace_id: "trace",
          request_id: null,
          workflow_id: "smoke-test",
          workflow_path: "/tmp/workflow.yaml",
          step_id: "step_check",
          duration_ms: null,
          error_code: null,
          error_message: null,
          data: {},
        },
      ],
      [
        {
          key: "draft",
          path: "/tmp/draft.txt",
          original_path: "/tmp/original.txt",
          step_id: "step_write",
          size_bytes: 42,
          registered_at: "2026-04-03T00:00:30Z",
        },
      ],
    );

    expect(overlay.runtimeByStepId.step_write.status).toBe("completed");
    expect(overlay.runtimeByStepId.step_check.status).toBe("running");
    expect(overlay.runtimeByStepId.step_write.artifactCount).toBe(1);
    expect(overlay.runOnlyStepIds).toEqual(["step_legacy"]);
    expect(overlay.draftOnlyStepIds).toEqual(["step_extra"]);
    expect(overlay.latestEvent?.event).toBe("step.started");
    expect(overlay.artifactCount).toBe(1);
  });
});
