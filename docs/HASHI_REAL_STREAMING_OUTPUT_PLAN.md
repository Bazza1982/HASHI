# HASHI Real Streaming Output Plan

**Goal:** make HASHI deliver assistant answers progressively, like Hermes, instead of showing a placeholder and then sending the full final answer at the end.

**Status:** implementation plan with Phase 0, Phase 1, Phase 2 capability marking, Phase 2b full-delta preservation, and CLI stream-json full-delta preservation implemented behind explicit capability/config gates. Direct Codex Responses streaming remains future work.

**Scope:** backend adapters, runtime streaming state, Telegram delivery, API gateway streaming, audit/transcript persistence, and all user-facing modes.

**Non-goal:** rewrite HASHI core identity, queue ownership, memory semantics, remote registry, or tool execution contracts.

**Review update:** Lily reviewed this plan after the first draft. The main corrections are included below: placeholder ownership must be split before final promotion can work; Claw CLI must be included; DeepSeek and Ollama already have streaming paths but need full-delta preservation; capability rollout should start with optional `getattr` checks; and final promotion must use wrapper/COS-processed `response_text`, not raw backend text.

---

## 1. Problem Statement

Current HASHI streaming is not real answer streaming for Telegram.

Today the runtime can create a placeholder and receive `StreamEvent` objects, but the final user-facing path is still mostly:

```text
send placeholder
run backend
delete placeholder
send final response with send_long_message()
```

This means the user still experiences the answer as arriving all at once, even if a heartbeat or activity preview was shown during generation.

Hermes behaves differently:

```text
provider streaming event
-> on_text_delta(text)
-> append to inflight assistant message
-> update UI/gateway/TTS immediately
-> complete final response without resending the full answer as a new message
```

The required update is therefore not a cosmetic placeholder change. It is a runtime and delivery boundary update: assistant deltas must become first-class volatile output state, while final persistence remains unchanged.

---

## 2. Design Principles

1. Real answer deltas must come from backend output, not from timers.
2. Telegram should edit the same visible answer message while the answer is being generated.
3. The final answer should promote the streamed message to final whenever possible, not delete it and send a duplicate.
4. Transcript, memory, audit, and cost accounting must continue to persist only the final response.
5. Backends that cannot produce text deltas must be labeled non-streaming and should not pretend to provide smooth answer streaming.
6. Verbose/tool activity streaming and answer text streaming are separate channels.
7. Streaming failures must degrade to the existing final-answer delivery path.
8. The change must preserve silent, bridge, background, audit, `/think`, `/verbose`, media, and remote flows.

---

## 3. Hermes Reference Model

Hermes has a true delta-first model.

Observed implementation pattern:

```text
run_conversation(..., stream_callback=...)
-> provider streaming call
-> on_text_delta(text)
-> agent._fire_stream_delta(text)
-> stream_delta_callback / _stream_callback consumers
-> final response assembled for persistence
```

Examples from local Hermes source:

- `conversation_loop.py` accepts `stream_callback` and prefers streaming paths.
- `codex_runtime.py` consumes `response.output_text.delta` and calls `on_text_delta(delta_text)`.
- `bedrock_adapter.py` consumes `contentBlockDelta.text` and calls `on_text_delta(text)`.
- `chat_completion_helpers.py` states that streaming fires callbacks for each text token/chunk.

HASHI should copy this architectural shape, not the exact code.

---

## 4. Current HASHI Streaming Inventory

Existing pieces:

