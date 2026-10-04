# Frontend command interactions v1

Status: HASHI3 persistence correction implemented and offline-qualified; live
adoption of this correction is not yet verified.
Owner: Frontend Connectors. Engineering layer: Functions (per-Agent Worker;
the separate Workbench client remains a Frontend Connector). Parent: `HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md` and
`HASHI_COMMAND_UI_STYLE_GUIDE.md`. Existing Core, PCM/PAO/Engine ownership is unchanged.

## Decision

Reuse canonical `COMMAND_SPECS`, effective runtime command registration and
registered callbacks. Do not create a second model list, configuration writer,
command implementation, pairing scheme or role. Existing Telegram handlers
remain unchanged. A bounded adapter captures reply/edit/answer operations and
projects them as client-neutral JSON cards. No Telegram HTTP request is needed
for the supported synchronous menu interaction.

`command_interactions.py` owns the bounded live action state, revision fences
and request replay protection. `command_interaction_bridge.py` adapts the
existing command/callback owners. Presentation-only menu state is recorded in
the canonical Conversation Session and exposed through capability-negotiated
chat projection v2. It never enters model history. This makes a full transcript
refresh derive the same menu card as the direct command response instead of
depending on client retention heuristics. `command_interaction_transport.py`
remains a Worker-owned adapter over the existing `runtime.slash` RPC. The shared
Backend API, Supervisor, Core protocol and protected source paths are unchanged.

## Transport

The existing authenticated `/api/admin/command` JSON endpoint continues to
accept an Agent name and one nonempty `command` string. The Workbench server
validates browser input and serializes a bounded versioned operation into a
reserved opaque command string. The unchanged API and Supervisor route that
string through the existing `runtime.slash` RPC. Only the current Agent Worker
recognizes it.

Version 1 has `catalogue`, `open`, `act`, `close`. The Workbench server supplies
a connection-binding fence; it is not an identity or permission. The Worker
derives the configured personal actor and the canonical `workbench/default`
Conversation Session and context generation. Catalogue reads do not create a
Conversation Session.

Chat projection v2 is a separate, capability-negotiated read contract. A caller
must request exactly `capabilities.command_ui=true`; v1 reads remain unchanged
and never receive menu state. V2 returns allowlisted `command_ui` state using
the same `command-ui:<menu_id>` identity as the direct response. Initial cards
and later revisions are Session-backed presentation rows, so snapshots and
polls are authoritative even when a client event or full refresh races the
direct response.

The Workbench-to-instance hop uses the existing authenticated Remote connection;
the token is not sent to the browser. The reserved transport is accepted only
from the existing `workbench_api` command source, and the Worker revalidates its
allowlisted fields. Governed profiles are deliberately rejected with 501: they
must not be impersonated as the personal configured owner. Ordinary
anonymous/enterprise users gain no rights through this feature.

A response card contains a `message_ref`, `op` (`upsert`/`delete`), text,
`meta.parse_mode`, and `command_ui`:

```json
{
  "version": 1,
  "menu_id": "opaque_server_generated_id",
  "revision": 1,
  "expires_at": 1800000000000,
  "closed": false,
  "rows": [[{"text": "Next", "button_id": "opaque_issued_action_id", "disabled": false}]]
}
```

The example IDs are placeholders, not usable credentials. The browser echoes
only the issued menu/button IDs and revision. Raw `callback_data` stays in the
Worker. The dispatcher resolves an issued action against the **current**
registered handler and effective command permission. It does not parse labels
into commands or send callbacks to a model.

## Failure boundaries

Cards are bound to instance, Agent, canonical actor, Session, context generation,
client and connection. A stale revision, expired card, changed callback
registration or incompatible binding fails closed. A revision is consumed before
the callback begins; the card remains visible with its actions disabled while
the callback runs, and an exception never re-enables a possibly executed button.

Live action state is in memory, bounded to 128 cards and 2,048 replay entries per
runtime, with a 15-minute lifetime. Opening another menu does not invalidate an
earlier unexpired menu. Repeated `(binding, request_id)` with the same payload
returns its cached result within that lifetime. A different payload using the
same request ID is rejected. Pending/failed outcomes are reserved before awaits.
This is **not durable exactly-once execution** across restarts or expiration.
Clients must not automatically retry an uncertain mutation. After a restart old
cards are unavailable and the user must inspect current state before reissuing
an operation.

