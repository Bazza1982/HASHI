# Phone Session context and action handoff

Phone speech is ordinary Session conversation history. PCM owns its projection,
budgeting and compaction exactly as it owns typed chat history. PAO owns Session
attribution, action authorization cutoffs, admission and durable
handoff/consumption facts. This shared Functions behavior applies to every Phone
provider and text Engine; the front-end intent judge is not a substitute for the
acting Agent's context. It supplements `HASHI_PCM_SYSTEM_DESIGN.md` and
`HASHI_PAO_SYSTEM_DESIGN.md`.

## Freeze before action admission

Immediately before a Phone delegation invokes its actual Agent admission,
PAO freezes all confirmed durable user/assistant fragments up to the proposal's
authorization cutoff. Scope binds owner, instance/generation, Agent, HASHI
Session/context generation, call/epoch, delegation/version/digest and cutoff.
The snapshot preserves roles, source event IDs, temporal order, sequence and
exact text, without the intent judge's 60-fragment/eight-turn truncation.
Missing source events, crossing fragments, pending durable provider fragments,
or a scope mismatch reject admission with an explicit
`phone_context_*` code before tool work.

The immutable `phone_context_handoff_id` is stored with the Proposal and carried
through the trusted `live_voice` request metadata. Reads enforce the same
owner/Agent/Session/context boundary. Idempotent retry and recovery use the
same snapshot. Late fragments, including late speech with an earlier timestamp,
cannot alter a previously authorized snapshot. Post-cutoff instructions must
use the explicit action update/cancellation contract rather than silently
reinterpret the old Run.

## Materialize before the real Provider request

For a full or stateless request, ordinary Phone speech reaches the Agent only
through SessionStore's canonical recent-exchange timeline. It is rendered as
`USER` and `ASSISTANT` conversation, then budgeted and compacted by the same
PCM/HCC path as typed chat. No second Phone copy is injected.

A fixed native thread already owns the earlier PCM snapshot. On an incremental
turn, only unconsumed Phone speech is appended as ordinary, unprotected
`history` sections whose keys use the standard `recent_exchange:` namespace.
Each section is one stable dialogue round, so PCM can compact older rounds while
retaining the newest rounds from the same long call. The model-visible body
contains speaker text only. Provider event IDs, call IDs, epochs, timestamps and
fragment boundaries remain durable audit facts and are not placed in the prompt.
The SessionStore query excludes already consumed fragment identities before
loading transcript JSON; ordinary turns do not reparse the lifetime call history.
Phone dialogue rounds and typed exchanges share one absolute conversation
timestamp for presentation order. GPT-Live offsets are relative to each Provider
session; after recovery, the durable Provider-replacement event supplies that
epoch's time origin. If an old recovery has no such event, the fragment arrival
time is the conservative fallback. These times are approximate. Provider arrival
sequence is never compared with a typed Message ordinal. HERV3 interleaves Phone
PCM and completed native Turns in its final prompt using the canonical HASHI
Message time; old Turns without that time fall back to HER acceptance time.
Fixed incremental turns do not load unused Session recent history.

A Phone action is the sole exception. PAO loads its immutable cutoff snapshot
and PCM projects that snapshot as one protected `phone_action_context` history
section. Every action also checks for unconsumed ended calls and sends them as
ordinary history in the same request, independently of whether a representation
migration is due. The ordinary recent-history view is removed for the current
logical call across all epochs, preventing duplication. The frozen action includes
earlier epochs as context but only the current epoch's cutoff-bound user speech
can authorize the new action. Successful Provider completion checkpoints the
ended history; failure leaves it pending. The action section carries a one-way
digest of its owning call scope. Ordinary turns preserve it through Provider
recovery while that logical call remains active. Once it is terminal, PCM first
projects the complete call as ordinary history and then sends the action tombstone.

Typed fixed PCM also performs a one-time representation migration. It reads
the exact current sections and validates each deprecated
`phone_external_context:*` identity, scope and fragment against durable
SessionStore or the immutable current action snapshot before issuing a
tombstone. Both the original `fragments` payload and the grouped durable
`fragment_groups` encoding are expanded through the same decoder before those
checks. Existing semantic Phone sections must likewise match a chronological
durable source snapshot by stable event-identity digest and rendered text.
Provider arrival sequence is used only to reconstruct the snapshot's first
arrival through a candidate high-water mark; it is never compared with chat
ordinals or used as transcript chronology. This deterministic reconstruction
spans multiple calls and remains unambiguous when fragments repeat identical
text. Missing, malformed or incomplete source fails closed and preserves the old
section. Verified source is rebuilt as ordinary semantic
`recent_exchange:phone:*` history. If the first upgraded turn is a Phone
action, earlier ended calls are rebuilt in that same request beside the
protected action snapshot, so cleanup cannot create a history gap. Existing
semantic Phone sections are replaced rather than copied when their new
consumption ledger has not yet committed.

Only a successful actual Provider result commits consumed fragment IDs.
Failure/cancellation does not advance them; restart reads the same durable
ledger. Background completion and capacity recovery use the same commit.
A checkpoint I/O failure is reported separately and does not convert an
already executed Provider result into an automatic action replay.
For typed fixed PCM, the protected action snapshot itself never advances the
ordinary-history watermark. After hang-up, the complete call enters ordinary
semantic history before `phone_action_context` is removed, including when no
speech arrived after the action. The next non-Phone-action turn tombstones that
temporary section even when it has no tail to append. Any ended-call history
migrated beside an action keeps its own normal success checkpoint.

`provider_request.prompt_audit.phone_context_handoff` records snapshot/cutoff,
requested/included counts, safe consumer lane and exact source-event IDs.
These content-free audit identities prove the source without exposing transport
metadata to the Agent.

