# HASHI Engine Runtime (HER) Backend Contract

Status: active for certified HER `0.1.0-hashi.22`; earlier unreleased integration
checkpoint recorded in
[HASHI_UNRELEASED_CHECKPOINT_2026-08-13.md](HASHI_UNRELEASED_CHECKPOINT_2026-08-13.md)

HER is derived from the MIT-licensed Claw runtime. The upstream copyright and
license notice ships with every packaged HER release as `CLAW_LICENSE`.

## Deployment scope

This contract and its packaged artifacts belong to standalone HASHI.
Certification is platform- and artifact-specific: a green Linux package does
not certify a Windows build, another architecture, or a downstream integration.

## Certified package identity

| Field | Certified value |
| --- | --- |
| Package version | `0.1.0-hashi.22` |
| HER source | `246b04e9fa28ef0b6f74c2d924ab3697b95197bd` |
| Certified tag | `her-0.1.0-hashi.22-certified-r2` |
| Upstream base | Claw `4ea31c1bc91c4e9bcbd67d51c550c01e127e6d0d` |
| Linux target / SHA-256 | `linux-x86_64` / `e6c88b9dd37c9191f9aad0df9fd0cf9bbeb4365778a10153a48b4cf752096c91` |
| Windows target / SHA-256 | `windows-x86_64` / `cd127b283d0bb8aa5db9d1863a617bb84a2c8cd0174ed305c72c5f97b294724d` |

The Linux package passed the full pinned-source certification suite. The Windows
package was built natively from the same clean source commit and passed native
`version`, `doctor`, `status`, target/provenance, and stdin-capability smokes.
Platform-specific live provider and rollout evidence remains separate.

## Ownership and session boundary

HASHI owns agent identity, memory injection, handoff context, authorization, request
identity, cancellation, and delivery. Claw owns the model/tool-loop session inside one
active backend lifecycle. HASHI records the Claw `session_id` and resumes it for the next
turn instead of rebuilding the browser plan from scratch.

The production `HERAdapter` (with `ClawCLIAdapter` retained as a compatibility
name) therefore:

- reports `supports_sessions = true`;
- captures `session_id` from `run_started` before tool execution and checkpoints it again
  from `run_finished`;
- passes `--resume <session_id>` on the next turn in the same backend lifecycle;
- permits only one writer at a time for that mutable persistent session;
- runs scheduler requests in isolated sessions with a complete HASHI-owned context
  snapshot, while adjacent direct user turns wait for the persistent-session lock and
  execute, commit, and deliver FIFO;
- binds a short direct reply to the latest eligible isolated result already delivered
  when that reply was enqueued; later scheduler output cannot capture it retroactively;
- quarantines a persistent session after a failed turn so the next request rebuilds
  from complete HASHI context instead of resuming possibly invalid tool state;
- clears the Claw identity on `/new` and when a new adapter instance is created;
- keeps Claw configuration and Tool Gateway state isolated per agent workspace.

The Bridge sends an incremental turn only in `fixed` mode after the adapter has a
session identity. Flex, Wrapper, Audit, and Dual Brain use HASHI-owned assembled
context. Those full-context modes must not also resume HER's previous internal
conversation.

Inside a fixed HER session, every task-control checkpoint receives the same effective,
normalized Claw session message list used by primary task execution. The current user
turn is appended before planning, while preserved prior user and assistant turns remain
available for references such as selecting an option from the immediately preceding
answer. This shared session-view contract applies to planning, replanning, self-review,
and direct-response finalization at every planning-enabled effort (`medium`, `high`,
`xhigh`, `max`, and `max+`). `low` deliberately has planning disabled. HASHI must not
duplicate the fixed-session history in the new prompt, and the runtime must not rely on
phrase matching to decide when context is needed.

After that exact persistent-session prefix, HER appends one request-local task-control
envelope and asks compatible providers for a JSON object. The envelope is deliberately
last so a long conversation cannot make the model continue ordinary chat, emit prose,
or execute a tool instead of returning the required TaskFrame. It is runtime control,
not user intent or new authority, and it is never appended to the persistent session.
Existing format validation and bounded retries remain fail-safe for providers that
occasionally return empty or invalid JSON.