`close` only invalidates this projection. It does not undo an operation, cancel a
Run or replace the existing command's own Cancel/Keep-current business action.
The Session-backed row is presentation-only and mutable by stable identity; it
is not a second semantic message or model-history archive. Before TTL, ordinary
polling, a full snapshot, an unrelated click reaching the transcript layer, or
opening another menu cannot erase the card because the backend projects it
again from authoritative state.

## Supported compatibility surface

Supported: `InlineKeyboardMarkup.to_dict()`/JSON inline rows; callback buttons;
safe HTTP(S) URL buttons; reply text; message/query edit text and markup; delete;
query answer/alert text; multiple synchronous replies. Existing active-choice,
Back/Refresh and confirmation labels are retained.

Unsupported Telegram specialties (Web Apps, login, payments, games, inline-mode
switching and similar) render as unavailable rather than being reinterpreted.
A handler that directly calls a Telegram network Bot, relies on unimplemented
`context.bot`/`user_data` interfaces, edits external Telegram messages, or defers
an edit past the request scope needs a separately reviewed adapter. Synthetic
message IDs are negative so they cannot identify a real Telegram message.
Transport compatibility does not prove that every existing command has been
qualified on this surface. Start with the supplied real `/notify` integration
test and a read-only navigation canary.

`/telegram` and `/whatsapp` control owner-scoped FC mirror preferences from any
authenticated frontend. Their handlers and callbacks use the same persisted
connector-delivery state and localized card contract. The `api_chat` text route
remains a compatibility entry point. Origin-platform replies continue to their
original endpoints; legacy TUI per-Run delivery values cannot override the
central switch.

## Source, qualification and live adoption

Approval covers this Functions/client implementation and local package delivery,
not live production commands. Source has not been pushed to GitHub by this kit.
Focused sandbox results are in the kit's evidence directory. Its
dependency-limited sandbox did not run the real `/notify` integration; the
local integration record below reports the full-checkout execution.

### Local integration record (2026-09-12)

The user authorized local follow-up development for Workbench menu support.
The candidate was assembled into `repair/nightly-batch-20260911` for the
registered `hashi1` source. This approval and work did not perform a live Agent
reboot or runtime adoption.

Package gates passed 19 backend-engine, 14 adapter, 15 assembly-safety, 27
frontend policy/proxy and 6 controller-boundary tests. The initial assembled
candidate's required offline product selection passed 4,577 tests with 165
deselected and 11 subtests passed; its six unrelated failures reproduced
unchanged on clean pre-integration HEAD `1d6f7b97` (6 failed, 11 passed).

Architecture correction: the first local candidate added a menu-specific shared
API branch and Supervisor RPC. Although neither file is a protected Core source,
that design made adoption depend on a shared process replacement and violated
the Function `/reboot min` boundary. Those additions were removed. The final
backend delta is confined to the per-Agent Function generation and tests/docs;
an Agent-only `/reboot` is the required adoption path. Request acceptance and a
running generation's completion receipt remain separate facts. Core/instance
state and platform configuration remain untouched.

The corrected tree was verified with HASHI1's exact CPython 3.12.13 runtime and
standard dependency lock. Menu plus direct lifecycle consumers passed 144 tests
and 11 subtests; the deterministic Core gate passed 667 tests. The protected
Core guard passed both the working tree and the full feature range from
`1d6f7b97`. An isolated real Worker reached `READY` as generation
`sha256:924d5d2b25fb3fd6303bbb6d30cec5d4ac29602e888ca30350c8fb7979594e2f`
and was immediately closed without activation, traffic, Telegram polling or a
model call.

The Workbench correction has a red/green regression proving that menu traffic
uses the existing authenticated admin-command path rather than the ordinary
Agent command path. Its server suite passed 373 tests, UI policy passed 255
tests, and the production build completed. The currently active HASHI1 Worker
generation remains the earlier `sha256:d67ff450468ccf8b63f6abb1ec59179e3d772029c964b3c4458a38230c39f127`;
an authenticated catalogue probe therefore returns the expected 501 upgrade
boundary. End-to-end live adoption still requires the operator's next targeted
Agent `/reboot`; no restart or shared replacement is required or permitted for
this change.

### HASHI3 persistence correction (2026-09-20)

