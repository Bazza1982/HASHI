# HER v2 Planned / Medium Stage-Tool Decision

| Field | Accepted value |
|---|---|
| Status | Accepted and frozen |
| Date | 2026-08-31 |
| Scope | HER v2 `/effort medium` / Planned only |
| Strategy | No tool access; resolve the goal, classify, select Strategy Cards and Habits, and provide strategic direction |
| Planning | Mechanically read-only tools; investigate current evidence and produce the concrete execution plan |
| Execution | Full authorised tool and side-effect access; implement, verify, and report |

## Decision

The canonical Planned/Medium work path is:

```text
Strategy (no tools)
  -> Planning (read-only investigation tools, no side effects)
  -> Execution (full authorised tools and side effects)
```

Planning may inspect the workspace, locate relevant implementation paths,
identify concrete candidates, and determine verification steps. It must return
a plan rather than modify artifacts, apply fixes, or complete the downstream
task. Runtime enforces this boundary with `allow_side_effects=False`; the prompt
is explanatory, not the security boundary.

The Strategy handoff and selected Strategy Card snapshots pass into Planning.
Both the resulting active plan and the Strategy handoff then pass into
Execution. Execution remains responsible for all mutations and final
verification.

## Evidence

The accepted single-variable SAM3 debugging A/B held the provider, model,
reasoning, task, Strategy tool access, Execution access, workspace isolation,
and verifier constant. Only Planning tool access changed.

| Metric | Planning without tools | Planning with read-only tools | Change |
|---|---:|---:|---:|
| Hidden verification | 6/6, quality 1.000 | 6/6, quality 1.000 | Equal |
| Agent time | 3231.5 s | 945.2 s | 70.8% lower |
| Total tokens | 13,305,255 | 1,879,606 | 85.9% lower |
| Tool calls | 164 | 72 | 56.1% lower |
| Provider calls | 129 | 55 | 57.4% lower |
| Thinking tokens | 39,271 | 21,342 | 45.7% lower |
| Cache-aware estimated cost | $0.074228 | $0.025258 | 66.0% lower |

A separate directional comparison against the earlier Strategy-tools baseline
also preserved 6/6 quality while the Planning-tools path used 41.4% less time
and 64.4% fewer total tokens. That comparison was not the clean
single-variable A/B, so it supports but does not replace the result above.

## Boundaries

- This decision freezes the product default for Planned/Medium. It does not
  claim that Planning improved hidden quality in this task; both arms passed.
- The measured result is one difficult SAM3 task with one completed sample per
  arm. Its efficiency magnitude is directional until replicated across more
  tasks, but the stage ownership is accepted for product development.
- Strategic/Low keeps its existing no-Planning path and existing Strategy tool
  ownership. Direct/Zero is unchanged.
- Adaptive/High, Reviewed/XHigh, and Assured/Max are retained internally but
  removed from the production selector while their design is postponed. This
  Medium decision does not choose their future policy.
- Planning receives only tools that HASHI mechanically exposes as read-only.
  A tool whose interface can mutate state is excluded even if the model
  promises not to use its mutating operation.

## Completion criterion

Medium-stage investigation is complete. Further work on Planned/Medium is
regression maintenance, provider compatibility, or broader replication—not a
reason to reopen the accepted stage ownership without contrary evidence.