| Component | Current role | Gap |
|---|---|---|
| `adapters/stream_events.py` | Defines `StreamEvent` and `KIND_TEXT_DELTA` | Event has only `summary`; no explicit final/inflight contract |
| `BaseBackend.generate_response(..., on_stream_event=...)` | Callback can receive backend events | Backends differ widely in delta quality |
| `openrouter_api.py` | Emits `KIND_TEXT_DELTA` from SSE `delta.content` | Good candidate for real answer streaming |
| `claude_cli.py` | Emits `KIND_TEXT_DELTA` from `text_delta` in stream-json | Good candidate if CLI emits partial text in live runs |
| `codex_cli.py` | Parses Codex CLI JSON lines | Does not emit real answer deltas from current `codex exec --json` path |
| `gemini_cli.py` | Best-effort CLI parsing | Needs verification; likely weak or non-delta depending on CLI behavior |
| `claw_cli.py` | Emits `assistant_delta` as `KIND_TEXT_DELTA` through stream-json | Conditional answer-stream candidate if stream-json is supported |
| `deepseek_api.py` | Already has streaming delta path | Needs full-delta preservation; do not treat as greenfield |
| `ollama_api.py` | Already has streaming line/delta path | Needs full-delta preservation; do not treat as greenfield |
| `runtime_pipeline.answer_preview_loop()` | Edits placeholder with deltas or heartbeat | Still preview-only; final delivery sends full answer separately |
| `send_long_message()` delivery | Sends final response | Needs stream-aware bypass/promote behavior |
| `api_gateway._handle_streaming()` | SSE response for API clients | Already streams if adapter emits deltas; falls back to one full chunk |

---

## 5. Target Architecture

Introduce a stream lifecycle object for each user-visible turn.

```text
QueueItem
-> setup_interactive_feedback()
-> create StreamedAnswerState if eligible
-> backend emits StreamEvent(KIND_TEXT_DELTA)
-> StreamedAnswerState accumulates answer_text
-> TelegramAnswerStreamer edits visible message on a throttle
-> backend returns BackendResponse(raw_final_text)
-> wrapper/COS may produce authoritative response_text
-> finalize streamed message:
   - if streamed text exists: edit same message to authoritative response_text
   - if final is too long: edit first message and send continuation chunks
   - if no deltas or stream failed: use existing send_long_message()
-> persist final response normally
```

Key distinction:

```text
streamed answer state = volatile UI state
wrapper/COS-processed response_text = authoritative final content
memory/transcript/audit = final content only
```

Important ownership split:

```text
cleanup_interactive_feedback() stops typing/tasks
stream finalization decides placeholder disposition
final delivery checks StreamFinalization before send_long_message()
```

The first implementation must not let cleanup delete the placeholder before stream finalization can promote it.

---

## 6. New Runtime Concepts

### 6.1 StreamedAnswerState

Proposed fields:

```python
@dataclass
class StreamedAnswerState:
    request_id: str
    chat_id: int
    placeholder: Any | None
    buffer: list[str]
    delta_count: int
    char_count: int
    started_at: datetime
    last_edit_at: float
    edit_count: int
    failed: bool
    failure_reason: str
    final_promoted: bool
```

Responsibilities:

- Append text deltas in order.
- Track whether a real text stream happened.
- Track edit success/failure.
- Decide whether final delivery can promote the existing message.

### 6.2 StreamedAnswerController

Proposed responsibilities:

- Consume `StreamEvent` objects.
- Route `KIND_TEXT_DELTA` to answer buffer.
- Route tool/progress/thinking events to verbose/audit channels without mixing them into the answer.
- Throttle Telegram edits.
- Finalize the streamed message.
- Emit instrumentation.

This can start inside `runtime_pipeline.py` and later move to a dedicated module such as `orchestrator/runtime_streaming.py` when stable.

### 6.3 Stream Finalization Contract

Add a return value to cleanup/finalize logic:

```python
@dataclass
class StreamFinalization:
    streamed: bool
    final_delivered: bool
    continuation_chunks_sent: int
    fallback_required: bool
    error: str
```

If `final_delivered=True`, the normal final `send_long_message()` path must not send the same answer again.

The finalization step must run after wrapper/COS processing has produced the final user-visible `response_text`. Streamed preview text may be raw backend text, but final promotion must use the same text that would otherwise be passed to `send_long_message()`.

---

## 7. Backend Coverage Plan

### 7.1 OpenRouter API

Current state:

