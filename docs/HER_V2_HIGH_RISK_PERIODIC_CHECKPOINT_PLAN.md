# Retired: HER v2 High-Risk Periodic Checkpoint Plan

Status: **superseded and not an active HER v2 contract**.

This document name is retained only so older links resolve. The optional
`STANDARD`/`HIGH_RISK` checkpoint-assessor design was incorrect: it allowed a
model to choose `CONTINUE`, `USER_INPUT_REQUIRED`, or `HALT`, emitted no
commentary, and used risk metadata as the cadence gate. That behaviour has been
removed from code and tests.

The authoritative replacement is the
[HER v2 Compulsory Replanning Repair Plan](HER_V2_COMPULSORY_REPLAN_REPAIR_PLAN.md).
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