TaskFrame validation remains strict, but TaskFrame is not an admission gate for the
task-performing Agent. If the initial planner exhausts its bounded attempts because of
a provider, response, schema, semantic-resolution, tool-name or assurance-field error,
HER records the rejected output, emits fallback telemetry, installs an
authorization-preserving fallback frame and returns control to the primary Agent. The
primary Agent continues from the authoritative request and canonical turn context under
the unchanged runtime permission policy. It must ask the user when that context cannot
safely resolve a material ambiguity. `planning_status=failed` and the bounded diagnostic
are exported in JSON, kept in cross-session receipts and made visible beside the primary
Agent's persona-authored result. A planning or review failure alone never proves that
task execution failed or succeeded.

Task planning is internal control state. The initial `TaskAcknowledgement` may be
presented once; later `TaskPlan` revisions remain technical telemetry. Only an explicit
`TaskCommentary` event may carry model-authored progress. Replans preserve the immutable
goal and authorization envelope, may not regress verified ledger evidence, use canonical
tool capabilities from the active registry, and stop after bounded review/no-change
budgets. Internal parser/reviewer failures are advisory technical events: they are
reported, but do not replace or suppress the primary Agent's answer. Only a real runtime
permission boundary, safety boundary or unresolved user decision may prevent the
corresponding action, and the primary Agent must then report progress and ask the user.

For every Flex turn, the complete canonical turn payload is the user message seen by
HER's task planner. The authoritative current request is repeated separately as the
active task boundary, but it never replaces the labeled Additional System Context,
Memory+ Continuity, Recent Context, or other accompanying evidence. Initial planning,
format retries, independent-review revisions, revision retries, and later replanning all
preserve that same payload. A planner may finalize a `direct_response` only after seeing
the complete payload. This is a mode-wide context contract; it does not depend on a list
of phrases or lexical triggers.

HER now implements the runtime's `set_session_mode()` hook. Disabling session
mode clears the in-memory identity and its persisted checkpoint, passes no
`--resume`, ignores any session returned by the full-context run, and does not
replace the fixed-mode checkpoint. This prevents a later switch back to fixed
mode from splicing pre-Flex HER history onto post-Flex conversation.

The ID captured at `run_started` is tentative until successful completion. A
cancelled, timed-out, or failed persistent turn clears the HASHI checkpoint;
the underlying session file remains available for diagnostics but is not
automatically resumed.
Runtime-owned Meditation explicitly sets `ephemeral_session`; every internal
health/helper call must do the same. Generic ephemeral HER backend construction
also forces that flag and disables both session tracking and Habit eligibility,
even if a caller or process-wide default attempts to enable them.

## Provider, model, and effort contract

HER provider profiles are instance configuration; secrets remain in HASHI's
normal credential chain. The canonical command flow is backend → provider →
model. `/provider` changes the provider and refreshes its allowlisted models;
`/model` stays within the current provider.

Request-scoped reviewer tools use the same configured provider/model route as the
primary run. Anthropic and the supported OpenAI-compatible families—including xAI,
DashScope, OpenRouter, and direct compatible endpoints—intersect the reviewer request's
allowlist with the process allowlist and preserve its additional virtual tool schemas.
No provider route may silently fall back to the primary agent's broader tool set.

HER effort controls agentic execution length, not provider reasoning depth:

| Effort | Maximum iterations | Planning and review capability ceiling |
| --- | ---: | --- |
| `low` | 12 | no planning; direct execution |
| `medium` | 32 | adaptive plan; no review loop |
| `high` | 96 | adaptive plan; optional self-review |
| `xhigh` | 192 | adaptive plan; optional independent read-only review |
| `max` | 384 | adaptive plan; optional independent read-only review |
| `max+` | 512 | same independent review plus optional isolated rerun of exact plan-declared tests |
| `ultra` | coordinated | HASHI-owned primary/worker orchestration above the single-Agent native ceiling |

`low` through `max+` are native single-Agent execution efforts. `ultra` is a
HER adapter effort: it uses a bounded primary plan and up to ten concurrent
isolated worker sessions, then assembles evidence through the primary. The
adapter assigns ordinary single-Agent efforts to those sessions; it never
passes `ultra` to the native executable as `CLAW_EXECUTION_EFFORT`.

