# Florence-2 Vision Pipeline Spec

> **Owner:** HASHI (platform layer)
> **Status:** Approved design, pending implementation
> **Date:** 2026-04-04

---

## 1. Problem

Claude LLM processes images by encoding them into tokens. A single screenshot consumes 1,500–3,000+ tokens, filling the context window quickly and degrading reasoning quality in long conversations. Most screenshots in HASHI are English-language UI/terminal/document captures where full multimodal processing is overkill.

---

## 2. Solution

Insert a **Florence-2** (ONNX INT8, CPU inference) preprocessing layer that converts images to text descriptions before they reach the LLM. This reduces per-image token consumption by ~80% while preserving the information agents need.

---

## 3. Final Design

### 3.1 Pipeline

```
Image arrives (any source)
  │
  ├── /raw flag set for this image? ──── YES ──→ Original image sent to LLM (current behaviour)
  │
  └── NO
      │
      ├── Florence-2 OCR + description (CPU, ONNX INT8)
      │   with timeout budget (see §3.6)
      │
      ├── Language detection on OCR output (see §3.3)
      │     │
      │     ├── Non-English detected ──→ Auto /raw: original image sent to LLM
      │     │
      │     └── English confirmed ──→ Wrap text, send to LLM (see §3.4)
      │
      └── Florence-2 failure/timeout ──→ Fallback: original image sent to LLM
```

### 3.2 Two Insertion Points

| Image Source | Where to Intercept | Change |
|-------------|-------------------|--------|
| **User sends image** (Telegram) | Bridge layer, before message reaches LLM | Image → Florence-2 → text wrapper |
| **Agent takes screenshot** (`browser_cli.py`) | New `--describe` flag on the screenshot command | Returns structured result with `description` field; default (no flag) behaviour unchanged |

Both paths use the same Florence-2 processing function.

### 3.3 Language Detection

After Florence-2 produces OCR output, run the following detection in order:

**Step 1 — Script detection (primary)**

Count codepoints in Unicode blocks known to represent CJK and other non-Latin scripts:

```python
import unicodedata

CJK_RANGES = [
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0x3040, 0x309F),   # Hiragana
    (0x30A0, 0x30FF),   # Katakana
    (0xAC00, 0xD7AF),   # Hangul Syllables
    (0x0600, 0x06FF),   # Arabic
    (0x0900, 0x097F),   # Devanagari
    # extend as needed
]

def _is_cjk_or_script(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in CJK_RANGES)

def detect_non_english(ocr_text: str, threshold: float = 0.10) -> bool:
    """Return True if text is likely non-English."""
    if not ocr_text or len(ocr_text) < 10:
        # Too short to decide — treat as English (safe fallback)
        return False
    script_chars = sum(1 for c in ocr_text if _is_cjk_or_script(ord(c)))
    ratio = script_chars / len(ocr_text)
    return ratio > threshold
```

**Step 2 — OCR void check**

If Florence-2 produced < 10 characters of OCR output from an image that is clearly non-trivial in size (> 50 KB), that is a signal of a non-Latin visual-only image (logos, diagrams, dense CJK). Treat as non-English → auto /raw.

**Why not raw non-ASCII ratio?**

Raw `ord(c) > 127` counts box-drawing characters (─┼├), Unicode arrows (→←), math symbols (∑∏), and OCR garbage bytes that appear in failed CJK recognition — all of which appear in English terminal/code screenshots. Script-range matching is required to avoid these false positives.

### 3.4 /raw Scope Semantics

`/raw` applies **per-image, at the time of the message**, not as a persistent mode:

| Trigger | Scope | How it works |
|---------|-------|--------------|
| User sends `/raw` as standalone message | Next image only | Bridge sets a one-shot `raw_next_image` flag; flag cleared after first image is processed |
| User sends `/raw` together with image in same message | That image only | Bridge processes that message with raw mode; does not affect future messages |
| User says phrases like "看原图" | Next image only | Bridge NLU matches phrase → same one-shot flag |
| Auto-detection: non-English content detected | That image only | Auto-decided per image; does not persist |
| Florence-2 failure | That image only | Fallback for that image only |

There is **no sticky /raw session mode**. Users who want to send multiple images raw must use `/raw` before each one, or prefix the batch message with `/raw`. This avoids the common bug where one `/raw` command silently applies to all subsequent images for the rest of the conversation.

If a future use case requires persistent raw mode, it will be a separate `/rawmode on/off` command, not an overloaded `/raw`.

