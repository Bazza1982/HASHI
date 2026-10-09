# Asynchronous questions within a Run

## October 10: complete Engine and frontend round trip

Approved scope: implement in HASHI3 and Workbench, then deploy to HASHI4.
HASHI3 adoption is authorized; HASHI4 reboot and real Telegram button acceptance
are reserved to the user. Approval, source delivery and live adoption are separate.

Canonical active Codex Runs use the bidirectional app-server protocol. Native
`item/tool/requestUserInput` batches become PAO questions; explicit answers are
returned to the same native RPC, original question IDs and original Turn. A
cancelled/expired question is an error, never an empty or default answer. Native
secret-input requests are rejected. Provider-only callers retain exec transport.
The request-scoped HASHI MCP inventory is retained. App-server lacks exec's
invocation-scoped hook trust, so its native shell and hooks are disabled: execution
and process control remain with the managed HASHI Gateway. Permission RPCs are
not clarification questions and are not granted by this bridge.

HERV3's main prompt requires the existing ask_user/get_user_answer contract when
clarification is needed, independent work while waiting, and no premature final
answer while required information is pending.

Creation atomically publishes the question event into the existing FC outbox.
The Agent delivery sweep derives pending Telegram work from that outbox. Telegram
renders option buttons and a localized free-text action. Clicking an option, or
replying to that bot's question message, enters PAO after actor, Agent, instance
and frozen Run destination checks. It never enters the ordinary new-Run queue.
Duplicate deliveries use existing FC claims; duplicate answers preserve one
durable result. Closed cards retain their question identity for late text replies.
The text action asks the user to reply to the question message; it is not a new
conversation prompt. Telegram client interaction remains a separate acceptance.

Workbench displays pending cards above the composer, outside the scrollable
transcript, with their own bounded scroll area. Session, context generation and
connection scope fence both rendering and submission. No option is auto-selected.

Evidence: focused PAO/HTTP/Telegram transport/native RPC tests cover scope,
expiry, cancellation, idempotency, Unicode and continuation. A real installed
Codex model received a question answer and completed the same native Turn.
Full deployment/adoption receipts are recorded separately in the dated repair.

Owner: PAO owns question identity, durable state and scope. Frontend Connectors
project question cards and collect authenticated answers. Engineering layer:
Functions. Parent: `HASHI_PAO_SYSTEM_DESIGN.md`. This changes no Protected Core.

`ask_user` creates a bounded clarification/preference question and immediately
returns its question_id. The Engine may continue independent work, then call
`get_user_answer` in the same original active Run. This is one PAO contract shared
by HERV3 and the Codex Tool Gateway. An answer does not create another Run,
resubmit the task, change tool permissions or authorize side effects. Unanswered
questions, default options, expired questions and silence never mean approval.

The question is bound to the durable instance/owner/Agent/Session/Run/request
and context generation. Agent identity comes from the canonical Run, not the
token's spelling. Question creation and authenticated answer submissions use
explicit idempotency keys: same key/same content replays one durable result;
same key/different content is a conflict. Different questions can be answered
out of order. No cross-Run answer lookup or cross-owner/session answer is allowed.

States are `pending`, `answered`, `consumed`, `expired` and `cancelled`.
Pending questions can be answered before their deadline. The matching Run tool
consumes an answered question without starting another task. Run termination,
inactive Session or a changed context generation invalidates outstanding
questions; expiry is explicit and never an inferred answer. Late writes reject
accurately. At most eight pending/answered questions exist per Run; expired
entries are refreshed before enforcing that limit. Lists are bounded at 50.

Every state read that can refresh or mutate a question begins a SQLite
`BEGIN IMMEDIATE` transaction before reading Run or Session scope. All scope
reads and writes use that same connection. This fences independent Agent Worker
connections, preventing cancellation/context changes from committing between
validation and an answer or question write. A rejected late answer can still
commit the legitimate cancellation/expiry refresh; it never persists the answer.
Unexpected persistence errors roll back. Events are written in the same
transaction and concurrent idempotent submissions produce one creation/answer.

The private tool endpoint accepts only a signed instance/Agent/request token.
The Codex Gateway carries that token rather than the administrator secret.
Public question list/answer routes require an authenticated owner; the scoped
tool token cannot act as an admin token or answer on the user's behalf. Token
lifetimes are one hour. HERV3 can issue a fresh token per call; an already
serialized Codex Gateway token expires after that hour and no automatic renewal
contract is claimed here. Questions have an explicit 30–3600 second deadline;
answer polling waits at most 30 seconds per tool call. Cancellation propagates
normally through the running tool. Reserved operation fields cannot override
the Engine tool's mechanical create/get operation.

## 2026-10-05 HASHI1 evidence

An independent real SQLite connection reproduced a pre-fix cancellation
committing before the answer write while that answer still reported saved.
The repaired boundary fences that cancellation until the answer commit; a
cancellation that wins first rejects the answer and durably refreshes state.
A malformed HERV3 tool payload previously overrode ask_user with get; it now
fails with question_reserved_field. A mixed-case Agent previously produced a
noncanonical row and unusable scope; its row now comes from the actual Run.

Focused QnA owner/review scope: 10 tests passed. It includes real isolated HTTP
for HERV3/Codex, continued independent file_list work, owner authentication,
cross-Run/admin-token rejection, expiry, durable out-of-order answers, real
independent SQLite connection fencing, concurrent single-effect idempotency
and canonical Agent scope. These are isolated evidence, not live model or
external client screenshots. Actual HASHI1 adoption and client/model acceptance
are pending the task coordinator's authorized canary; no other instance is used.