An explicit `max_tool_iterations` remains an operator override but is bounded to
8–512. Effort is a capability ceiling, not a quota: the initial plan selects the
smallest task-matched execution, verification, testing, and review scope available
under that ceiling. A greeting at MAX+ therefore receives one planning decision and
one normal reply, with no independent review, tool use, or test. MAX+ has no private
token or wall-clock budget; `/timeout` remains the operator-owned outer control.

For a validated `direct_response` profile, the acknowledgement field is the complete
final answer and `remaining_work` must be empty. HER returns that answer once without a
second execution generation. Semantic compaction runs only when another model call is
required, so completed answers are never held behind a maintenance provider call.

Semantic compaction remains capacity-driven at the existing context threshold. Before
starting its provider call, HER derives an internal deadline from HASHI's effective
`/timeout` idle policy, caps it by the request's remaining hard timeout, and reserves a
small cleanup grace so timeout cancellation and the terminal event can complete before
the outer watchdog. This is not a second operator setting. If a model has already
selected a tool, HER dispatches that tool and records its result before considering
compaction for the next provider call. A validated compaction atomically replaces only
eligible historical active context; the current turn remains verbatim, and the complete
pre-compaction session is archived as recoverable raw history. Provider, timeout, and
schema failures leave active context unchanged and are attempted at most once per
request. With `/verbose on`, `started`, `completed`, and `failed` lifecycle events expose
the effective deadline/source, trigger phase, elapsed time, outcome, and continuation
state. With `/verbose off`, those events remain in local audit logs without adding
Telegram progress messages.

The adaptive task profile records task kind, risk, state-change scope, direct-response
eligibility, deliverables, material claims, verification mode, testing mode, exact test
commands where applicable, review mode/targets/triggers, and stop conditions. HIGH may
select self-review. XHIGH, MAX, and MAX+ may select an independent review, but none is
mandatory merely because the capability exists.

An independent reviewer starts without the task-performing conversation and receives a
separate read-only tool registry. It can inspect workspace files, glob/grep results, Git
status/diff/log/show/blame, and file identity/SHA-256 directly. Full immutable task tool
inputs and outputs remain addressable by stable evidence IDs; the compact review packet
is only an index, and truncation never makes older raw evidence inaccessible. MAX+ may
also expose a disposable, network-isolated `ReviewRun`, but only for an exact command
declared in the initial task profile. Reviewer `pass`, `revise`, and `block` verdicts are
advisory. They trigger primary-agent reconsideration within the planned scope but cannot
replace its judgment, suppress its final answer, or redefine completion. Provider-returned
encrypted or redacted reasoning is never reconstructed.

HIGH self-review and the shared mid-task replanning checkpoints use the same immutable
task evidence store through a bounded inline ledger of current-turn tool inputs and
results. The ledger is evidence for reflection, never new authorization. A checkpoint
may update its task frame and trigger further primary-agent thought or revision, but a
format failure or adverse finding remains fail-open and cannot suppress or replace the
primary agent's answer. Repeated divergence through the same canonical tool capability
triggers one immediate review; the configured periodic cadence then resumes.

## Tool Gateway contract

`ToolRegistry` remains the single capability catalog and execution core. API backends
consume it directly. Claw consumes the same registry through the `hashi-tools` MCP stdio
adapter generated under the agent's `backend_state` directory.

- Browser behavior remains in `tools.browser`; the HER adapter does not duplicate it.
- The generated Gateway context is mode `0600`, contains only secrets required by the
  allowed tools, and excludes live runtime/config objects.
- Claw-native `--allowedTools` and HASHI capability permissions remain separate layers.
- A required `hashi-tools` MCP entry is validated during backend initialization.
- HER pipes and continuously drains stdio MCP child stderr instead of inheriting it;
  child diagnostics cannot contaminate structured CLI output, and their raw content is
  not retained because it may contain server-owned secrets.
- MCP calls use existing JSON schemas, ToolRegistry permission checks, and tool audit
  records.
- HASHI scheduler operations use the explicit `hashi_scheduler_*` Gateway namespace;
  native Claw cron operations remain distinguishable as `claw_local_cron_*`.
- HASHI repository file operations use `hashi_file_*`; native `read_file`/`write_file`
  remain scoped to the Claw workspace and cannot stand in for HASHI authority.
- The gateway stops excessive total calls, repeated identical calls, and consecutive
  error loops with explicit errors instructing the model to report partial progress.