- Already uses SSE when `on_stream_event` is present.
- Emits `KIND_TEXT_DELTA` from `delta.content`.

Required changes:

- Stop truncating text deltas too aggressively for answer streaming. `summary=content[:120]` is insufficient for exact reconstruction if larger chunks arrive.
- Add `detail` or a new field for full delta content, or use `summary` as full delta for `KIND_TEXT_DELTA`.
- Preserve current truncated display behavior for verbose activity separately.
- Add tests that streamed Telegram final equals returned `BackendResponse.text`.

Risk:

- Tool-call turns may interleave deltas and tool calls. The adapter should only emit answer text deltas when content is user-visible assistant answer text.

### 7.2 Claude CLI

Current state:

- Uses `--output-format stream-json`.
- Emits `KIND_TEXT_DELTA` for `content_block_delta` with `delta.type == "text_delta"`.

Required changes:

- Verify actual installed Claude CLI emits partial text in live HASHI runs.
- Preserve full text delta, not a display-truncated fragment.
- Ensure `result` final text remains authoritative.
- Add fixtures for `text_delta`, `thinking_delta`, tool input delta, and final `result`.

Risk:

- CLI version differences may change event names.
- Some coding/tool turns may produce few answer text deltas until the final result.

### 7.3 Codex CLI

Current state:

- Current `codex exec --json` path emits structured lifecycle events and final/intermediate `agent_message`, but not reliable per-token answer deltas.
- This is the main reason Zelda with `codex-cli` does not feel like Hermes.

Required changes:

Option A, preferred for Hermes parity:

- Add a direct Codex Responses streaming adapter or mode that uses the same conceptual path as Hermes:

```text
responses.create(..., stream=True)
-> response.output_text.delta
-> StreamEvent(KIND_TEXT_DELTA, full_delta)
-> final assembled BackendResponse.text
```

Option B, limited fallback:

- Keep `codex-cli` as non-answer-streaming.
- Emit progress/tool/thinking only.
- Mark capability as `supports_answer_stream=False`.
- Do not promise smooth output for this backend.

Required capability split:

```python
supports_activity_stream: bool
supports_answer_stream: bool
```

Risk:

- Direct Responses streaming may require credentials and SDK paths that differ from the current authenticated Codex CLI.
- Reconstructing true deltas from `agent_message` snapshots is risky and can duplicate or reorder text.

### 7.4 Gemini CLI

Current state:

- Best-effort CLI streaming via stdout/stderr parsing.
- Actual answer delta support is uncertain.

Required changes:

- Live-capture Gemini CLI output under HASHI.
- If it emits incremental answer lines, map them to full `KIND_TEXT_DELTA`.
- If it only emits final text, mark `supports_answer_stream=False`.
- Consider a native Gemini API adapter for proper answer streaming.

Risk:

- Heuristic parsing can create false deltas or leak non-answer status text into the answer.

### 7.5 Claw CLI

Current state:

- `adapters/claw_cli.py` already maps `assistant_delta` to `KIND_TEXT_DELTA`.
- Stream behavior depends on Claw stream-json support and runtime configuration.

Required changes:

- Add Claw CLI to the capability matrix.
- Preserve full assistant deltas for answer streaming.
- Add fixtures for `assistant_delta`, completion, and fallback events.
- Mark as `supports_answer_stream=True` only when stream-json is available and verified.

Risk:

- Claw event shapes may differ by version.
- Tool/action deltas must not be appended to visible answer text.

### 7.6 DeepSeek API

Current state:

- Existing adapter already has a streaming delta path.
- The current text delta emission truncates visible content for display-style streaming.

Required changes:

- Verify the existing SSE stream path end-to-end.
- Preserve full answer deltas for answer streaming rather than emitting only `content[:120]`.
- Handle reasoning content separately from visible answer content.
- Add tests that streamed deltas reconstruct the same final answer.

Risk:

- Some providers expose reasoning deltas differently or require model-specific fields.

### 7.7 Ollama API

Current state:

