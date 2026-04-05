---
id: bad
name: Bad Signal
type: action
description: Record a negative signal — agent did something wrong. Processed into habits during dream.
run: bad.py
---

Record a /bad signal with optional comment. Context is captured automatically from the current transcript (including thinking tokens) and processed into habit candidates during the next dream.

Usage:
  /skill bad
  /skill bad [comment...]
