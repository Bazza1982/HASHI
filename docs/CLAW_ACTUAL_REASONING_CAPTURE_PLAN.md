# Claw Actual Reasoning Capture Plan

## Status

Drafted: 2026-05-25

Goal: make `claw-cli` reasoning capture useful for live debugging, quality control, transcript review, and audit evidence. The current hidden-count output proves that provider reasoning exists, but it is not sufficient:

```text
provider reasoning block received (8 chars hidden)
```

The target behavior is to capture and stream the actual provider-returned reasoning details wherever the provider exposes them, matching the behavior already present in direct HASHI backends such as `openrouter-api` and `deepseek-api`.

## Problem

`claw-cli` currently receives OpenRouter / DeepSeek V4 reasoning chunks, but the Claw JSONL layer collapses them into count-only summaries before HASHI sees them.

Current Claw behavior:

```json
{
  "kind": "thinking_summary",
  "summary": "provider reasoning block received (8 chars hidden)",
  "thinking_chars": 8
}
```

This is not enough for:

- quality control
- debugging model decisions
- comparing Claw behavior against direct OpenRouter / DeepSeek backends
- runtime audit evidence
- post-hoc review in `transcript.jsonl` and Workbench

## Existing Behavior In Other Backends

Direct HASHI backends already stream provider-returned reasoning content into `KIND_THINKING`:

- `adapters/deepseek_api.py`
  - reads `delta.reasoning_content`
  - appends it to `reasoning_chunks`
  - emits `StreamEvent(kind=KIND_THINKING, summary=reasoning_text[:400])`
- `adapters/openrouter_api.py`
  - requests `payload["reasoning"] = {"enabled": True, "exclude": False}`
  - reads `delta.reasoning`
  - reads `delta.reasoning_details`
  - emits `StreamEvent(kind=KIND_THINKING, summary=snippet[:400])`

The HASHI runtime then already has the right plumbing:

- `_make_stream_callback()` receives `KIND_THINKING`
- `_thinking_flush_loop()` periodically flushes thinking
- `_flush_thinking()` writes thinking to transcript and user-visible channels when `/think` is on
- token audit records `thinking_tokens`

Therefore the missing part is Claw's stream-json event shape and HASHI's `claw_cli` mapping.

## Definition Of "Actual Reasoning"

For this plan, "actual reasoning" means reasoning content explicitly returned by the provider API in fields such as:

- `reasoning_content`
- `reasoning`
- `reasoning_details[].text`
- `reasoning_details[].summary`

It does not mean decrypting encrypted provider fields or inventing reasoning when the provider does not return it. If a provider sends an encrypted or redacted reasoning block, HASHI should record that fact clearly with metadata.

## Target Output

Instead of count-only messages, Claw should emit actual reasoning text when provider-returned text is available:

```json
{
  "kind": "thinking_delta",
  "text": "I need to inspect the adapter mapping first.",
  "thinking_chars": 44,
  "reasoning_source": "reasoning",
  "visibility": "provider_returned"
}
```

For `reasoning_details`:

```json
{
  "kind": "thinking_delta",
  "text": "Need to check the transcript and token audit.",
  "thinking_chars": 45,
  "reasoning_source": "reasoning_details.text",
  "visibility": "provider_returned"
}
```

If the provider emits only encrypted/redacted reasoning:

```json
{
  "kind": "thinking_redacted",
  "summary": "provider emitted encrypted reasoning block",
  "thinking_chars": 0,
  "reasoning_source": "reasoning_details.encrypted",
  "visibility": "provider_redacted"
}
```

## Design

### 1. Preserve reasoning text inside Claw runtime events

Current Claw path:

```text
OpenRouter / DeepSeek response
  -> openai_compat.rs
  -> ContentBlockDelta::ThinkingDelta { thinking }
  -> RuntimeStreamEvent::ThinkingProgress { chars, redacted }
  -> stream-json "thinking_summary" with hidden count
```

Target Claw path:

```text
OpenRouter / DeepSeek response
  -> openai_compat.rs
  -> ContentBlockDelta::ThinkingDelta { thinking }
  -> RuntimeStreamEvent::ThinkingDelta { text, source, visibility }
  -> stream-json "thinking_delta" with actual provider-returned text
```

