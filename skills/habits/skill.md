---
id: habits
name: Habit Governance
type: action
description: Review Lily's habit recommendation report, govern cross-agent copies, and manage shared patterns/protocols
run: habits.py
---

Bridge-native management surface for the habit recommendation pipeline.

Usage:
  /skill habits                  — show status and quick usage
  /skill habits report           — regenerate Lily recommendation report
  /skill habits dashboard        — regenerate Phase 5 evaluation dashboard
  /skill habits list             — list recent copy recommendations
  /skill habits list pending     — list pending copy recommendations
  /skill habits approve 3,4      — approve recommendation ids 3 and 4
  /skill habits reject 7         — reject recommendation id 7
  /skill habits apply            — apply all approved copy recommendations
  /skill habits shared list      — list active shared patterns / protocols
  /skill habits shared promote zelda <habit_id> pattern
  /skill habits shared retire <shared_pattern_id>

Notes:
  - Mutating actions (`approve`, `reject`, `apply`, `shared promote`, `shared retire`) are Lily-only governance actions.
  - Read-only actions (`report`, `dashboard`, `list`, status) can be inspected from any agent.
