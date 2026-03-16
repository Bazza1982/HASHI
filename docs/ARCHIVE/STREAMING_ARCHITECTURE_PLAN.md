# Streaming Architecture Development Plan

**Project:** Bridge-U-F Orchestrator
**Goal:** Production-quality real-time streaming for `/verbose on`, summary-only for `/verbose off`
**Status:** ~90% implemented — needs testing, hardening, and polish

---

## Current State Summary

### What's Already Built

| Component | File | Status |
|-----------|------|--------|
| `StreamEvent` dataclass + constants | `adapters/stream_events.py` | DONE |
| `StreamCallback` type + `on_stream_event` in `BaseBackend.generate_response()` | `adapters/base.py:141-144` | DONE |
| Claude CLI `--output-format stream-json` + `readline()` | `adapters/claude_cli.py:93-190, 276-360` | DONE |
| Codex CLI event parsing in existing `readline()` loop | `adapters/codex_cli.py:149-183, 253` | DONE |
| Gemini CLI `readline()` + stderr heuristic parsing | `adapters/gemini_cli.py:74-94, 177-292` | DONE |
| OpenRouter SSE streaming with `stream: true` | `adapters/openrouter_api.py:73-178` | DONE |
| `_streaming_display_loop` in `agent_runtime.py` | `orchestrator/agent_runtime.py:607-719` | DONE |
| `_streaming_display_loop` in `flexible_agent_runtime.py` | `orchestrator/flexible_agent_runtime.py:1069-1172` | DONE |
| `_make_stream_callback` helper | Both runtimes | DONE |
| Verbose routing in `process_queue()` (both runtimes) | `agent_runtime.py:958-984`, `flexible_agent_runtime.py:1327-1350` | DONE |
| `FlexibleBackendManager.generate_response()` passes `on_stream_event` | `orchestrator/flexible_backend_manager.py:105-114` | DONE |

### What Needs Work

The following items are **not yet done or need verification/hardening**:

---

## Phase 1: End-to-End Testing (Priority: HIGH)

The streaming code has been written but may not have been tested live. The developer picking this up should:

### 1.1 Claude CLI stream-json Verification

**File:** `adapters/claude_cli.py`

- [ ] Verify `--output-format stream-json` works with the installed Claude CLI version
- [ ] Test that `_parse_stream_json_line()` correctly handles all event types emitted by Claude Code:
  - `{"type":"assistant","subtype":"thinking","content":"..."}`
  - `{"type":"tool_use","tool":"Read","input":{...}}`
  - `{"type":"tool_result","content":"..."}`
  - `{"type":"assistant","subtype":"text","content":"..."}`
  - `{"type":"result","result":"final text","duration_ms":...,"cost_usd":...}`
- [ ] Verify the `result` event properly delivers the final response text (line 118: `return event.get("result", "")`)
- [ ] Test fallback when `stream-json` is not supported — currently falls back to `text` format when `on_stream_event is None`
- [ ] Verify the race condition fix (local `proc` variable) works correctly in streaming mode (line 276+)
- [ ] Test with `--no-session-persistence` fallback path (lines 355-359) — does it retry correctly in streaming mode?

### 1.2 Codex CLI Event Parsing

**File:** `adapters/codex_cli.py`

- [ ] Verify `_parse_codex_event()` fires for real Codex JSON events
- [ ] Check all event types are actually emitted by Codex: `turn.started`, `item.started/command_execution`, `item.completed/command_execution`, `item.completed/file_change`, `item.started/todo_list`, `item.completed/agent_message`
- [ ] Look at `codex_exec_events.jsonl` for real event samples to validate the parsing logic

### 1.3 Gemini CLI Heuristic Parsing

**File:** `adapters/gemini_cli.py`

- [ ] Gemini's stderr patterns (`_STDERR_PATTERNS`, line 22-28) are regex guesses — verify against actual Gemini CLI stderr output
- [ ] The current patterns may not match Gemini CLI's actual output format. Run a real Gemini request with `/verbose on` and capture stderr to refine the regexes
- [ ] Consider: does Gemini CLI even emit anything useful to stderr? If not, this adapter will only show `text_delta` events from stdout lines

