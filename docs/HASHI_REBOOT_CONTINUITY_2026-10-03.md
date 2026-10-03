# HASHI Reboot Continuity Decision

Status: approved target architecture; start-progress barrier implemented offline;
stable-boundary deployment and live acceptance remain pending.

Decision date: 2026-10-03

Owners: PAO (replacement transaction and receipts), Frontend Connector (stable
API and typed progress), platform configuration (independent listener
supervision).  This work is entirely outside protected Core.

## Required command boundary

`/reboot min` replaces the selected Agent Worker and only Agent-local owners
that can use the same isolated prepare/drain/commit/rollback transaction.  It
must not restart shared intake, other Agents, Scheduler, Backend API, API
Gateway, or Core.  The target may stop accepting new side effects during the
short commit window, while its durable status, transcript and reboot operation
remain readable through the stable API entry.

`/reboot max` replaces every hot-replaceable business Function and Agent
Worker.  It must not close the public Backend API or API Gateway listeners.
Their implementation generation is fixed at cold start.  A max candidate uses
private generation-specific backend endpoints; the stable entry first drains
the old target, then atomically publishes the candidate target.  Requests not
yet sent to a business target may wait behind a bounded switch barrier.  A
request whose bytes reached a target is never automatically replayed after an
unknown outcome.

`/restart` is the only adoption path for protected Core and stable API/API
Gateway listener implementation.  A max receipt must distinguish business
Functions it adopted from stable-entry or Core changes still waiting for a
cold restart.

## Owner and replacement matrix

| Owner | Current process | min | max | restart |
|---|---|---:|---:|---:|
| Agent model, tools, HER, Agent command execution | per-Agent Function Worker | selected Agent | all running Agents | recovery only |
| Agent-local ingress state and isolated caches | Worker plus Supervisor handle | only when the owner has a transferable snapshot and rollback | all eligible owners | incompatible state migration |
| Telegram/WhatsApp shared intake and routing | shared Functions | no reinitialization | business generation drain/switch behind stable entry | stable protocol change |
| Scheduler and background-job managers | shared Functions | unchanged | drain old generation, activate candidate, then switch | incompatible persistence change |
| Workbench business handlers | currently shared with listener | unchanged | private backend target may change | public listener/handler contract code |
| Backend API public listener and reboot progress read | to be independent platform-supervised stable boundary | unchanged | remains online; target pointer changes only | code/listener generation |
| API Gateway business backend | currently shared with listener | unchanged | private backend target may change | public listener/gateway contract code |
| API Gateway public listener | to be independent platform-supervised stable boundary | unchanged | remains online | code/listener generation |
| Remote | independently supervised Remote | unchanged | unchanged | separate explicit Remote lifecycle when its own code changes |
| Core process supervision and handoff protocol | protected Core | unchanged | unchanged | cold replacement only |

An owner is not declared Agent-local merely because its module name contains an
Agent name.  It needs a real isolated state snapshot, a bounded drain, one
publish point, and rollback evidence before min may include it.

## Why the old broad handoff cannot provide listener continuity

The protected Core currently prepares a shared candidate, asks the old shared
process to quiesce, closes that process, and only then activates the candidate.
Backend API and API Gateway are children of the old shared process and bind the
public ports themselves.  On Windows the old shared process and all descendants
also belong to a kill-on-close Job Object.  A listener spawned from shared
Functions therefore cannot survive the handoff, and the candidate cannot bind
the same public port before the old listener closes.

This is a topology fact, not a frontend presentation defect.  The stable
listener must be outside the shared process tree and be adopted once by a cold
platform lifecycle.  Protected Core does not need a new source file, import, or
protocol field.

## Stable boundary contract

The platform supervisor owns a small listener process outside the Core-managed
shared Job/process group.  It binds the configured public Workbench and API
Gateway ports.  Shared generations bind private loopback ports and register a
target containing instance ID, service, generation, PID, endpoint and a
monotonic target revision.  Registration and switch control are local-only and
authenticated by an instance-private capability.

Before broad quiesce, PAO publishes `switching` with the reboot operation ID.
The boundary continues to answer its own health and typed reboot-operation
reads.  It stops dispatching new mutations to the retiring target and holds a
bounded queue.  Read requests may remain attached until a verified target is
published; the boundary must not invent cached success.  After candidate
services and their identity probes pass, PAO atomically publishes the new
target.  Queued requests are sent exactly once.  On pre-commit failure PAO
restores the old target.  Post-send unknown outcomes are reported as unknown
and never replayed automatically.

The stable boundary is not Core and does not import product managers.  Its code
and public listener lifecycle are adopted only by `/restart`.  `/reboot max`
may change private business targets but never reloads the boundary module.

## Durable progress contract

The PAO reboot receipt is the only operation authority.  It uses one
`operation_id` and monotonic events:

```text
{sequence, phase, status, created_at, message_id?}
```

Admission persists sequence 1 before execution is woken.  For Workbench, the
start presentation is idempotently recorded in the canonical SessionStore and
its `message_id` is attached to a later progress event before any target route
is fenced.  If that stable write cannot be confirmed, the operation is rejected
without qualifying or touching a Worker.  Final delivery failure does not
rewrite the transaction outcome.

The stable API projection is read incrementally by operation ID and
`after_sequence`; a repeated read returns the same events and never reruns the
operation.  Workbench treats a target Agent's switching state as local, not as
instance offline.

## Adoption and evidence

Source implementation and offline tests do not prove live adoption.  The first
stable-boundary deployment requires an authorized cold lifecycle on HASHI2 and
HASHI3.  Acceptance then requires continuous socket/HTTP probes across min,
max, failed candidate and rollback, plus in-flight work and queued-mutation
evidence.  HASHI1/HASHI4 deployment remains forbidden until that evidence is
complete and explicitly accepted.