- Existing adapter already has a streaming line/delta path.
- The current text delta emission truncates visible content for display-style streaming.

Required changes:

- Verify the existing streaming response line handling.
- Preserve full answer deltas from `response` chunks.
- Finalize from `done=true` aggregate.
- Add tests that streamed deltas reconstruct the same final answer.

Risk:

- Local model streaming can produce very small chunks; Telegram edit throttling must batch them.

### 7.8 Mock/Test Backends

Required changes:

- Add fake streaming backend that emits deterministic deltas with delays.
- Add fake non-streaming backend that emits no deltas.
- Add fake erroring stream backend that fails mid-stream.

These fixtures are mandatory before changing live Telegram behavior.

---

## 8. Mode Coverage Plan

### 8.1 Normal Telegram Mode

Target behavior:

```text
send placeholder
edit same message with answer chunks
finalize same message
send continuation chunks only if needed
```

No duplicate final answer.

### 8.2 `/verbose off`

Target behavior:

- Real answer text still streams if backend supports answer streaming.
- Tool/progress activity is hidden.
- User sees the answer grow, not debug events.

This is the key user-facing change. Streaming answer output should not require `/verbose on`.

### 8.3 `/verbose on`

Target behavior:

- Answer text streams in the main answer message.
- Tool/progress events can either:
  - update a compact status line above the answer, or
  - use the existing verbose activity display separately.

Do not mix tool logs into the assistant answer text.

Recommended first implementation:

```text
one Telegram message = streamed answer
verbose activity = existing secondary/status behavior where available
```

If only one placeholder exists, answer text takes precedence over activity display.

### 8.4 `/think on`

Target behavior:

- Reasoning/thinking stream remains separate from answer stream.
- Thinking is never appended to visible answer.
- Existing thinking buffer/flush behavior remains unchanged unless explicitly redesigned.

### 8.5 Silent Items

Target behavior:

- No Telegram streaming.
- Backend may still emit events for audit if audit is active.
- Final response is persisted or suppressed according to existing silent behavior.

### 8.6 Bridge Requests

Target behavior:

- Bridge/API clients that request streaming should receive deltas.
- Bridge requests that do not request streaming should receive final response only.
- Telegram-specific streamed message state must not be created for non-Telegram bridge delivery.

### 8.7 API Gateway Streaming

Target behavior:

- Existing OpenAI-compatible SSE should stream `KIND_TEXT_DELTA`.
- If backend emits no deltas, gateway may keep current fallback of sending full text as one chunk.
- Add metadata/logging that reports whether the response was truly streamed or fallback-streamed.

### 8.8 Background Mode

Target behavior:

- Before detach threshold: stream answer normally.
- On detach: either keep the streamed partial message and edit it to "continuing in background", or finalize partial with a clear continuation notice.
- On background completion: edit or reply with final result without duplicating already streamed text.

First implementation can disable answer streaming after detach and use existing background notification.

### 8.9 Audit Mode

Target behavior:

- Audit collector receives stream telemetry.
- Audit evidence may record delta counts and timings.
- Audit prompt must not include raw full partial answer stream unless needed; final output remains authoritative.

### 8.10 Remote/HChat/Agent-to-Agent

Target behavior:

- Do not stream over remote/hchat unless the protocol explicitly supports partial message events.
- Keep final-message semantics for remote agent messages.
- Future extension can add `agent_message.delta` and `agent_message.complete` to remote protocol.

### 8.11 Media/File Responses

Target behavior:

- Text answer streaming only.
- File/photo/document sending remains final-action delivery.
- If a text preface streams before file delivery, finalization must not suppress file sending.

### 8.12 Long Telegram Messages

Target behavior:

- Telegram message edit limit is treated as a hard boundary.
- Stream only the latest editable chunk or first chunk until near the limit.
- On finalization, send continuation messages for remaining final text.

Recommended v1:

```text
stream first <= 3400 chars
on final, edit first message to first chunk
send remaining chunks via send_long_message()
```

---

