# HASHI Engine Runtime (HER) Backend Contract

Status: active for HER `0.1.0-hashi.10`; unreleased integration checkpoint
recorded in
[HASHI_UNRELEASED_CHECKPOINT_2026-08-13.md](HASHI_UNRELEASED_CHECKPOINT_2026-08-13.md)

HER is derived from the MIT-licensed Claw runtime. The upstream copyright and
license notice ships with every packaged HER release as `CLAW_LICENSE`.

## Deployment scope

This contract and packaged Linux binary belong to the standalone HASHI runtime.
Certification and deployment are performed independently for each HASHI instance. They
do not update Aptenra's embedded HASHI runtime, Windows `aptenra_hashi.exe`, debug
candidate, or installation package.

Aptenra adoption is a separate release task: explicitly integrate the selected HASHI
changes, build the Windows artifact, record its provenance and SHA-256, and run the
Aptenra product certification suite. No change in this document or package propagates
to Aptenra automatically.

## Certified package identity

| Field | Certified Linux value |
| --- | --- |
| Package version | `0.1.0-hashi.10` |
| HER source | `85a481d9e5c94804ed9c0bd300ca9a635732c22d` |
| Upstream base | Claw `4ea31c1bc91c4e9bcbd67d51c550c01e127e6d0d` |
| Target | `linux-x86_64` |
| SHA-256 | `882c9a71013bdd6155558ff4dc8df4a8e002188e144b04f7fda2fb96f0f83ac2` |

The same manifest also contains a Windows `0.1.3-hashi.3` artifact built from
`b27f4180`. It is retained for the reviewed Aptenra/Windows path but is not the
`.10` runtime and must not be used as evidence of current cross-platform parity.

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
- clears the Claw identity on `/new` and when a new adapter instance is created;
- keeps Claw configuration and Tool Gateway state isolated per agent workspace.

The Bridge sends an incremental turn only in `fixed` mode after the adapter has a
session identity. Flex, Wrapper, Audit, and Dual Brain use HASHI-owned assembled
context. Those full-context modes must not also resume HER's previous internal
conversation.

That last invariant is not yet fully enforced: HER advertises sessions and
persists its identity, but it does not currently implement the runtime's
`set_session_mode()` hook. A non-ephemeral HER adapter can therefore pass
`--resume` even when the Bridge has assembled a full-context Flex/composed turn.
Until an explicit mode policy and regression coverage land, this is a known
publication and rollout gate rather than a supported continuity guarantee.

Capturing the ID at `run_started` allows a cancelled fixed-mode turn to retain
its intended session identity. Resume after a hard process kill remains
best-effort if HER did not persist the session file before exit; that case is
logged and must not silently switch to a different agent's session.
Runtime-owned Meditation explicitly sets `ephemeral_session`; every internal
health/helper call must do the same. Generic ephemeral HER backend construction
does not yet force that flag, so helper isolation is included in the open
session-policy gate.

## Provider, model, and effort contract

HER provider profiles are instance configuration; secrets remain in HASHI's
normal credential chain. The canonical command flow is backend → provider →
model. `/provider` changes the provider and refreshes its allowlisted models;
`/model` stays within the current provider.

HER effort controls agentic execution length, not provider reasoning depth:

| Effort | Maximum iterations | Planning | Extra time rule |
| --- | ---: | --- | --- |
| `low` | 12 | off | normal timeout |
| `medium` | 32 | on | normal timeout |
| `high` | 96 | on | normal timeout |
| `xhigh` | 192 | on | normal timeout |
| `max` | 384 | on | normal timeout |
| `max+` | 512 | on | 1,500-second max-plus budget |

An explicit `max_tool_iterations` remains an operator override but is bounded to
8–512. Provider-returned encrypted or redacted reasoning is never reconstructed.

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
- The gateway stops excessive total calls, repeated identical calls, and consecutive
  error loops with explicit errors instructing the model to report partial progress.

## Multimedia and media-read contract

HER `.10` can consume model-visible images returned by allowed MCP tools. The
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

HER currently has two separately owned Habit/Meditation paths:

1. the runtime-governed SQLite candidate/evidence path in
   `orchestrator/her_habits.py` and `orchestrator/runtime_habits.py`, surfaced
   through `/skill habits`;
2. the default-off adapter-direct JSON path in `adapters/her_habits.py`,
   surfaced through `/habit`, where validated changes become active
   immediately.

Neither path may run for a non-HER actual executor or an ephemeral maintenance
call. Their storage, promotion, audit, and notification semantics are not
interchangeable. Because both can currently be eligible for one foreground HER
run when the adapter-direct path is enabled, mutual exclusion or consolidation
is a release gate. See
[HER_AGENT_HABIT_MEDITATION_CONTRACT.md](HER_AGENT_HABIT_MEDITATION_CONTRACT.md)
and [HER_HABIT_MEDITATION.md](HER_HABIT_MEDITATION.md).

## Streaming contract

The authenticated packaged HER may emit `stream-json`. HASHI consumes assistant,
thinking, tool, and usage events, but HASHI remains responsible for deciding which
events are visible and how final delivery is promoted. Encrypted or provider-redacted
reasoning must never be reconstructed or exposed.

Provider reasoning deltas are an exact byte-fragment contract. HER must preserve
leading, trailing, and whitespace-only fragments from the provider stream; HASHI
concatenates those raw fragments without trimming or guessing token boundaries. This
prevents both joined words (`Theusersays`) and invented spaces inside words
(`prov id er`).

## Binary contract

Production resolution uses `runtime_policy = require-packaged`. The adapter verifies the
platform, executable permission, manifest identity, and SHA-256 before execution. The
certified binary and its provenance are recorded in:

- `hashi_assets/her/manifest.json`
- `hashi_assets/her/certification_baseline.json`

A source checkout, PATH binary, or legacy external binary must not silently replace the
packaged runtime.

## Certification exceptions

The baseline is deliberately fail-closed:

- every Rust workspace test must pass;
- the full Rust workspace, including all targets, must pass Clippy with warnings denied;
- no diagnostic allowlist is active;
- any new test failure or Clippy diagnostic fails certification.

Run the full certification check with:

```bash
python scripts/verify_her_certification.py \
  --source-root /path/to/claw-code-hashi-4ea31c1
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
smokes, log review, and only then the intended wider `/reboot max`. Aptenra is a
separate consumer and needs its own Windows build, provenance, release register,
and product test matrix.

The current open HER gates are:

- enforce no-resume behavior for Flex/composed full-context turns;
- resolve or explicitly isolate the two Habit/Meditation pipelines;
- capture live image, PDF, audio, screenshot, session, and `/habit` canary
  evidence after reboot;
- build and certify a `.10`-equivalent Windows package before claiming platform
  parity.
