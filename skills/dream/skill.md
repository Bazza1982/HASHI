---
id: dream
name: Dream (compatibility route)
type: action
description: Compatibility entry; use native /dream for HER Habit maintenance
run: dream.py
---

Dream has moved to the native HER-only `/dream` command. HASHI intercepts
`/skill dream ...` and forwards the arguments without running this action.

The legacy general-memory implementation is retired and cannot write new
memories, edit `AGENT.md`, or delete old snapshots. Existing legacy files remain
read-only historical data.

Usage:
  /skill dream on     — compatibility alias for /dream on
  /skill dream off    — compatibility alias for /dream off
  /skill dream now    — compatibility alias for /dream now
  /skill dream undo   — compatibility alias for /dream undo
  /skill dream status — compatibility alias for /dream status
