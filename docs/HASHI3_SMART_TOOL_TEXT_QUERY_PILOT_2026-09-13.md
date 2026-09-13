# HASHI3 Smart Tool text-query pilot — 2026-09-13

Status: implemented and verified on HASHI3. No other HASHI instance has been
promoted or synchronised.

Scope: HASHI3 only. Promotion or synchronisation to another HASHI instance
requires a later explicit operation and its own adoption evidence.

## Approval and ownership

The operator requested implementation and testing on HASHI3 before any wider
sync. HER v2 owns tool choice, response to typed admission guidance, and
re-planning. PAO's Tool Registry owns permission, deterministic admission,
invocation, process cleanup, and audit. The implementation is replaceable
Functions-layer behavior plus ignored HASHI3 instance configuration; protected
Core is unchanged.

## Implemented candidate

- `log_query` performs case-sensitive or Unicode-aware case-insensitive literal
  matching in fixed-size UTF-8 chunks. It caps terms, matches, and excerpt size
  and does not materialise an entire JSONL record.
- Smart Tool admission inspects `grep`/`ripgrep` text-search shapes and a bounded
  prefix of authorised log-like targets. Risky only-matching context expansion
  or unbounded output against a detected large record returns
  `status: needs_replan` before a subprocess is created.
- The typed result names `log_query` as the safe alternative. HER's execution
  environment contract tells the model to follow that result and not evade the
  guard.
- HASHI3 explicitly configures a 1,000,000-byte large-record threshold, a
  33,554,432-byte bounded probe, and a 1,800-second foreground subprocess fuse.
  The fuse is a final process-local safeguard, not the primary detection method
  and not a HER Turn, stage, Tool-loop, or background-job deadline.

## Verification record

Red-state proof before implementation:

```text
4 failed in 0.58s
```

The failures proved that the old Registry dispatched the dangerous shell form,
did not expose `log_query`, advertised no instance safety deadline, and did not
pass the configured fuse to the executor.

Focused green check after implementation:

```text
python -m pytest -q tests/test_smart_tool_registry.py tests/test_tool_audit.py tests/test_her_v2_prompts.py tests/test_process_execution.py
94 passed in 6.03s
```

The Smart Tool scenario creates an 18,141,830-byte single-record JSONL fixture,
finds a mixed-case literal through `log_query`, returns a bounded excerpt, and
keeps the model-visible result below 2,000 characters.

Isolated Function-generation qualification:

```text
candidate generation sha256:e2c536ab051709495d25657b38d39ffdfb4ef4f00836630a550aeede2a7d2212
READY (isolated Worker PID 28768, then cleanly stopped without traffic takeover)
```

HASHI3 live adoption:

```text
Core PID retained: 4208
agent1 Worker PID: 2856 -> 15896
adopted generation: sha256:e2c536ab051709495d25657b38d39ffdfb4ef4f00836630a550aeede2a7d2212
status: online, healthy, idle
```

The live HER v2 canary was submitted through a typed TUI delivery policy with
`telegram.mirror: false`. HER v2 invoked `log_query` against the real
3,408,164-byte `codex_exec_events.jsonl`, then invoked the exact guarded shell
shape against a nonexistent canary path so an accidental dispatch would remain
harmless. The running Smart Tool ledger independently recorded:

```text
log_query  status=success       effect=observed  error_code=null
shell      status=needs_replan  effect=no_change error_code=unsafe_text_search
```

The shell result suggested `log_query`; admission rejected it before reading
the target or spawning a process. The canary Run completed successfully, and
the Agent was restored to its original `codex-cli / gpt-6-astra / max`
selection afterward.

Final regression gates:

```text
focused Smart Tool, process, HER prompt, and FYI tests: 98 passed in 5.96s
full pytest gate: 669 passed, 1 skipped in 119.37s
Ruff selected changed Python files: passed
Protected Core change check: passed
git diff whitespace check: passed
```

## Remaining release boundary

This remains a HASHI3-only pilot. Sync to other instances requires a separate
operator decision after review of this record and the final regression gates.