### 3.5 Text Wrapper Format

When Florence-2 successfully processes an image, the output is wrapped as:

```
[IMAGE_CONTENT source="{source_type}" original_path="{file_path}"]

--- LAYOUT ---
{Florence-2 layout description}

--- TEXT (OCR) ---
{Florence-2 extracted text, with tag characters escaped}

[/IMAGE_CONTENT]
```

**Tag character escaping in OCR text:**

The OCR text section must have `[/IMAGE_CONTENT]` escaped to prevent format injection:

```python
ocr_safe = ocr_text.replace("[/IMAGE_CONTENT]", "[/IMAGE\u200bCONTENT]")
```

The zero-width space (U+200B) is invisible in display but breaks exact tag matching. This is minimal and sufficient for this threat model (no public web browsing; all image sources are user-controlled or agent-controlled).

**`source` values:**

| Value | Meaning |
|-------|---------|
| `user_upload` | User sent via Telegram |
| `screenshot` | Agent captured via `browser_cli.py --describe` |

### 3.6 Concurrency, Warmup, and Timeout Budget

Florence-2 preprocessing **must not block message delivery**. This is a design constraint that requires explicit implementation, not just a slogan:

**Warmup**

Florence-2 ONNX model is loaded at bridge startup (not on first image). If load fails, the bridge logs a warning and sets `FLORENCE_AVAILABLE = False`; all images fall through to /raw for the session. No lazy loading.

**Timeout budget**

Each preprocessing call has a hard timeout of **30 seconds** (CPU float32, no KV cache: ~5–10 s per image; 30 s gives 3–5× headroom for large images):

```python
import concurrent.futures

_florence_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

def preprocess_with_timeout(image_path: Path, timeout: float = 8.0) -> PreprocessResult:
    future = _florence_executor.submit(_run_florence, image_path)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        log.warning("Florence-2 timeout on %s, falling back to raw", image_path)
        return PreprocessResult(raw_fallback=True, reason="timeout")
    except Exception as e:
        log.exception("Florence-2 error on %s: %s", image_path, e)
        return PreprocessResult(raw_fallback=True, reason="exception")
```

**Concurrency**

Single-worker thread pool (`max_workers=1`) prevents GPU/CPU memory spikes if multiple images arrive quickly. Additional images queue behind the current one; each still has its own timeout budget. If the queue exceeds **3 pending items**, new images skip preprocessing (raw fallback) with a `queue_full` reason logged. This cap prevents unbounded memory growth.

**No async blocking**

The bridge's async message handler must call `preprocess_with_timeout()` in a thread pool, not in the async event loop:

```python
loop = asyncio.get_event_loop()
result = await loop.run_in_executor(None, preprocess_with_timeout, image_path)
```

---

## 4. browser_cli.py Contract

### 4.1 Additive Change (Not Breaking)

The existing `browser_cli.py screenshot` command returns an image file path:

```bash
python browser_cli.py screenshot --url "..." --out /tmp/shot.png
# Output: /tmp/shot.png
```

This contract is **unchanged**. Adding Florence-2 preprocessing uses a new flag:

```bash
python browser_cli.py screenshot --url "..." --out /tmp/shot.png --describe
# Output: JSON on stdout
```

With `--describe`, the command outputs a JSON object:

```json
{
  "image_path": "/tmp/shot.png",
  "description": "[IMAGE_CONTENT source=\"screenshot\" original_path=\"/tmp/shot.png\"]\n\n--- LAYOUT ---\n...\n\n--- TEXT (OCR) ---\n...\n[/IMAGE_CONTENT]",
  "raw_fallback": false,
  "fallback_reason": null
}
```

Agents that want text output use `--describe`. Agents that want the raw path continue using the current form. Both are valid; neither is deprecated.

### 4.2 Migration Path

Agent prompts can be updated incrementally to use `--describe`. The `/raw` bypass is available as `--raw` flag with `--describe`:

```bash
python browser_cli.py screenshot --url "..." --out /tmp/shot.png --describe --raw
# Always returns raw image path in description field (no Florence-2)
```

---

## 5. Model Setup

### 5.1 Model

- **Model:** Florence-2-base (or Florence-2-large)
- **Format:** ONNX INT8 quantized
- **Inference:** CPU only (ONNX Runtime, CPUExecutionProvider)
- **Estimated size:** ~400MB (INT8)

### 5.2 Why CPU