Implementation notes:

- Add a `ThinkingDelta` or extend `ThinkingProgress` in `crates/runtime/src/conversation.rs`.
- Preserve chunk text from `ContentBlockDelta::ThinkingDelta { thinking }`.
- Attach source metadata where possible:
  - `reasoning_content`
  - `reasoning`
  - `reasoning_details.text`
  - `reasoning_details.summary`
- Keep `thinking_chars` for token estimation.
- Keep separate redacted/encrypted events for non-text reasoning details.

### 2. Track source fields in `openai_compat.rs`

Current parser normalizes reasoning through `first_non_empty_reasoning()`, which loses source information.

Target:

- return a small struct instead of a bare string:

```rust
struct ReasoningFragment {
    text: String,
    source: ReasoningSource,
    visibility: ReasoningVisibility,
}
```

- preserve source in both streaming and non-streaming paths.
- support multiple fragments when `reasoning_details` has more than one text/summary item.

This avoids flattening useful provider metadata too early.

### 3. Emit Claw stream-json events that HASHI can consume directly

Add JSONL event kinds:

- `thinking_delta`
- `thinking_summary`
- `thinking_redacted`

Recommended schema:

```json
{
  "kind": "thinking_delta",
  "text": "...",
  "thinking_chars": 123,
  "reasoning_source": "reasoning",
  "visibility": "provider_returned",
  "has_signature": false
}
```

For backwards compatibility:

- keep accepting old `thinking_summary`
- keep `thinking_chars` on all thinking events
- do not remove `message_stop`, `usage`, or `run_finished`

### 4. Update HASHI `adapters/claw_cli.py`

Current mapping:

```python
if kind == "thinking_summary":
    summary = str(event.get("summary") or "Claw thinking")
    return StreamEvent(kind=KIND_THINKING, summary=summary[:400], detail=detail)
```

Target mapping:

```python
if kind == "thinking_delta":
    text = str(event.get("text") or "")
    source = str(event.get("reasoning_source") or "")
    detail = f"thinking_chars={thinking_chars};source={source}"
    return StreamEvent(kind=KIND_THINKING, summary=text[:400], detail=detail)
```

Also:

- `thinking_redacted` should become a clear audit event, not a fake reasoning text.
- `_stream_json_usage()` should count `thinking_delta` and `thinking_redacted`.
- stream capabilities should remain `supports_thinking_stream=True`.

### 5. Improve persistence for quality control

Thinking details need to be easy to audit after the request finishes.

Required logs:

- `transcript.jsonl`
  - role: `thinking`
  - text: provider-returned reasoning chunks when `/think` is on
- `token_audit.jsonl`
  - `thinking_tokens`
  - `thinking_chars`
  - `thinking_event_count`
  - `thinking_redacted_count`
  - `thinking_sources`
- audit evidence files
  - observable thinking events
  - tool calls
  - tool results
  - final core/visible output

Do not overload `response.text` with reasoning content. Reasoning should remain a separate stream/log channel.

### 6. Add tests

Claw Rust tests:

- streaming `delta.reasoning` emits `thinking_delta` with text.
- streaming `delta.reasoning_content` emits `thinking_delta` with text.
- streaming `reasoning_details[].text` emits `thinking_delta` with source metadata.
- streaming encrypted/redacted reasoning emits `thinking_redacted`.
- non-streaming responses preserve reasoning text and source metadata.
- `cargo fmt --check`.

HASHI Python tests:

- `test_claw_cli_adapter.py`
  - `thinking_delta` maps to `KIND_THINKING` with actual text.
  - `thinking_redacted` maps to an audit-visible redaction event.
  - `_stream_json_usage()` counts `thinking_delta`.
  - old `thinking_summary` remains compatible.
- runtime pipeline tests:
  - `thinking_tokens` are non-zero when Claw emits thinking text.
  - transcript receives thinking text when `/think` is on.

Live smoke:

```text
/backend claw-cli deepseek/deepseek-v4-pro
/think on
/verbose on
```

Prompt:

```text
Use no tools. Think briefly before answering. Answer with marker CLAW_ACTUAL_REASONING_SMOKE_OK.
```

Expected:

- Workbench shows meaningful thinking text, not only hidden counts.
- `transcript.jsonl` has `role: thinking` rows with provider-returned reasoning text.
- `token_audit.jsonl` has non-zero `thinking_tokens`.

## Rollout Plan

### Phase 1: Claw event schema

Files:

- `rust/crates/runtime/src/conversation.rs`
- `rust/crates/rusty-claude-cli/src/main.rs`
- `rust/crates/api/src/types.rs`
- `rust/crates/api/src/providers/openai_compat.rs`

Outcome:

- Claw stream-json can emit actual provider-returned reasoning text.
- Old hidden-count events remain backwards compatible.

Validation:

```bash
cargo fmt -p api -p runtime -p rusty-claude-cli --check
cargo test -p api openrouter -- --nocapture
cargo test -p rusty-claude-cli --test output_format_contract
cargo check -p runtime -p tools -p rusty-claude-cli
```

### Phase 2: HASHI Claw adapter mapping

Files:

- `adapters/claw_cli.py`
- `tests/test_claw_cli_adapter.py`

Outcome:

- `thinking_delta` becomes `KIND_THINKING` with real text.
- `thinking_redacted` remains visible as redaction metadata.
- usage counting works for text and redacted events.

Validation:

```bash
python -m pytest tests/test_claw_cli_adapter.py tests/test_runtime_pipeline.py -q
python -m py_compile adapters/claw_cli.py orchestrator/flexible_agent_runtime.py
```

### Phase 3: audit and token metadata

Files:

- `orchestrator/runtime_pipeline.py`
- `orchestrator/runtime_audit.py`
- `orchestrator/audit_mode.py`
- tests around audit evidence and token audit

Outcome:

- `thinking_event_count`, `thinking_chars`, `thinking_sources`, and redaction counts are recorded.
- audit mode can review observable thinking evidence without scraping transcript text.

Validation:

```bash
python -m pytest tests/test_audit_mode.py tests/test_runtime_audit.py tests/test_runtime_pipeline.py -q
```

### Phase 4: live verification without HASHI runtime restart unless approved

Build Claw release binary:

```bash
cargo build --release -p rusty-claude-cli
```

Live check can use the next Claw subprocess if HASHI already points at the release binary. If Python adapter changes need live runtime reload, ask for explicit approval before any HASHI runtime restart or agent reboot.

Validation:

- send one Workbench smoke request to diaochan
- inspect:
  - `transcript.jsonl`
  - `token_audit.jsonl`
  - verbose Workbench output

## Acceptance Criteria

The fix is complete only when all are true:

1. Claw stream-json emits actual provider-returned reasoning text when available.
2. HASHI verbose output shows meaningful reasoning text, not hidden count spam.
3. `transcript.jsonl` records reasoning text under `role: thinking`.
4. `token_audit.jsonl` records non-zero thinking usage and source metadata.
5. redacted/encrypted provider blocks are recorded honestly as redacted, not silently dropped.
6. old Claw stream-json clients remain compatible.
7. tests cover OpenRouter `reasoning`, DeepSeek `reasoning_content`, and `reasoning_details`.
8. no HASHI runtime restart is performed without explicit user approval.

## Risks

- Providers may not always return reasoning text even when reasoning tokens are billed.
- Providers may return encrypted or redacted reasoning details.
- Reasoning text can be long; transcript flushing must chunk cleanly.
- Logging actual provider-returned reasoning may increase sensitive data retention in workspaces.
- Workbench display should avoid flooding the UI; transcript/audit logs can retain more detail than the visible pane.

## Recommended Implementation Order

1. Add Claw `thinking_delta` event with provider-returned text.
2. Preserve reasoning source metadata in `openai_compat.rs`.
3. Update HASHI `claw_cli.py` to map `thinking_delta` to `KIND_THINKING`.
4. Add tests for text, summary, redacted, and backwards-compatible hidden-count events.
5. Build release Claw binary.
6. Run live smoke on diaochan without HASHI restart.
7. If Python adapter reload is required for live runtime, request approval before reboot/restart.
