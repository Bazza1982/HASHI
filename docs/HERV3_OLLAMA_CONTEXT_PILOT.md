# HERV3 Ollama context pilot

## Owner and approval

HERV3 owns this Function-level provider adaptation. On 2026-10-15 the user
authorized branch `feature-ollama`, an MSI Agent6 trial with Ollama/Qwen at
65,536 tokens, provider-specific fixes, and GPU observation before deciding
whether to retain the development. This is not a Core migration, PCM redesign,
merge approval, or authority to restart unrelated Agents/shared services.

On 2026-10-16 the user authorized continuing this branch at 49,152 tokens,
with an 8,192-token reasoning/output reserve, instead of the initial 64K
baseline. Local speech workloads must retain GPU capacity. This supersedes
the capacity target, not the ownership, adoption gates, or restart scope.

## Failure and decision

The captured failed request contained the user's task and completed tool
results. Its rendered input measured 38,588 tokens while the running Ollama
context was 32,768. Ollama 0.35.0's OpenAI-compatible request schema ignores
the adapter's `options.num_ctx`; server truncation could remove the only user
message before Qwen's template ran. The resulting `No user query found in
messages` error happened before model inference. Both the missing HASHI
capacity integration and destructive server truncation mattered.

The pilot uses an instance opt-in, `ollama_native_context: true`, on the
Agent's existing `allowed_backends` Ollama profile. No shared model catalogue,
PCM thresholds, Persona, Memory, CLI provider, or DeepSeek behavior changes.
Without the opt-in the existing OpenAI-compatible transport stays unchanged.

The native `/api/chat` request uses the existing capacity resolver and exact
`model_capabilities[model]` declaration:

- `context_window_tokens` becomes the effective `options.num_ctx`;
- `response_headroom_tokens` becomes `options.num_predict`;
- `truncate: false` prevents the service from silently pruning the task;
- an unknown capacity fails closed, and an estimated oversized input is
  rejected without rewriting the task or completed tool results;
- exact-model `ollama_num_batch` and `ollama_num_gpu` optionally bound batch
  scratch memory and GPU layer placement without changing model weights.
- native-only `ollama_keep_alive_seconds` optionally sets a nonnegative integer
  idle residency in seconds. The 48K candidate uses 90 seconds to retain a
  prefix between tool rounds, then release the model allocation when idle.
  This is not a GPU quota, concurrency broker, or immediate voice preemption.

The shared estimator is an estimate, not the model tokenizer. It provides an
early check with an output reserve; it is not proof of an exact token fit.
The native service remains responsible for hard input rejection. A recognized
native context-overflow rejection uses the existing typed capacity error.
No completed tool is automatically replayed to recover capacity.

## Protocol and evidence

The pilot currently accepts text messages and structured tools. Native media
inputs fail explicitly rather than being flattened or silently discarded.
Assistant tool argument objects and tool-result names are translated on copies
of the outbound messages. The original task/history are not mutated.

Native JSON/NDJSON preserves reasoning and answer streaming, awaits callbacks,
reuses the existing inactivity guard, and requires `done: true`. EOF, invalid
JSON, cancellation, and interrupted transport retain partial protocol/wire
evidence but cannot authorize tool execution. Missing finish reasons and output
length exhaustion remain failures.

Native overflow may wrap a llama.cpp JSON error inside the outer `error`
string. The structured `exceed_context_size_error` is recognized without
reclassifying unrelated HTTP 400 failures. The typed failure retains the
original HTTP response for private wire/error evidence.

Ollama's native `done_reason: stop` with structured tool calls is translated
to the harness's canonical `tool_calls` completion. This is explicitly marked
`finish_reason_source: ollama_native_translation`; the actual native reason
is retained in `provider_compatibility.native_done_reason` and unchanged raw
NDJSON records. Provider token counters are used when returned. Native output
tokens include reasoning; a separate reasoning-token count is not invented.

The native compatibility record now retains valid provider total/load/prompt/
generation durations in nanoseconds, plus observed first-response and
first-answer latencies. Token rates derive only from available counts and
positive durations; missing measurements stay unknown. Reported prompt counts
and duration do not establish an uncached-token rate or a cache-hit count.
Actual request context, output reserve, optional batch/layer controls and idle
residency are recorded alongside each physical provider call. Existing wire
capture and canonical completion decisions remain unchanged.

## Qualification and adoption

Focused red/green evidence: before the change, nine of ten initial pilot
cases failed; the non-opt-in compatibility case passed. The cases exercise
actual payload preparation, capacity rejection, native response parsing,
tool completion decisions, streaming callbacks, raw wire capture, and failure
boundaries. Additional checks cover incomplete/truncated results, read errors,
exact-model memory controls, and direct HERV3 completion consumers.

Tests run in an independent Python 3.12 environment, not the live Core
interpreter. Protected Core and unrelated checkout changes remain untouched.
Live qualification must separately record the actual context reported by
Ollama, original-request render-only preservation (no business-tool execution),
long-input inference, Agent6 Worker adoption, multi-round tool results, and
GPU/latency measurements. A successful initial canary is not long-term
stability. Gemma's configured capacity is not evidence that Gemma was tested.

### 2026-10-15 initial results

- 27 focused checks passed in the independent Python 3.12 environment,
  including the existing streaming-capture and HERV3 completion consumers.
  The captured native overflow envelope and missing-response-evidence cases
  were red before their fix; exact-model memory-control cases also failed
  against the pre-fix adapter before passing on the changed adapter.
- The original failing request rendered successfully with its user message
  preserved and eight tool definitions. This was render-only: no original
  business tool was executed or replayed.
- Qwen completed a synthetic 56,935-input-token task at an actual 65,536-token
  context and returned the correct marker. It generated 316 output tokens in
  573.22 seconds overall; measured prompt and generation rates were 102.29
  and 19.05 tokens/second respectively.
- Full-GPU trial: total device memory peaked at 23,995 MiB, minimum free
  memory was 332 MiB, peak GPU temperature was 70 C and peak power 326.37 W.
  These are whole-device measurements with other local GPU users present.
- With Qwen-only `ollama_num_gpu: 60` and `ollama_num_batch: 128`, a separate
  short production-adapter/real-read-only-registry check completed three tool
  rounds and four physical provider calls in 59.76 seconds. Markers, arithmetic,
  and unchanged fixture hashes passed. Actual context remained 65,536; model
  GPU allocation was smaller than total model allocation, confirming CPU
  offload. Whole-device peak was 22,758 MiB, minimum free 1,569 MiB, peak
  temperature 61 C and peak power 255.24 W. This was not the same long-input
  workload, so it does not establish a long-input speedup or memory peak.
- A deliberately over-limit synthetic request was rejected with HTTP 400:
  8,215 input tokens against a 1,024-token context, with structured native
  `exceed_context_size_error`. The intended 65,536-token trial allocation was
  subsequently restored through a render-only call.
- Actual Agent6 adoption did **not** occur. Its supported hot reboot rejected
  the candidate because the changed Function source is uncommitted. The old
  Agent6 Worker and all other Workers stayed unchanged. No commit, merge, Core
  migration, shared-service restart, or long-running Agent6 trial was performed.
- The unadopted instance profile was restored through the revisioned writer,
  retaining the candidate profile privately for a later authorized deployment.
  This avoids advertising a new capacity to an old provider implementation.
  User authorization to commit this scoped change is still needed before
  Agent6 can adopt it and begin the requested longer trial.

### 2026-10-16 48K continuation

- The candidate is 49,152 total / 40,960 usable input / 8,192 reasoning and
  output, with Qwen batch 128 and no forced GPU-layer offload. This remains
  an instance candidate: the active Agent6 profile was not overwritten to
  advertise a capacity that its old Worker cannot enforce.
- Focused red/green evidence for idle residency and timing records was seven
  failures before the implementation, then 33 passing checks including the
  existing streaming-capture and HERV3 completion consumers. Invalid residency
  values fail explicitly; legacy non-opt-in transport remains unchanged.
  The broader final run passed 180 checks across the pilot, stream capture,
  HERV3 adapter, meter cost and HERV3 configuration suites.
- A fresh real-service render-only check preserved the original user task and
  all eight tool definitions at an actual 49,152-token context. No original
  business tool was executed. The earlier 38,588-token diagnosis remains a
  separate measurement, not a new tokenizer estimate.
- The production adapter and real, restricted read-only ToolRegistry completed
  three chained file reads / four physical provider calls in 20.73 seconds.
  Both markers, arithmetic and unchanged fixture hashes passed. This was an
  independent harness, not the running Agent6. Native records included load,
  prompt, generation and first-response timing; output rates were about
  41.7-42.1 tokens/second in this short workload.
- A synthetic 13,937-input-token task returned its marker in 31.71 seconds.
  An immediately repeated prompt returned it in 7.36 seconds with a different
  output-token count. This is a warm-prefix observation, not a same-output
  speed comparison or a measured cache-hit percentage.
- A larger synthetic task used 39,537 input tokens, retained the configured
  8,192-token output reserve, and returned the correct marker: 220 output
  tokens, 35.91 seconds overall, about 37.23 output tokens/second. It shared
  a prefix with the earlier synthetic test, so this is not a cold-prefill
  benchmark. It does not exercise all 8,192 possible output tokens.
- A separate cold-model check first verified that Ollama had no loaded model,
  then repeated the 39,537-token synthetic task with the 8,192-token output
  allowance. The marker was correct: 236 output tokens, 61.45 seconds overall,
  including 12.14 seconds model loading and 42.92 seconds prompt evaluation;
  output was about 37.16 tokens/second. The 48K allocation remained fully GPU
  according to Ollama. This is not the original business task or a full-output
  stress run.
- Ollama reported model allocation equal to GPU allocation: 19,243,938,610
  bytes, context 49,152, throughout these checks. Whole-device memory peaked
  at 23,211 MiB; minimum free was 1,116 MiB, temperature 70 C and power
  349.78 W. Minimum available host RAM was about 9,747 MiB. This establishes
  reported full-GPU placement, not proof against Windows shared-memory paging.
- The already-resident local multimodal service remained running and its
  health endpoint returned OK. About 1.1 GiB free is still a narrow margin:
  active STT/TTS peak memory, latency and simultaneous-compute contention were
  not measured. Do not call the voice/headroom acceptance complete or retain
  48K unconditionally if a real voice workload exceeds this margin.
- Idle release was observed about 90.68 seconds after the preceding inference:
  `/api/ps` was empty and whole-device usage returned to 4,515 MiB, with
  19,812 MiB free. The local multimodal process remained alive. No explicit
  unload, process termination, or service restart was used. This observation
  preceded the separate cold-model check; a later request may load Qwen again.
- No global PCM change, shared-service restart, new permission, hidden tool
  replay, commit or merge was performed. Existing offset/limit file reads and
  bounded log search remain the paging mechanism; this change does not claim
  an automatic tool-result budgeting or cross-Worker GPU scheduler policy.
- Actual Agent6 adoption and the longer observation trial remain pending the
  committed-Function-source gate. The previous candidate rejection is not
  bypassed or retried merely because independent live tests passed. Gemma
  remains untested, despite its capacity declaration in the private candidate.

### 2026-10-16 deployment authorization

The user subsequently explicitly approved committing this scoped Ollama change,
deploying the 48K profile and rebooting MSI Agent6, with further task testing
reserved for the user. This authorizes the supported Agent6-only hot adoption,
not a merge/push, Core migration, shared Function restart, Ollama restart,
other-Agent reboot, old-business-task replay or speech stress test.

The preceding independent results and candidate-only state are historical
pre-deployment evidence. The deployment must separately verify the source
commit, revisioned profile write, terminal reboot receipt and new Agent6 Worker
identity. Record the observed outcome without treating command acceptance as
successful adoption; preserve unrelated checkout/configuration changes.

### 2026-10-16 Agent6 deployment outcome

- Source commit `4dd4cb1a` contains only this pilot's two adapter files, focused
  tests and three owning/FYI documents. Unrelated checkout work was not staged,
  committed or reset. The same 180 related checks passed again before commit.
- Only Agent6's existing Ollama profile was updated through the revisioned
  configuration writer: native context enabled, 49,152 total / 8,192 response
  allowance, Qwen batch 128 and 90-second idle residency. Other Agent settings,
  tools, model choices and global configuration were preserved.
- The supported Backend API lifecycle submitted exactly one `/reboot min`
  targeting Agent6. Its terminal receipt was `succeeded`, lifecycle `online`,
  with targets exactly `[agent6]`; acceptance alone was not used as success.
- Agent6's Worker PID, nonce and generation all changed. The newly active
  generation's two adapter byte hashes match both committed source and checkout.
  Its published runtime profile contains the deployed capacity/idle settings;
  `online`, `backend_ready`, `startup_success` and `worker_accepting` were true,
  and `worker_phase` was `ACTIVE`. HERV3/Ollama/Qwen remained selected.
- Core process identity and protected-source digest stayed unchanged, as did
  unselected Worker identities and observed Backend API/multimodal listeners.
  No broad reboot, Core/Ollama/voice restart, task replay or extra notification
  was performed.
- Detailed operation IDs, process identities, configuration revisions and
  artifact hashes remain in ignored Agent-local deployment evidence. No
  inference task or speech stress test was started after adoption: the user
  explicitly reserved subsequent functional testing. The earlier approximately
  1.1-GiB loaded-model margin remains a caution, not a voice acceptance pass.

## Rollback and trial limits

Retain the experiment on `feature-ollama`; do not merge or commit it implicitly.
Rollback uses the revisioned instance configuration writer to remove the pilot
opt-in and restore only this profile's saved capacity/memory fields, then an
Agent6-only supported hot reboot. Do not restore an old whole configuration
document, replay the old business request, kill unrelated GPU users, modify
model weights, or cold-restart Core/Ollama as a testing shortcut.