- AMD Radeon 890M (GPU) shared memory is used by Ollama models
- CPU is mostly idle during agent operations
- INT8 on CPU: ~1–3 seconds per image (acceptable)
- No GPU memory contention

### 5.3 Model Storage

```
/home/lily/.cache/florence2-onnx/
├── florence2-base-int8.onnx
└── tokenizer/
```

---

## 6. Token Savings Estimate

| Scenario | Current (raw image) | With Florence-2 | Savings |
|----------|-------------------|-----------------|---------|
| Terminal screenshot | ~2,000 tokens | ~200–400 tokens | ~85% |
| UI screenshot | ~2,500 tokens | ~300–500 tokens | ~80% |
| Document/table | ~3,000 tokens | ~400–600 tokens | ~80% |
| Non-English (auto /raw) | ~2,500 tokens | ~2,500 tokens | 0% (bypassed) |

Over a typical conversation with 5–10 screenshots, this saves 10,000–25,000 tokens — roughly 10–20% of the context window.

---

## 7. Failure Handling

| Failure Mode | Behaviour |
|-------------|-----------|
| Florence-2 model not loaded at startup | `FLORENCE_AVAILABLE = False`; all images use /raw for session |
| Florence-2 processing timeout (>8s) | Kill future, fallback to /raw, log `reason=timeout` |
| Florence-2 crash/exception | Catch, fallback to /raw, log exception |
| ONNX Runtime not installed | Set `FLORENCE_AVAILABLE = False` on import, log warning |
| Queue full (>3 pending) | Skip preprocessing for that image, log `reason=queue_full` |
| Image file corrupt/unreadable | Report error to user, skip image |

**Core principle:** Florence-2 failure must never block message delivery. The system works fine without it (current behaviour); the pipeline is purely an optimization. This is enforced by the explicit timeout/executor design in §3.6, not assumed by convention.

---

## 8. Implementation Plan

### 8.1 Phase 1: Florence-2 Setup

1. Download/convert Florence-2 to ONNX INT8 format
2. Create `hashi/tools/vision_preprocess.py`:
   - `PreprocessResult(text: str | None, is_english: bool, raw_fallback: bool, reason: str | None)`
   - `load_florence()` — called once at startup, sets module-level `FLORENCE_AVAILABLE`
   - `preprocess_image(image_path: Path, timeout: float = 8.0) -> PreprocessResult`
3. Test on sample HASHI screenshots (English terminal, English UI, Chinese Telegram)

### 8.2 Phase 2: Bridge Integration

1. Call `load_florence()` in bridge startup sequence
2. Modify bridge image handler:
   - Intercept image before LLM call
   - Check `/raw` one-shot flag (see §3.4)
   - Call `preprocess_image()` via `loop.run_in_executor()`
   - If English result: wrap as `[IMAGE_CONTENT]` text, send as message
   - If non-English or failure: send original image (current path)
3. Add one-shot `/raw` flag logic

### 8.3 Phase 3: browser_cli.py Integration

1. Add `--describe` flag to `screenshot` command
2. If `--describe`: call `preprocess_image()`, output JSON result (see §4.1)
3. Default behaviour (no `--describe`) unchanged
4. Add `--raw` flag to force raw path in `--describe` output

### 8.4 Phase 4: Validation

1. Compare agent accuracy: same tasks with Florence-2 pipeline vs raw images
2. Measure actual token savings in real conversations
3. Verify non-English auto-detection works for Chinese/Japanese content
4. Verify timeout and queue-full fallback paths with synthetic delays

---

## 9. Original Image Retention

- All original images are **always saved** to disk (current behaviour unchanged)
- Original path included in `[IMAGE_CONTENT]` wrapper via `original_path` attribute
- `/raw` (one-shot) can be used to send the original image at any time
- Cleanup: images older than 7 days may be pruned (existing media cleanup policy)

---

## 10. What This Spec Does NOT Cover

- **No Qwen2.5-VL or second model** — Florence-2 + /raw fallback to Claude covers all cases
- **No sticky /raw session mode** — per-image semantics only; persistent mode is a separate future command
- **No \"description may be incomplete\" warnings** — unnecessary overhead; user triggers /raw when needed
- **No complex tag escaping** — zero-width space escape in §3.5 is sufficient for this threat model (no public web browsing, all sources user/agent controlled)

---

## 11. Decision Record