### 1.4 OpenRouter SSE Streaming

**File:** `adapters/openrouter_api.py`

- [ ] Verify `httpx` async streaming works with `self.client.stream("POST", ...)` (line 131)
- [ ] Test with actual OpenRouter API — does it return SSE with `data: ` prefix lines?
- [ ] Handle potential `httpx` streaming bugs (connection drops, partial chunks)

### 1.5 Telegram Display Loop

**Files:** `orchestrator/agent_runtime.py:607-719`, `orchestrator/flexible_agent_runtime.py:1069-1172`

- [ ] Test that `_streaming_display_loop` actually edits the placeholder message with activity events
- [ ] Verify rate limiting (2.5s interval) prevents Telegram 429 errors
- [ ] Test with a long-running task (>2 minutes) to ensure the display loop stays alive
- [ ] Verify the "Done" message appears at the end (line 718-719)
- [ ] Test that HTML parse mode doesn't break with special characters in event summaries (e.g., `<`, `>`, `&` in file paths)

---

## Phase 2: Bug Fixes & Hardening (Priority: HIGH)

### 2.1 HTML Escaping in Display Loop

**Problem:** Event summaries may contain `<`, `>`, `&` which break `parse_mode="HTML"`.

**Fix needed in both runtimes' `_streaming_display_loop`:**

```python
import html

def _build_display() -> str:
    elapsed = int(time.time() - started)
    header = f"🔍 <b>{html.escape(self.name)}</b> | {html.escape(str(engine))} | {elapsed}s\n"
    body = "\n".join(html.escape(line) for line in buffer[-MAX_LINES:])
    # ... rest unchanged
```

**Files to modify:**
- `orchestrator/agent_runtime.py` — `_build_display()` at ~line 648
- `orchestrator/flexible_agent_runtime.py` — `_build_display()` at ~line 1109

### 2.2 Gemini CLI Race Condition

**File:** `adapters/gemini_cli.py`

**Problem:** `_read_streaming()` uses `self.current_proc` directly (lines 188-257) instead of a local variable. This is the same race condition that was fixed in `claude_cli.py`. If `shutdown()` nulls `self.current_proc` during streaming, it will crash with `'NoneType' object has no attribute`.

**Fix:** Capture `proc = self.current_proc` at the top of `_read_streaming()` and use `proc` throughout, same pattern as `claude_cli.py`.

### 2.3 Queue Overflow Handling

**File:** Both runtimes' `_make_stream_callback`

- [ ] Verify the `asyncio.Queue(maxsize=200)` doesn't cause dropped events for very active tasks
- [ ] Check what happens when `put_nowait()` raises `QueueFull` — currently silently drops events (which is correct, but should be logged at DEBUG level)

### 2.4 Event Cleanup on Cancellation

- [ ] When `/stop` is issued during streaming, verify:
  1. The `stop_event` is set
  2. The display loop exits cleanly
  3. The queue is drained
  4. No orphaned `create_task` callbacks remain

---

## Phase 3: Quality & Polish (Priority: MEDIUM)

### 3.1 Richer Event Display

Current display is functional but basic. Consider:

- [ ] Show a **running tool count** in the header: `"🔍 sakura | claude-cli | 45s | 8 tools"`
- [ ] Truncate file paths to basename for readability: `Read: config.py` instead of `Read: /full/path/to/config.py`
- [ ] Group consecutive tool_start + tool_end into a single line after the tool completes

### 3.2 Verbose OFF Summary Enhancement

Currently `/verbose off` uses `_escalating_placeholder_loop` which only shows elapsed time. Consider adding a brief **completion summary** that includes:
- Total tool calls made
- Total duration
- Number of files read/edited

