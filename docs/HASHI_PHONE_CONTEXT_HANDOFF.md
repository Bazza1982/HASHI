# Phone external context handoff

PCM owns projection of Phone transcript context. PAO owns Session attribution,
action authorization cutoffs, admission and durable handoff/consumption facts.
This is shared Functions behavior for every Phone provider and text Engine;
the front-end intent judge is not a substitute for the acting Agent's context.
It supplements `HASHI_PCM_SYSTEM_DESIGN.md` and `HASHI_PAO_SYSTEM_DESIGN.md`.

## Freeze before action admission

Immediately before a Phone delegation invokes its actual Agent admission,
PAO freezes all confirmed durable user/assistant fragments up to the proposal's
authorization cutoff. Scope binds owner, instance/generation, Agent, HASHI
Session/context generation, call/epoch, delegation/version/digest and cutoff.
The snapshot preserves roles, source event IDs, temporal order, sequence and
exact text, without the intent judge's 60-fragment/eight-turn truncation.
Missing source events, crossing fragments, pending durable provider fragments,
scope mismatch or an oversized context reject admission with an explicit
`phone_context_*` code before tool work.

The immutable `phone_context_handoff_id` is stored with the Proposal and carried
through the trusted `live_voice` request metadata. Reads enforce the same
owner/Agent/Session/context boundary. Idempotent retry and recovery use the
same snapshot. Late fragments, including late speech with an earlier timestamp,
cannot alter a previously authorized snapshot. Post-cutoff instructions must
use the explicit action update/cancellation contract rather than silently
reinterpret the old Run.

## Materialize before the real Provider request

The shared runtime prepares the external Phone history on every persistent
Session turn, including a fixed native-thread resume (`incremental=true`).
Phone action Runs read only their frozen snapshot. Ordinary post-call text
reads ended calls' durable fragments after checking pending transcript inboxes.
The old lossy Phone rows are removed from that turn's generic recent-history
projection; the single display call record stays `history_eligible=false`.

Phone context is quoted history with explicit speaker/source and no new
authority. Its section is mandatory for budgeting but retains `history`
authority. Fixed native threads receive only unconsumed fragments; a new
Engine/native thread or a stateless/full request materializes the complete
relevant transcript. Consumption is per owner/Agent/Session/context and actual
Engine/native-session lane, using stable call/epoch/event identities.

For HERV3, Phone history section keys bind the stable source-event batch.
An incremental tail appends a new history resource rather than replacing the
previous batch. Omitting an already received batch does not revoke it.
The real fixed-session coordinator retains both batches in the next model
input. Same-batch retries remain idempotent.

Only a successful actual Provider result commits consumed fragment IDs.
Failure/cancellation does not advance them; restart reads the same durable
ledger. Background completion and capacity recovery use the same commit.
A checkpoint I/O failure is reported separately and does not convert an
already executed Provider result into an automatic action replay.

`provider_request.prompt_audit.phone_context_handoff` records snapshot/cutoff,
requested/included counts, safe consumer lane and exact source-event IDs.
This audit reflects the actual assembled Provider input, rather than merely
proving that SessionStore contains a transcript.

## Bounded failure behavior

Each full frozen snapshot or turn projection is limited to 65,536 UTF-8 bytes
and 10,000 fragments. The byte bound applies to the lossless serialized form.
`hashi.phone-fragments.grouped.v1` declares event columns once and groups only
adjacent fragments with the same call, epoch and source. Every fragment retains
its exact role, text, source event ID, sequence and time range. Neither speech
nor event boundaries are merged or omitted. Snapshot loading expands this form
for existing owners and also accepts previously persisted ungrouped snapshots.
This representation avoids repeating long call IDs and field labels for every
word received from a streaming provider. There is no silent tail truncation or
invented summary.
If complete necessary context cannot fit, `phone_context_budget_exceeded`
keeps the complete durable source and explicitly names its Session/handoff.
No retrieval tool has been added by this change. Long-context failures remain
conservative refusals; this is not evidence of successful full long-call
understanding by a model.

SessionStore schema 23 adds `phone_context_handoffs` and
`phone_context_consumption`. They are Session-owned data, distinct from the
Phone display record or a provider thread; Session purge includes these facts.
No Protected Core contract or provider permission is changed.

## HASHI1 verification, 2026-10-05

Two initial red tests reproduced the fixed-resume omission and missing
action snapshot. After repair, the Phone/runtime/Session direct-consumer set
passed 302 tests. Focused evidence covers two actions and post-call tail,
fixed versus stateless behavior, successful/failed Provider consumption,
durable restart, a new thread/Engine, scoped reads, frozen retry with late
fragments, pending transcription and budget refusal. Further real PCM and
HERV3 coordinator checks confirm history authority and prefix/tail persistence;
the checkpoint failure check confirms one Provider call and a successful result.

These are isolated product-boundary checks. They use no production call,
microphone, real user authorization or business action. Shared implementation
and Engine-contract tests do not establish live acceptance for every configured
Phone model/provider. The HASHI1 rollout owner records source/artifact/live
adoption separately; other instances are outside this scope.

## HASHI3 ingress and failure settlement, 2026-10-06

A physical microphone/cloud Phone call exposed a gap after snapshot creation:
SessionStore's trusted origin normalization retained the delegation markers but
dropped `phone_context_handoff_id`. The Worker therefore refused its prompt
before any Provider or tool call. Normalization now carries that ID only after
loading the immutable snapshot under the same owner/Agent/Session/context and
matching its call, epoch, delegation, version and digest. An unrelated snapshot
is rejected; later speech cannot change the admitted action's input.

The queue's generic exception path also left those rejected Runs visibly
running. It now settles them through the existing request notification owner,
which persists the terminal Session result before publishing activity or listener
results. It does not replay the action or overwrite an already terminal Run.

Two focused regressions failed before these repairs and passed afterward
(22 owning checks); 234 direct-consumer checks passed. The first qualification
run rejected uncommitted Function source as designed. Committed-source
qualification, Worker adoption and a fresh physical call are recorded in the
HASHI3 repair journal separately; the earlier call is evidence of the defect,
not successful Agent action execution. This change does not authorize replay
of either failed historical action.

The committed-source gate subsequently passed 793 tests with one skip, and all
eleven HASHI3 Workers adopted that generation without replacing Core. A fresh
physical LifeCam/OpenAI call admitted a single real Get-Date tool request,
returned its result through Phone, confirmed provider shutdown and carried the
remaining Phone history into a subsequent text reply. Separate native recording
acceptance survived a real page reload and confirmed the same original Run,
which completed without tools. Speaker-to-microphone stimulus was generated;
this does not establish human listening or mobile handset acceptance.

Switching that same QA Session to Codex exposed a further representation defect:
309 genuine word fragments contained 1,447 bytes of speech but the repeated
metadata occupied 67,052 bytes, exceeding the unchanged bound before Provider
work. The new lossless representation addresses both action snapshots and
post-call projections. Focused regressions cover full new-Engine input, exact
event provenance, immutable cutoff, unchanged durable source and old snapshot
compatibility. Actual new-Engine adoption is recorded separately; the failed
request is not automatically replayed.