## Unified context budgeting

Phone has no separate byte or fragment limit. Ordinary speech uses PCM's normal
prompt budget and HCC/history compaction. Long Phone history can therefore
produce the same deterministic continuity capsule as long typed-chat history;
it does not fail with `phone_context_budget_exceeded`.

The immutable action snapshot remains lossless in SessionStore.
`hashi.phone-fragments.grouped.v1` is retained as a durable storage encoding:
it declares event columns once and groups adjacent fragments with the same call,
epoch and source. Snapshot loading expands this form and also accepts older
ungrouped snapshots. This encoding is audit storage only and is never the
model-visible conversation format.

## Logical-call recovery

Terminal Phone phases are irreversible. Schema 29 does not reopen old
`ended`, `failed` or `interrupted` rows. It repairs the earlier schema 27
mistake only when a currently recovering row has the matching
`legacy_terminal_recovery` event for the same call epoch; a later genuine
resume epoch is left untouched.

A replacement transport in `connecting` is governed by its fresh controller
lease. The previous provider's `max_ends_at` applies only while `active`, so
it cannot immediately push a new connection back to `recovering`. Workbench
may retry recovery with exponential delay, but PAO permits at most five
consecutive replacement attempts since the last verified `active` state.
Exhaustion settles the logical call as `failed` and publishes the terminal
state instead of leaving an endless recovery loop.

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
309 genuine word fragments contained 1,447 bytes of speech but repeated metadata
occupied 67,052 bytes, exceeding the then-current Phone-specific bound before
Provider work. Grouped snapshot storage reduced that overhead but retained the
wrong separate limit and still exposed transport structure to the Agent. The
2026-10-27 unified-context decision above supersedes that interim design:
durable action evidence remains lossless, while model input uses semantic
Session history and PCM's existing context controls. Historical failed requests
are never automatically replayed.

## HASHI3 real-call legacy migration correction, 2026-10-27

A real Agent 1 Phone action completed successfully and the Provider confirmed
normal user hang-up. Its new `phone_action_context` contained clean semantic
speaker text, but the resumed HERV3 PCM still carried six deprecated raw Phone
sections. Those sections occupied 93,132 serialized bytes and made the actual
Provider prompt 137,226 characters. The successful action had also advanced the
old ordinary-history consumption lane, which would hide the call after the
temporary action section was removed. This was a partial migration defect, not
a Phone transport or Provider failure.

The correction gives typed fixed PCM a versioned semantic consumer, enumerates
and tombstones exact legacy section keys, preserves earlier ended calls during
an in-call migration, and keeps the current action snapshot out of the ordinary
history watermark. A read-only copy of the real Session/HERV3 state migrated
all six legacy sections into one 3,315-character semantic history section,
removed the temporary action section, and retained no legacy call values or
Phone transport metadata in that semantic projection. Focused Phone and live
voice regression passed 87 tests. This paragraph records defect evidence and
offline qualification; committed source, Worker adoption and a later live turn
remain separate facts.

## HASHI3 cross-call continuity review correction, 2026-10-27

PR review found that a second Phone action could omit an unconsumed earlier
ended call, and that one long call was represented as one PCM history section.
It also identified unsafe legacy cleanup when exact source was unavailable and
repeated parsing of already consumed fragments. Four focused red tests
reproduced those boundaries before implementation.

The correction makes every fixed-session Phone action carry pending ended-call
history, divides calls into stable dialogue-round history units, validates old
Phone sections before removal, and excludes consumed identities in SQL before
transcript JSON is loaded. After implementation, 30 focused Phone/FYI checks and
475 related Phone, HERV3, PCM and runtime checks passed. Protected-Core, syntax
and patch checks also passed. This is offline source qualification only;
committed-source qualification, GitHub review, Worker adoption and live Phone
acceptance remain separate facts.

## PR 110 final migration and first-open correction, 2026-10-27

Final review found three remaining migration boundaries: grouped legacy payloads
were rejected before exact-source validation, a successful first HERV3 full-open
Phone action could consume its temporary snapshot, and semantic-v2 validation
treated arrival sequence as chronology. The correction reuses the durable
grouped decoder, keeps action snapshots outside ordinary consumption on every
transport mode, removes their fixed-PCM section after the owning call becomes
terminal, and matches old semantic source by stable identities, digest and text
while allowing late unrelated fragments. Every invalid or incomplete source
still fails closed without producing a removal plan.

## PR 110 active-context and chronology correction, 2026-10-27

Independent follow-up found that an ordinary action during the same active call
could tombstone its protected Phone snapshot, that semantic-v2 migration could
reject valid multi-call or repeated-text source after bounded backtracking, and
that HERV3 transport compared Provider arrival sequence with typed Message
ordinal. The correction binds each action snapshot to a one-way call-scope
digest, retains it until terminal settlement, reconstructs legacy membership by
arrival high-water while preserving chronological source order, and carries one
absolute conversation timestamp into fixed PCM ordering. Focused tests assert
the final HERV3 materialized prompt, not only intermediate section creation.


## PR 110 recovery follow-up, 2026-10-28

The requested branch-only fix preserves a logical call through Provider
replacement, keeps authorization scoped to the new epoch, and orders native HER
Turns and Phone history by their conversation times. The fixed incremental path
does not load unused lifetime Phone history. The owning decision is implemented
in Functions on the existing PR branch.

Offline checks use durable test stores, a simulated Provider replacement, real
fixed-HER materialization, and native Windows regression suites. GitHub CI,
independent review, Worker adoption, and live Provider verification remain
separate facts; no live adoption is claimed here.
