---
id: good
name: Good Signal
type: action
description: Record a positive signal — agent did something well. Processed into habits during dream.
run: good.py
---

Record a /good signal with optional comment. Context is captured automatically from the current transcript (including thinking tokens) and processed into habit candidates during the next dream.

Usage:
  /skill good
  /skill good [comment...]
