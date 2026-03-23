---
id: dream
name: Dream
type: action
description: Nightly AI reflection — consolidates today's conversations into long-term memory
run: dream.py
---

Dream is the nightly reflection process that helps agents grow over time.

Like human sleep consolidating memories, Dream reviews today's conversations and extracts what's truly worth remembering.

Usage:
  /skill dream on     — enable nightly dreaming (cron at 01:30)
  /skill dream off    — disable nightly dreaming
  /skill dream now    — run reflection immediately
  /skill dream undo   — undo the most recent dream (pure file restore, no LLM)
  /skill dream status — show dream status and last reflection summary