## 9. Delivery Algorithm

### 9.1 During Generation

```text
if item is Telegram-visible and backend supports answer stream:
    create placeholder
    create StreamedAnswerState
    create answer event queue
    pass stream callback to backend
else:
    use existing placeholder/final delivery behavior
```

On each `KIND_TEXT_DELTA`:

```text
append full delta to buffer
if enough time passed and content changed:
    edit placeholder with accumulated answer preview
    log edit success/failure
```

Throttle defaults:

```text
first edit target: <= 0.7s after first delta
normal edit interval: 0.8-1.2s
429 backoff: Telegram-provided retry or 3s fallback
max editable chars: 3400 initially
```

### 9.2 On Completion

```text
response_text = wrapper/COS-processed visible final text
if streamed_state has real deltas and not failed:
    edit placeholder to response_text first chunk
    send continuation chunks if needed
    mark final_delivered=True
else:
    delete placeholder
    send_long_message(final_text)
```

Matching rule:

- Do not require streamed buffer to exactly equal final text.
- Wrapper/COS-processed `response_text` is authoritative and should replace the in-flight preview on final edit.
- Log mismatch ratio for diagnostics.

### 9.3 On Error

```text
if stream failed but backend returns final:
    fallback to final send_long_message()
if backend fails:
    edit placeholder to concise error or use existing error delivery
```

---

## 10. Capability Model

Current `supports_thinking_stream` is too broad.

Initial rollout should avoid adding new required dataclass fields. Use optional capability checks first:

```python
supports_answer_stream = bool(getattr(capabilities, "supports_answer_stream", False))
```

After the first behavior is proven, add explicit optional fields or defaults:

```python
supports_activity_stream: bool
supports_answer_stream: bool
supports_reasoning_stream: bool
supports_tool_stream: bool
```

Backend examples:

| Backend | Activity stream | Answer stream | Reasoning stream | Notes |
|---|---:|---:|---:|---|
| OpenRouter API | Yes | Yes, implemented behind config gate | Model-dependent | SSE `delta.content`; full deltas preserved |
| Claude CLI | Yes | Yes, implemented behind config gate | Yes if emitted | Stream-json `text_delta`; live version verification still required |
| Codex CLI current | Yes | No | Partial/progress only | Needs direct Responses path for true parity |
| Gemini CLI | Yes | Yes, implemented behind config gate | Unknown | Stream-json assistant `message` content; live version verification still required |
| Claw CLI | Yes | Conditional, implemented capability flag | Unknown | `assistant_delta` via stream-json when supported |
| DeepSeek API | Yes | Yes, implemented behind config gate | Model-dependent | Existing SSE path; full deltas preserved |
| Ollama API | Yes | Yes, implemented behind config gate | No/limited | Existing line stream path; full deltas preserved |

User-facing behavior must be driven by `supports_answer_stream`, not by `supports_thinking_stream`.

---

## 11. Implementation Phases

### Phase 0: Instrument Current Behavior

Status: implemented.

Files:

- `orchestrator/runtime_pipeline.py`
- `orchestrator/runtime_delivery.py` or current delivery helper location
- backend adapters as needed

Tasks:

- Log when answer streaming is eligible.
- Log backend capability flags using `getattr(..., False)` so Phase 0 has no broad adapter churn.
- Log every stream finalization result.
- Log edit count, delta count, char count, fallback reason.

Acceptance:

- A live Zelda run can prove whether it used real deltas or fallback.

### Phase 1: Streamed Final Message for Fake Backend

Status: implemented for guarded runtime finalization.

Tasks:

- Add `StreamedAnswerState`.
- Add streamed finalization path.
- Split cleanup ownership so stopping feedback does not automatically delete a placeholder that stream finalization owns.
- Add fake backend tests.
- Ensure final `send_long_message()` is skipped when final message was promoted.
- Promote only fake backend streams in this phase.

Acceptance:

- Unit test shows Telegram bot receives multiple edits and no duplicate final send.
- Final persisted response remains unchanged.
- Cleanup does not delete a placeholder that finalization will promote.

