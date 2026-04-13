---
id: good
name: Good Signal
type: action
description: Record a positive signal — agent did something well. Processed into habits during dream.
run: good.py
---

Record a positive signal with optional comment via `/skill good`. Context is captured automatically from the current transcript (including thinking tokens). If an OpenRouter key is available, the signal is processed into habit candidates immediately; otherwise it remains queued for the next dream.

Usage:
  /skill good
  /skill good [comment...]
