---
id: bad
name: Bad Signal
type: action
description: Record a negative signal — agent did something wrong. Processed into habits during dream.
run: bad.py
---

Record a negative signal with optional comment via `/skill bad`. Context is captured automatically from the current transcript (including thinking tokens). If an OpenRouter key is available, the signal is processed into habit candidates immediately; otherwise it remains queued for the next dream.

Usage:
  /skill bad
  /skill bad [comment...]