The user reported that `/sys` and `/reboot` cards on HASHI3 appeared and then
vanished while the same Workbench frontend remained stable on HASHI4. A live,
read-only reproduction showed that HASHI3 returned a complete menu immediately
but stored only its text. The next v1 transcript snapshot therefore lacked
`command_ui` and could replace the direct response. Source and running menu-file
hashes matched HASHI4, ruling out a stale frontend or missing source file; the
regression was the incomplete HASHI3 projection contract.

The correction gives every captured menu a stable presentation identity,
records its allowlisted state with the presentation row, persists later menu
revisions, and implements the Workbench's already-negotiated chat projection
v2 contract. V1 remains compatible. A second menu no longer prematurely
invalidates the first. Four focused regressions failed before implementation
and passed afterward. The final command interaction, projection, SessionStore
and Connector selection passed 140 tests and 11 subtests.

Approval, implementation and adoption remain separate: the current user request
authorized diagnosis and source correction, not a HASHI3 `/reboot` or
`/restart`. No live process was changed during this correction.

### HASHI1 durable cross-frontend command results (2026-09-25)

Workbench command-menu operations now opt into the same typed command
invocation envelope used by the Session command endpoint. The Worker reserves
the `(Session, client, request)` identity before the handler runs and atomically
stores its result with a `frontend.command_result` Session event. A pending or
unknown reservation is not executed again. A completed retry returns the saved
result and event identity, but drops volatile button state and requests a menu
refresh because Worker-local callback state may have been lost. Telegram menu
callbacks and TUI command paths remain covered by the shared dispatcher and
their connector-specific admission fences.

Focused HASHI1 offline regression passed 178 tests and four subtests across
command contracts, Workbench worker transport, Telegram menu callbacks, Session
storage/API, TUI client, and Remote TUI proxy. This verifies source behavior,
not live Worker adoption; no instance was restarted.

The same durable reservation/complete primitive now fences native Telegram
slash commands and callbacks. Each is bound to its stable transport identity,
connector endpoint, Session generation and request digest before the handler
runs. Completed redelivery is ignored; conflicting reuse and pending/unknown
outcomes never re-execute. The Session result event explicitly reports that
Telegram transport delivery was not observed, so it is not a delivery receipt.
Workbench callbacks retain their Workbench fence and do not pass through this
Telegram-native wrapper.


## Command discovery and derived Runs (2026-10-05)

CommandSpec `menu_visible` controls the compact Telegram bot menu.
`picker_visible` is an optional independent discovery choice for authenticated
client command pickers; when absent it follows menu_visible. `/move` and
`/clone` explicitly opt into picker discovery with handler-aligned usage while
remaining absent from Telegram's compact menu. Runtime support, dynamic
registration overrides and command authorization still decide availability;
visibility never grants execution. Retired aliases, terminal-only logo and
the existing hidden token/wipe/reset commands are not exposed by this change.
The client selection inserts text and does not submit a migration or clone.

`command_request_context` owns trusted PAO command-to-Run handoff. An authenticated
local adapter supplies its verified Connector identity; native Telegram supplies
its transport Update, never a guessed chat-ID identity. Derived commands resolve
the same actual owner/Agent/Session, fence its context generation, preserve the
original channel and carry explicit ingress transport into queue admission.
The queue independently freezes its delivery route from this trusted metadata.
`wiki:query` remains a business source label and is not an internal bypass.
Wiki adds only its retrieval tool allowlist and provider identity. Stable command
invocation/transport IDs scope the derived Run idempotency key to Session and
context generation, preserving command replay without duplicate queueing.

The local command executor and command bridge preserve classified failure codes
and `request_outcome` (`not_admitted`, `accepted`, `unknown`) instead of collapsing
every failure into command_menu_command_failed. Accepted Wiki replies include
derived_request_id. Unknown enqueue outcomes cannot justify automatic retry.
Missing/untrusted Connector or stale Session is rejected before enqueue; clients
retain the draft and explain the actual submission state using their locale.

Red/green evidence used the real local command executor, Frontend Connector
admission and SessionStore for Workbench-compatible API, Telegram and TUI routes.
Picker support/catalogue tests proved move/clone were supported but missing,
then discovered according to policy without widening Telegram's menu. Source
and offline qualification do not assert a client screenshot or running adoption.