| Decision | Rationale |
|----------|-----------|
| Florence-2 over Qwen2.5-VL | Much smaller, faster, sufficient for English OCR. Visual reasoning handled by /raw → Claude |
| Single model over dual model | Qwen2.5-VL is a superset of Florence-2; if we need it, just use it alone. But we don't — /raw covers the gap |
| CPU over GPU | Avoid memory contention with Ollama on shared 890M GPU |
| Script-range detection over raw non-ASCII ratio | Box-drawing chars, Unicode arrows, math symbols appear in English terminal screenshots and would trigger false positives |
| /raw per-image (not sticky) | Sticky mode causes silent future-image bugs; per-image is explicit and predictable |
| --describe flag (not changed return type) | browser_cli.py existing callers must not break; additive flags are safe |
| Explicit executor + timeout | "Never blocks" is a design requirement, not a convention — it requires code to enforce |
| Zero-width space tag escape | Minimal injection isolation appropriate to threat model; not complex escaping |

---

## 12. Observability and Statistics

### 12.1 Event Log

Every image processed by the pipeline emits one structured log event to a JSONL file:

```
/home/lily/.cache/florence2-stats/events.jsonl
```

Each line is a JSON object:

```json
{
  "ts": "2026-04-04T14:23:01.123Z",
  "source": "user_upload",
  "image_bytes": 84200,
  "outcome": "ocr_success",
  "language": "english",
  "ocr_chars": 412,
  "inference_ms": 1340,
  "fallback_reason": null,
  "agent": "lily"
}
```

**`outcome` values:**

| Value | Meaning |
|-------|---------|
| `ocr_success` | Florence-2 ran, English detected, text sent to LLM |
| `auto_raw_nonen` | Florence-2 ran, non-English detected, original image sent |
| `user_raw` | User triggered /raw, preprocessing skipped |
| `fallback_timeout` | Florence-2 timed out, original image sent |
| `fallback_exception` | Florence-2 crashed, original image sent |
| `fallback_queue_full` | Queue full, preprocessing skipped |
| `fallback_unavailable` | Model not loaded at startup, original image sent |

**`language` values:** `"english"`, `"non_english"`, `"unknown"` (when /raw skipped detection)

### 12.2 Token Savings Estimate

For `ocr_success` events, the log also records an estimated token savings:

```json
{
  ...
  "estimated_raw_tokens": 2100,
  "estimated_ocr_tokens": 380,
  "estimated_saved_tokens": 1720
}
```

Token estimates use the rule of thumb: raw image ≈ `image_bytes / 40` tokens; OCR text ≈ `ocr_chars / 4` tokens. These are approximations, not exact counts, but they are consistent enough to track trends over time.

### 12.3 Query Script

```
/home/lily/projects/hashi/scripts/florence_stats.py
```

Usage:

```bash
# Summary for last 7 days
python florence_stats.py

# Summary for a specific date range
python florence_stats.py --from 2026-04-01 --to 2026-04-07

# Raw CSV dump for analysis
python florence_stats.py --csv
```

Output (default):

```
Florence-2 Pipeline Stats  2026-04-01 → 2026-04-07
════════════════════════════════════════════════════
Total images processed:     142
  ocr_success               108  (76.1%)
  auto_raw_nonen             18  (12.7%)
  user_raw                    9   (6.3%)
  fallback_timeout            4   (2.8%)
  fallback_exception          2   (1.4%)
  fallback_queue_full         1   (0.7%)

Avg inference time (success): 1,420 ms
Estimated tokens saved:      185,240

Language detection:
  English confirmed:         108  (87% of attempted)
  Non-English detected:       18  (13% of attempted)

Model health: OK (0 startup failures)
```

### 12.4 Alert Thresholds

The bridge logs a WARNING (visible in HASHI diagnostics) if rolling 24h stats exceed:

| Metric | Threshold | Meaning |
|--------|-----------|---------|
| `fallback_timeout` rate | > 20% | Model too slow — consider reducing timeout or switching model |
| `fallback_exception` rate | > 5% | Model unstable — check ONNX runtime, image formats |
| `auto_raw_nonen` rate | > 40% | Too many non-English images — check if threshold needs tuning |
| Avg inference time | > 5,000 ms | Performance regression |

These thresholds are configurable in `hashi/config.py`.

### 12.5 Implementation Note

`vision_preprocess.py` (from §8.1) writes the event log. The stats script reads it. No database required — JSONL is append-only, lightweight, and can be queried with standard tools. File is rotated monthly: `events-2026-04.jsonl`, `events-2026-05.jsonl`, etc.

---

_This spec will be updated as implementation progresses._
