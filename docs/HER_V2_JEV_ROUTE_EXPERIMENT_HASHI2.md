# HER v2 JEV route experiment — HASHI2

Status: implemented in the HASHI2 Functions layer; live adoption remains
unverified because this change did not restart a running service.

## Decision

JEV owns one closed `Choice` at the Strategy boundary. The active choices are
exactly:

- `DIRECT_RESPONSE`
- `SIMPLE_TASK`
- `COMPLEX_TASK`
- `CONFIRMATION_REQUIRED`

JEV chooses only the workflow category. The normal Strategy model continues to
resolve `real_goal`, select the existing Strategy Cards and Habits, and write the
execution brief. JEV probabilities and confidence are retained as uncertainty
evidence, not treated as a guarantee of certainty.

`CONFIRMATION_REQUIRED` is deliberately narrow. It is selected only when the
goal, target, execution scope, or required user choice is still materially
unclear and no safe bounded first step can be chosen. Missing or uncertain user
authorization, ownership, consent, permissions, risk acceptance, private
authorization metadata, or agent-selectable technical parameters are not triage
clarification triggers. The typed request envelope is already the authority input
for the turn, but HER's goal sorting, planning, and execution stages must not
interpret it as a conversational gate: they must not ask, re-confirm, predict a
denial, or make an authority check the first plan step. Only an actual tool result
or observed failure may change the execution report.

Planning and Execution have the same non-interactive boundary: Planning only
constructs the plan; it must not ask for authorization or wait for approval.
Execution must carry out the plan and resolved goal. Strategy Card text about
authorization, confirmation, or HITL is not copied into the plan, and an imagined
gate is never treated as execution evidence. Actual tool errors remain reportable
as observed failures.

`HIGH_VOLUME_TASK` remains readable only for old ledgers/replays. New JEV
routing never emits it; the former category is represented by
`COMPLEX_TASK`.

## Serial pilot

When `her_v2.route_judgment.serial_initial_response` is enabled, HER v2:

1. emits a tool-free acknowledgement;
2. runs the JEV route judgment against the frozen turn context;
3. runs the existing Strategy/Card stage with the JEV route as authority; and
4. generates a substantive Immediate Response only after a direct route is
   known, or proceeds to the existing work/clarification path.

JEV failure is audited and safely falls back to the existing Strategy model.
No strategy cards, permission boundary, PCM authority, or Core code is changed.

## Verification

Focused tests cover the four-choice wire contract, retired-choice rejection,
route authority in the Strategy prompt, serial ordering, existing Strategy Card
selection, and the existing HER v2 Runtime/Adapter suite. Live provider
adoption requires the normal Functions hot reboot and a real Agent test.