### Phase 2: OpenRouter Real Answer Streaming

Status: partially implemented. OpenRouter now advertises `supports_answer_stream=True`, preserves full answer deltas, and can use streamed final promotion when `answer_stream_final_delivery` is enabled. Live Telegram verification is still required before enabling broadly.

Tasks:

- Preserve full text deltas.
- Route `KIND_TEXT_DELTA` to streamed answer state.
- Verify `/verbose off` still streams the answer.
- Verify `/verbose on` does not mix tool/progress lines into answer.

Acceptance:

- Live OpenRouter long answer visibly grows in Telegram.
- Final answer is the same message, not a new duplicate.

### Phase 2b: Existing API Full-Delta Preservation

Status: implemented for OpenRouter, DeepSeek, and Ollama.

Tasks:

- Preserve full deltas for OpenRouter, DeepSeek, and Ollama.
- Keep display truncation separate from answer reconstruction.
- Add adapter tests for all three paths.

Acceptance:

- The streamed buffer can reconstruct the final visible answer without `content[:120]` truncation loss.

### Phase 3: Claude CLI Real Answer Streaming

Status: partially implemented. Claude stream-json `text_delta` now preserves full answer chunks and advertises answer streaming. Live CLI verification is still required.

Tasks:

- Verify actual stream-json event shape.
- Preserve full `text_delta`.
- Add tests with event fixtures.

Acceptance:

- Live Claude CLI long answer visibly grows in Telegram.

### Phase 4: Codex Strategy

Status: current `codex-cli` remains explicitly non-answer-streaming.

Decision point:

- If direct Codex Responses streaming credentials/path are available, implement a `codex-responses` or `codex-api` backend.
- If not, mark current `codex-cli` as non-answer-streaming and stop presenting it as smooth streaming.

Acceptance for direct path:

- `response.output_text.delta` produces `KIND_TEXT_DELTA`.
- Zelda on Codex direct path streams like Hermes.

Acceptance for fallback path:

- UI clearly logs and reports non-streaming backend fallback.
- No heartbeat is described as answer streaming.

### Phase 5: Remaining Backends

Status: partially implemented. Claw CLI now advertises answer streaming only after stream-json support is detected; DeepSeek and Ollama are represented as real streaming API backends with full-delta preservation. Gemini stream-json assistant messages now preserve full answer chunks and advertise answer streaming, pending live version verification.

Tasks:

- Claw CLI: verify stream-json `assistant_delta` and mark conditional answer-stream support.
- DeepSeek API: verify existing SSE streaming and full-delta preservation.
- Ollama API: verify existing line streaming and full-delta preservation.
- Gemini CLI: verify or mark non-answer-streaming.
- Any legacy/fixed backend: define explicit capability.

Acceptance:

- Capability matrix is accurate.
- Each backend has at least one test proving streaming or fallback.

### Phase 6: Background, Bridge, and Long Message Hardening

Tasks:

- Background detach behavior.
- API gateway true-vs-fallback streaming metadata.
- Long final answer chunk promotion.
- Cancellation cleanup.

Acceptance:

- No duplicate messages.
- `/stop` leaves no orphaned edit loops.
- Long answers finalize cleanly.

---

## 12. Test Plan

### Unit Tests

Add tests for:

- Fake backend emits `["Hello", " ", "world"]`; Telegram edits accumulate text.
- Finalization edits streamed message to wrapper/COS-processed authoritative final text.
- Normal final delivery is skipped when `final_delivered=True`.
- Cleanup stops feedback without deleting stream-owned placeholder.
- No deltas means existing final delivery path is used.
- Stream edit failure falls back to final delivery.
- Long final text streams first chunk and sends continuation chunks.
- `/verbose off` still answer-streams.
- `/verbose on` keeps tool events out of answer text.
- Silent item does not create Telegram stream.
- Background detach stops or marks stream appropriately.

### Adapter Tests

