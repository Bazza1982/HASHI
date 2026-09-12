# Frontend command interactions v1

Status: locally integrated source candidate; live adoption not yet verified.
Owner: Frontend Connectors. Engineering layer: Functions (shared ingress and
per-Agent Worker). Parent: `HASHI_FRONTEND_CONNECTOR_ARCHITECTURE.md` and
`HASHI_COMMAND_UI_STYLE_GUIDE.md`. Existing Core, PCM/PAO/Engine ownership is unchanged.

## Decision

Reuse canonical `COMMAND_SPECS`, effective runtime command registration and
registered callbacks. Do not create a second model list, configuration writer,
command implementation, pairing scheme or role. Existing Telegram handlers
remain unchanged. A bounded adapter captures reply/edit/answer operations and
projects them as client-neutral JSON cards. No Telegram HTTP request is needed
for the supported synchronous menu interaction.

`command_interactions.py` owns only the disposable projection, its revision
fences and bounded request replay protection. `command_interaction_bridge.py`
adapts the existing command/callback owners. `command_interaction_api.py` admits
requests through the existing authenticated Backend API command path. The
existing private Worker JSON RPC gains an additive Function-side method; no Core
protocol version or protected source changes are required.

## Transport

The existing `/api/agents/{name}/command` JSON endpoint accepts an optional
`command_ui` envelope. Without it the existing command behavior is preserved.
Version 1 has `catalogue`, `open`, `act`, `close`. The authenticated external
server supplies a connection-binding fence; it is not an identity or permission.
The Backend API supplies the configured personal actor and the canonical
`workbench/default` Conversation Session and context generation. Catalogue reads
are authenticated but do not create a Conversation Session.

The API requires the existing nonempty local admin token. Authenticated Remote
already injects that token on its loopback hop. The token is not sent to the
browser. Governed profiles are deliberately rejected with 501: they must not be
impersonated as the personal configured owner. Ordinary anonymous/enterprise
users gain no rights through this feature.

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
the callback begins; an exception never re-enables a possibly executed button.

The store is in memory, bounded to 128 cards and 2,048 replay entries per runtime,
with a 15-minute lifetime. Repeated `(binding, request_id)` with the same payload
returns its cached result within that lifetime. A different payload using the
same request ID is rejected. Pending/failed outcomes are reserved before awaits.
This is **not durable exactly-once execution** across restarts or expiration.
Clients must not automatically retry an uncertain mutation. After a restart old
cards are unavailable and the user must inspect current state before reissuing
an operation.

`close` only invalidates this projection. It does not undo an operation, cancel a
Run or replace the existing command's own Cancel/Keep-current business action.
All projection state is disposable and is not a second sent-message archive.

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

## Source, qualification and live adoption

Approval covers this Functions/client implementation and local package delivery,
not live production commands. Source has not been pushed to GitHub by this kit.
Focused sandbox results are in the kit's evidence directory. Its
dependency-limited sandbox did not run the real `/notify` integration; the
local integration record below reports the full-checkout execution.

### Local integration record (2026-09-12)

The user authorized local follow-up development for Workbench menu support.
The candidate was assembled into `repair/nightly-batch-20260911` for the
registered `hashi1` source. This approval and work did not perform a live
shared replacement, Agent restart, or runtime adoption.

Package gates passed 19 backend-engine, 14 adapter, 15 assembly-safety, 27
frontend policy/proxy and 6 controller-boundary tests. In the assembled
checkout, the focused menu suite passed 34 tests and 11 subtests, the Worker
runtime group passed 127 tests, and the Core gate passed 667 tests. The
required offline product selection passed 4,577 tests with 165 deselected,
11 subtests passed and six unrelated failures; all six failures reproduced
unchanged on clean pre-integration HEAD `1d6f7b97` (6 failed, 11 passed).
Live adoption remains unverified.

Shared ingress/supervisor changes require the existing explicitly scoped shared
Functions replacement after tests. An Agent-only `/reboot min` cannot adopt the
shared API. See `HASHI_SLIM_CORE_ARCHITECTURE.md`; request acceptance and a running
generation's completion receipt are separate facts. No replacement command is
executed by the assembly tool. Core/instance state and platform configuration
remain untouched.