## Multimedia and media-read contract

HER `.22` can consume model-visible images returned by allowed MCP tools. The
Gateway accepts canonical MCP image content and the reviewed legacy screenshot
result shapes, then validates MIME type, decoded size, item count, and ordering
before forwarding provider content. MCP `isError` results remain failures even
if they contain otherwise parseable content.

`media_read` is the bounded, audited bridge for local media supplied to HER:

- images are normalized for model-visible inspection;
- PDFs prefer text extraction and can render bounded page images when visual
  evidence is required;
- audio uses the configured transcription path and reports an explicit failure
  when no safe route is available;
- video is probed and converted into a bounded deterministic frame set, with
  optional audio transcription;
- raw base64 is not written to normal session output; historical messages keep
  only private cache references and degrade safely after cache loss.

Every local path remains subject to workzone and ToolRegistry permission checks.
See [her_multimedia_multimodal_plan.md](her_multimedia_multimodal_plan.md) for
the detailed limits, compatibility matrix, and live rollout cases.

## Habit boundary

The default-off adapter-direct JSON path in `adapters/her_habits.py` is the
authoritative HER Habit/Meditation owner. It is surfaced through `/habit`, where
validated changes become active immediately. The adapter also honors the
runtime's per-request intake eligibility, so internal and maintenance requests
do not learn simply because the agent-wide switch is on.

Standalone HASHI contains no second Habit writer. The explicit
`HERAdapter.habit_pipeline_owner = "adapter"` marker lets downstream consumers
that still carry an older compatibility pipeline keep it dormant instead of
planning or meditating twice. See
[HER_HABIT_MEDITATION.md](HER_HABIT_MEDITATION.md).

## Streaming contract

The authenticated packaged HER may emit `stream-json`. HASHI consumes assistant,
thinking, tool, and usage events, but HASHI remains responsible for deciding which
events are visible and how final delivery is promoted. Encrypted or provider-redacted
reasoning must never be reconstructed or exposed.

Every mapped HER event carries a stable logical `event_id` and one explicit
presentation owner: `technical`, `user_commentary`, `reasoning`, `final`,
`control`, or `internal`. It also retains bounded origin, phase, revision,
required-delivery, and provenance metadata. Raw audit and bounded local activity
publication happen before display filtering. The HER message router directly
dispatches each event to the one owner selected by its delivery class. It keeps only a
request-local set of event IDs already accepted by the existing presenter: persistence
still records every occurrence, presenter failure remains retryable, and replay after a
successful presentation is suppressed. Transport reliability and final delivery remain
owned by the existing runtime sender and outbox:

- `/verbose` accepts only technical planning, tool, test, validation, retry,
  compaction, session, and runtime telemetry;
- `/commentary` accepts only explicit model-authored Persona acknowledgement or
  interim-report events;
- `/think` accepts only genuine provider-returned reasoning or explicit
  provider-redaction notices;
- final and required control events use mandatory lanes outside all three
  optional switches.

Persona commentary cadence and technical lease cadence are independent clocks.
Tool/runtime activity may postpone a neutral `/verbose` lease, but must never
delay a model-authored `/commentary` event. At most one material Persona update
may wait in the cadence slot: a newer update explicitly coalesces the older one.
Normal completion, failure, cancellation, or budget exhaustion must resolve
that slot without a terminal message burst. When the final answer makes the
pending update obsolete, HASHI records it as `superseded_by_final`; it must not
be silently dropped or presented as delivered.

Provider token deltas are not commentary by themselves. Once a complete primary
assistant turn contains both visible Persona text and at least one tool request,
native HER emits that assembled text exactly once as `assistant_commentary`
before the corresponding `ToolStart`. A tool-free terminal assistant turn stays
on the final lane, and an `AskUserQuestion` turn is finalized through the
required user-input lane rather than duplicated as commentary. Harmless
TaskFrame aliases that normalize to the same available tool capability are
deduplicated in first-seen order; non-tool prose and unavailable capabilities
remain invalid and planning continues under the last confirmed frame.

Effort controls event generation, never a second presentation gate. A direct
TaskFrame acknowledgement is buffered until the initial frame establishes
whether `direct_response=true`; direct answers are classified as `final` and
delivered once by the existing post-generation final lane, never first as
commentary. Deterministic TaskFrame summaries and neutral runtime leases are
technical telemetry rather than guessed Persona speech. The configured
`system_md` remains the only Persona source.

