> **Legacy HERV2 archive — non-normative.** This document records the retired staged runtime. HERV3 uses one continuous main-model/tool loop with no Triage, Strategy, Planning, Replanning, Review, or stage-based Finalisation routing. Strategy Cards and Habit/Meditation remain optional. See [HERV3 upgrade](HERV3_UPGRADE.md).

# Retired: HERV2 High-Risk Periodic Checkpoint Plan

Status: **superseded and not an active HERV2 contract**.

This document name is retained only so older links resolve. The optional
model-authored risk-label checkpoint-assessor design was incorrect: it allowed
a model to choose `CONTINUE`, `USER_INPUT_REQUIRED`, or `HALT`, emitted no
commentary, and used risk metadata as the cadence gate. That behaviour has been
removed from code and tests.

The authoritative replacement is the
[HERV2 Compulsory Replanning Repair Plan](HER_V2_COMPULSORY_REPLAN_REPAIR_PLAN.md).
In summary:

- Adaptive (`high`), Reviewed (`xhigh`), and Assured (`max`) are eligible;
- after Execution starts, 10 completed tool results or 300 seconds forces
  Replanning at the next safe boundary;
- the threshold detector makes no model decision;
- each Replan answers completion, plan-suitability, and commentary questions;
- each Replan activates a plan version and sends exactly one Persona-rendered
  or deterministic fallback update;
- completion below 100% resumes work without replaying side effects; 100% stops
  adding work and proceeds to assurance or Finalisation; and
- no Replan-count, time, token, turn, tool-loop, or whole-workflow ceiling is
  introduced.
