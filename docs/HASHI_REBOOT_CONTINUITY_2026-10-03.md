# HASHI Reboot Presentation Decision

Status: approved current behavior; Workbench-origin max presentation barrier
and frontend integration implemented offline; instance adoption and live
acceptance remain pending.

Decision date: 2026-10-03

Owners: PAO (replacement transaction and the one reboot receipt authority) and
Frontend Connector (rendering and the authenticated presentation ACK). This
work stays in replaceable Functions. It does not change or add a protected Core
file and it does not introduce another listener or supervisor process.

## Current command boundary

`/reboot min` replaces only the selected Agent Worker through the existing
prepare, drain, commit and rollback transaction. It does not update or restart
the Backend API, API Gateway, shared intake, another Agent, Scheduler, or Core.
The Workbench connection therefore remains up. A min operation does not wait
for the Workbench presentation ACK defined below because it does not perform
the shared handoff that interrupts that API connection.

`/reboot max` keeps its existing broad shared-Functions handoff. The current
Backend API and Workbench connection may disconnect while that handoff occurs;
zero-outage API continuity is not a goal of this decision. Before HASHI
publishes the shared replacement request, a max initiated by Workbench must:

1. durably accept one owner- and Agent-scoped operation;
2. return its typed operation and start state to the initiating Workbench;
3. wait for that same browser to ACK that it actually rendered the start state;
4. persist the ACK in the same PAO reboot receipt; and only then
5. publish the shared replacement request that can disconnect Workbench.

A SessionStore write or an HTTP response accepted by the Workbench server is
not presentation proof. If the browser ACK is missing, late, incorrectly
scoped, or names the wrong progress sequence, the operation is rejected with
`start_presentation_unconfirmed`. HASHI must not silently continue the broad
handoff.

`/restart` remains the cold adoption path for protected Core. This decision
does not change Core lifecycle, process supervision, public ports, or Windows
Job ownership.

## Single receipt authority

The PAO reboot receipt remains the only operation authority. Schema 5 adds one
bounded `presentation_ack` member:

```text
{
  status: not_required | pending | confirmed | expired,
  expected_sequence: integer | null,
  sequence: integer | null,
  message_id: string | null,
  confirmed_at: timestamp | null
}
```

The operation projection exposes only the fields a client needs to render and
ACK: `required`, `status`, `expected_sequence`, and `sequence`. Receipt
progress remains monotonic. Browser confirmation appends `start_presented`
before `shared_replacement_requested`. Re-reading or repeating the same ACK is
idempotent and never repeats the reboot.

The ACK is fenced by all of:

- authenticated `owner_id` derived by the API, never accepted from the body;
- receipt `operation_id`;
- receipt `source_agent`; and
- the exact `expected_sequence` returned with the accepted operation.

The Backend API compatibility route is intentionally thin:

```text
POST /api/runtime/reboot/operations/{operation_id}/presentation-ack
body: {agent_id, sequence, message_id?}
```

It calls the PAO manager and does not create another status store. A scope
mismatch is indistinguishable from a missing operation. A stale sequence,
non-pending operation, or operation that does not require an ACK is a conflict,
not success.

## Frontend coverage boundary

This barrier proves only that the Workbench browser which initiated a max
operation rendered its start state. It must not be described as proof that
every open Workbench window or every Frontend Connector received the notice.

Telegram and other frontends retain their own origin delivery behavior. The
current SessionStore event-consumer cursor cannot prove that another browser
is live or that its DOM rendered an event: it has no connector/client lease or
presentation semantic. The frontend connector registry describes
capabilities, not live connections. Extending the guarantee to max operations
started from another frontend would require the existing Frontend Connector
delivery owner to define a live endpoint lease and rendered-ACK quorum. The
reboot manager must not invent a second connection registry.

## Evidence and adoption

Focused offline evidence must show:

- Workbench-origin max does not publish a kernel handoff request before the
  correctly scoped browser ACK;
- wrong owner, Agent, operation, or sequence cannot release the barrier;
- timeout expires the ACK, rejects the receipt, and leaves routes untouched;
- duplicate ACK is idempotent; and
- Workbench-origin min completes without an ACK and leaves API acceptance
  unchanged.

Source and offline tests do not prove live adoption. Live acceptance on the
authorized HASHI2/HASHI3 instances must observe the start state in Workbench
before the expected max disconnect, then verify reconnect and the terminal
receipt. No Core change, extra listener process, or zero-outage claim is part
of that acceptance.