This data could be accumulated from `StreamEvent` objects even in non-verbose mode (just don't display them live).

### 3.3 Event Deduplication

Some backends may emit duplicate or near-duplicate events. Add a simple dedup check:
- Skip if `event.summary == last_event.summary` and `event.kind == last_event.kind` within 1 second

### 3.4 Gemini CLI Streaming Improvement

The Gemini CLI adapter has the **weakest streaming** because Gemini CLI doesn't emit structured events. Options to improve:

1. **Capture both stdout and stderr concurrently** (already done)
2. **Refine regex patterns** based on actual Gemini CLI output
3. **Consider Gemini API adapter** for full streaming (separate task — would need Google AI API key, function calling support, etc.)

---

## Phase 4: Testing Checklist

### Manual Testing Script

Run these scenarios and verify streaming behavior:

```
1. /verbose on → Send a coding task to Claude backend → Verify live activity feed
2. /verbose on → Send a coding task to Codex backend → Verify live activity feed
3. /verbose on → Send a coding task to Gemini backend → Verify at least text deltas
4. /verbose on → Send a simple question to OpenRouter → Verify text streaming
5. /verbose off → Send same tasks → Verify NO streaming, only escalating placeholder
6. /verbose on → Send task, then /stop mid-stream → Verify clean cleanup
7. /verbose on → Long task (>2 min) → Verify display loop stays alive
8. /verbose on → Task with file paths containing < > & → Verify no HTML breakage
```

### Logs to Check

- `bridge.log` — look for `StreamEvent` related warnings/errors
- Check Telegram for message edit failures (429, "message to edit not found")
- `codex_exec_events.jsonl` — verify events are being parsed correctly

---

## Architecture Reference

### Data Flow

```
User sends message
    → process_queue() checks self._verbose
    → Verbose ON:  creates asyncio.Queue + _make_stream_callback → _streaming_display_loop
    → Verbose OFF: creates _escalating_placeholder_loop (no stream callback)

    → generate_response(on_stream_event=callback_or_None)
        → Backend reads stdout/stderr line-by-line
        → Parses events → calls on_stream_event(StreamEvent(...))
        → Callback puts event into Queue
        → Display loop drains Queue → edits Telegram placeholder every 2.5s

    → Backend returns BackendResponse with final text
    → stop_event.set() → display loop exits
    → Final response delivered to Telegram
```

### Key Files

| File | Purpose |
|------|---------|
| `adapters/stream_events.py` | `StreamEvent` dataclass, `KIND_*` constants, `StreamCallback` type |
| `adapters/base.py` | `BaseBackend` with `on_stream_event` parameter |
| `adapters/claude_cli.py` | `--output-format stream-json`, `_parse_stream_json_line()`, `_read_streaming()` |
| `adapters/codex_cli.py` | `_parse_codex_event()` in existing readline loop |
| `adapters/gemini_cli.py` | `_parse_stderr_line()` heuristics, `_read_streaming()` |
| `adapters/openrouter_api.py` | SSE streaming with `_stream_response()` |
| `orchestrator/agent_runtime.py` | `_streaming_display_loop()`, `_make_stream_callback()`, verbose routing |
| `orchestrator/flexible_agent_runtime.py` | Same as above for flex runtime |
| `orchestrator/flexible_backend_manager.py` | Passes `on_stream_event` through to active backend |

---

## Notes for the Implementing Agent

1. **Don't rewrite what's already built.** The streaming plumbing is in place. Focus on testing, fixing bugs (especially the Gemini race condition and HTML escaping), and verifying end-to-end behavior.

2. **The Claude CLI streaming path is the highest value.** If you can only test one thing, test Claude CLI with `/verbose on`.

3. **The Gemini CLI streaming will be the weakest.** That's OK — Gemini CLI doesn't expose structured events. The heuristic stderr parsing is best-effort.

4. **Watch for Telegram rate limits.** The 2.5s edit interval should be safe, but test with active tasks that generate many events.

5. **The `_make_stream_callback` / Queue pattern is shared** between both runtimes. Changes to one should be mirrored to the other.
