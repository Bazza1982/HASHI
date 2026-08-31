# HER v2 Three-Mode Production Decision

| Field | Accepted value |
|---|---|
| Status | Accepted and frozen for practical deployment |
| Date | 2026-08-31 |
| Scope | HASHI3 HER v2 public `/effort` surface |
| Public modes | Direct (`zero`), Strategic (`low`), Planned (`medium`) |
| Default | Planned (`medium`) |
| Deferred | Adaptive (`high`), Reviewed (`xhigh`), Assured (`max`) redesign |

## Decision

HER v2 exposes exactly three production execution modes:

| Mode | Wire value | Active path | Intended use |
|---|---|---|---|
| Direct | `zero` | One fully capable Quick agent | Conversation, simple actions, and lowest orchestration overhead |
| Strategic | `low` | Strategy and selected Cards, then fully capable Execution | General work that benefits from a task-matched method without formal Planning |
| Planned | `medium` | No-tool Strategy, read-only Planning, then fully capable Execution | Complex coding, investigation, and multi-step work needing a concrete evidence-based plan |

These modes are orchestration policies. They do not change provider reasoning,
tool-call ceilings, filesystem authority, or user-granted scope.

## In-turn semantic progress

The three public modes use stage-authored, event-driven Commentary only:

| Mode | Semantic Commentary contract |
|---|---|
| Direct | None; observable work uses `/verbose` and completion uses the final response |
| Strategic | One Strategy milestone after validated Strategy and before Execution |
| Planned | One Strategy milestone, then one Planning milestone before Execution |

Strategy and Planning write these milestones directly in the current typed PCM
Persona. Persona authority is presentation-only and cannot change the resolved
goal, strategy, plan, facts, permissions, or workflow authority. The validated
stage message enters the existing typed Commentary router without a second
Persona model invocation. The isolated Persona renderer remains available for
required clarification and control messages.

Runtime timer heartbeats are intentionally excluded. Tool calls and other
observable Execution activity remain owned by `/verbose`; the completed
Execution outcome remains owned by the final response.

## Compatibility

- Canonical persisted and wire values remain `zero`, `low`, and `medium`.
- `/effort direct`, `/effort strategic`, and `/effort planned` select those
  canonical values.
- Legacy aliases `fast` and `fast_path` continue to select Strategic (`low`).
- Existing saved `high`, `xhigh`, or `max` HER values migrate to Planned
  (`medium`) when Agent state loads.
- The internal enum and higher-mode implementation remain loadable for
  historical artifacts and isolated regression coverage. They are not shown in
  menus and cannot be newly selected through `/effort`.

## Deferred work

Replanning, independent Review, and assurance-oriented modes are postponed.
Their triggers, authority model, real-world value, and interaction with a
single continuous Execution thread require a separate redesign and evidence
cycle. Repetitive failures alone are telemetry, not proof that Replanning is
required or likely to repair the task.

No production path should depend on the dormant higher modes until that design
is explicitly reopened and accepted.

## Release criterion

The three-mode surface is ready when:

1. `/effort` displays only Direct, Strategic, and Planned;
2. descriptive and legacy aliases resolve to canonical values;
3. retired saved values migrate safely to Planned;
4. Direct, Strategic, and Planned preserve their tested stage and tool-access
   boundaries; and
5. current operator documentation describes the same three-mode contract.