Provider reasoning deltas are an exact byte-fragment contract. HER must preserve
leading, trailing, and whitespace-only fragments from the provider stream; HASHI
concatenates those raw fragments without trimming or guessing token boundaries. This
prevents both joined words (`Theusersays`) and invented spaces inside words
(`prov id er`).

An OpenAI-compatible provider may return an error object inside an already
successful SSE connection. HER derives retryability from its status and timeout
metadata just as it does for an HTTP error response. One incomplete retryable
stream may be replayed under the original request deadline. The incomplete
assistant attempt is never committed to conversation history, and tools are not
executed until a complete attempt has returned, so the retry cannot duplicate a
tool side effect. `provider_retry` is technical `/verbose` telemetry; a second
failure remains a normal backend failure with its structured provider evidence.

Before any OpenAI-compatible provider request, HER validates the translated
assistant tool-call/tool-result sequence. A missing, duplicate, orphaned, or
interleaved result fails locally as `invalid_session_state`; HER never silently
reorders or deletes history to make an invalid request appear valid.

Every stream-json run that emitted `run_started` emits exactly one final
`run_finished`, including HTTP/auth and malformed/truncated provider-stream failures.
Protocol failures identify the last safe event and use a stable public message. Failed
`run_finished` events retain redacted `http_status`, `error_type`,
`provider_request_id`, semantic `error_message`, bounded `body_snippet`, and
`retryable` fields when available. HASHI correlates those fields and bounded
stderr with its own request ID in `backend_state/her_diagnostics.jsonl`.
Redaction targets configured credential values and bearer tokens; status,
provider trace IDs, error types, and diagnostic prose remain intact.

## Binary contract

Production resolution uses `runtime_policy = require-packaged`. The adapter verifies the
platform, executable permission, manifest identity, and SHA-256 before execution. The
certified binary and its provenance are recorded in:

- `hashi_assets/her/manifest.json`
- `hashi_assets/her/certification_baseline.json`
- `hashi_assets/her/releases/0.1.0-hashi.22/certification_evidence.json`
- `hashi_assets/her/releases/0.1.0-hashi.22/source/her-source.bundle`

A source checkout, PATH binary, or legacy external binary must not silently replace the
packaged runtime.

## Certification exceptions

The baseline is deliberately fail-closed:

- every Rust workspace test must pass;
- the full Rust workspace and complete HASHI `tests` + `veritas` suite must pass;
- Clippy runs workspace/all-targets with warnings denied and must match exactly the 40
  pinned inherited diagnostics; an addition, removal, movement, or lint change fails;
- Linux and Windows packages must match one manifest, clean source commit, branch,
  target, embedded provenance, and SHA-256 contract;
- the certified tag and complete-history source bundle must resolve to that source
  commit and match their pinned identity;
- immutable evidence must cover the Debug Lab, Gateway/scheduler, media bridge, and
  Windows native smoke matrix before promotion.

Run the full certification check with:

```bash
python scripts/verify_her_certification.py \
  --source-root /path/to/her-source-hashi-22
```

## Interrupted-turn continuation

Session identity and normal multi-turn resume are active. An operating-system kill may
still interrupt HER before its internal plan is fully flushed, so HASHI separately
persists the authoritative original user prompt when `/stop` is issued. A later bare
continuation request is rebound to that prompt while retaining the HER session,
workspace artefacts, and completed tool side effects. This makes task identity durable;
the exact internal model-plan position remains dependent on HER's last session flush.

## Rollout boundary

Passing package and unit certification does not load a running HASHI process.
Adoption requires an explicit canary `/reboot min`, live provider/media/Habit
smokes, log review, and only then the intended wider `/reboot max`.

The certified r2 package completed its Momo `/reboot min` canary, including direct
continuity, isolated scheduler execution, authoritative scheduler/file tools, typed
failure finalization, commentary idempotency, and an Ultra run with three required
workers and zero run errors. Wider Agent adoption remains an explicit operator rollout
action rather than an outstanding certification gate. `.20` remains the previous
known-good rollback release, and rejected `.21` remains inactive forensic evidence.