Add fixtures for:

- OpenRouter SSE `delta.content`.
- Claude CLI `content_block_delta` text events.
- Claw CLI `assistant_delta` events.
- Codex Responses `response.output_text.delta` if direct path is implemented.
- DeepSeek SSE deltas with full content preservation.
- Ollama streaming JSON lines.
- Non-streaming backend fallback.

### Live Tests

Run:

```text
1. Telegram + OpenRouter + /verbose off + long answer
2. Telegram + OpenRouter + /verbose on + long answer with tool use
3. Telegram + Claude CLI + long answer
4. Telegram + Claw CLI + long answer if stream-json is available
5. Telegram + Codex current CLI + long answer, verify explicit non-stream fallback
6. API gateway stream=true + OpenRouter
7. /stop during streamed answer
8. Long answer > Telegram edit limit
9. Background detach during streamed answer
```

Logs must show:

```text
answer_stream eligible=true backend=openrouter-api
answer_stream_delta request_id=... delta_count=...
answer_stream_edit_success request_id=... edit_count=...
answer_stream_finalize promoted=true continuation_chunks=...
```

---

## 13. Rollback Plan

Add config flag:

```json
{
  "answer_stream_final_delivery": true
}
```

Rollback behavior:

- If disabled, use existing placeholder plus final `send_long_message()`.
- Keep backend streaming events available for API/audit if already safe.
- Do not remove old delivery path until several live backends pass.

---

## 14. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Telegram edit rate limits | Stream stalls or warnings | Throttle, backoff, final fallback |
| Backend emits partial snapshots instead of deltas | Duplicated answer text | Adapter-specific delta normalization |
| Final text differs from streamed text | User sees correction at finalization | Final text replaces preview; log mismatch |
| Tool/progress events leak into answer | Bad user output | Separate answer/activity channels |
| Long answer exceeds edit limit | Finalization fails | Stream first chunk, send continuations |
| Codex CLI cannot provide deltas | Zelda remains non-smooth | Add direct streaming backend or honest fallback |
| Cleanup deletes placeholder before finalization | Stream promotion cannot happen | Split feedback cleanup from placeholder disposition |
| Wrapper/COS final text differs from raw streamed text | Final edit appears to jump | Promote wrapper/COS `response_text` as authoritative and log mismatch |
| Background detach duplicates text | Confusing Telegram output | Explicit detach finalization contract |
| Memory polluted by partial output | Core consistency regression | Persist final response only |

---

## 15. Definition of Done

HASHI has real streaming output when:

1. At least one production backend streams visible answer text into Telegram progressively.
2. `/verbose off` still streams the answer text.
3. `/verbose on` streams answer text without mixing tool/progress logs into the answer.
4. Final answer is promoted into the streamed message instead of duplicated.
5. Non-streaming backends are accurately detected and fall back honestly.
6. Memory, transcript, audit, and token accounting use final response only.
7. API gateway still supports OpenAI-compatible SSE.
8. Live logs can prove whether a turn was truly streamed or fallback-delivered.
9. Tests cover streaming, fallback, cleanup ownership, long messages, cancellation, silent mode, verbose mode, and at least two real streaming adapters.
10. Claw CLI, DeepSeek, and Ollama are represented accurately in the capability matrix.

---

## 16. Recommended First Patch

The first implementation checkpoint is complete:

1. Add instrumentation plus `getattr(capabilities, "supports_answer_stream", False)` capability detection without changing backend behavior.
2. Add `StreamedAnswerState` and fake backend tests.
3. Split cleanup/finalization ownership so stream finalization controls placeholder disposition.
4. Implement final-message promotion for fake deltas only.
5. Preserve full deltas for OpenRouter, DeepSeek, and Ollama.
6. Enable real final promotion for OpenRouter behind a config flag after tests pass.
7. Leave `codex-cli` as explicit non-answer-streaming until a direct Responses streaming path exists.

This keeps HASHI core stable while moving the user-visible output path toward Hermes-style streaming.
