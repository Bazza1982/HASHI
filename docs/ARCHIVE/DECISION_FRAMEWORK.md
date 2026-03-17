# Stress Test Decision Framework

This document defines how the monitoring agent (小英) should handle issues discovered during automated stress testing.

## Core Principles

1. **When in doubt, ask** — Don't guess on risky operations
2. **Log everything** — Every decision and action must be recorded
3. **Preserve data** — Never delete logs, transcripts, or config without explicit permission
4. **Minimal intervention** — Prefer restarting one agent over restarting the whole bridge

## Error Classification

### 🟢 GREEN — Auto-Fix Allowed

These errors have known patterns with safe, reversible fixes.

| Error Pattern | Fix Action | Risk Level |
|---------------|------------|------------|
| Telegram polling conflict | Restart affected agent | Low |
| OpenRouter closed client | Restart affected agent | Low |
| Empty inlineData | Send /new to agent | Low |
| Transient timeout (single) | Retry once, then skip | Low |

**Conditions for auto-fix:**
- Pattern is documented in DEBUGGING.md
- Fix action is well-defined and reversible
- No risk of data loss
- Single agent affected

### 🟡 YELLOW — Log and Monitor

These errors may self-resolve or need more data before acting.

| Error Pattern | Action | Escalate If... |
|---------------|--------|----------------|
| Network errors (httpx) | Log, continue testing | >5 in 10 minutes |
| Single request timeout | Log, skip that test | >3 consecutive |
| Non-critical agent hiccup | Log, monitor | Persists >15 min |

**Handling:**
- Record in decision log
- Include in periodic summary report
- Escalate to RED if threshold exceeded

### 🔴 RED — Pause and Notify

These errors require human decision before proceeding.

| Error Pattern | Why Escalate |
|---------------|--------------|
| Claude nested session | Environment/config issue |
| Codex chunk limit | Prompt structure issue |
| PTY/ConPTY failure | Windows compatibility |
| Multiple agents down | Systemic problem |
| Transcript corruption | Data integrity risk |
| Unknown error pattern | No safe fix known |

**Handling:**
1. Immediately pause stress test (don't stop, pause)
2. Collect diagnostic information
3. Send notification with options to operator
4. Wait for decision before resuming

## Decision Notification Format

When escalating to RED, provide:

```
🚨 **Stress Test Needs Decision**

**Error**: [Brief description]
**Affected**: [Agent(s) or component]
**Time**: [When it occurred]

**Diagnosis**:
[What the agent found when investigating]

**Options**:
1️⃣ [Safe option] — Risk: Low
2️⃣ [More aggressive option] — Risk: Medium
3️⃣ Pause and wait for manual inspection
4️⃣ Skip this issue and continue testing

**Agent Recommendation**: [Which option and why]
```

## Decision Log Format

All decisions are logged to `state/stress_test_decisions.jsonl`:

```json
{
  "timestamp": "2026-03-10T15:30:00+11:00",
  "error_type": "telegram_conflict",
  "classification": "GREEN",
  "affected_agents": ["lily"],
  "action_taken": "restart_agent",
  "decision_by": "auto",
  "outcome": "success",
  "notes": "Agent recovered after restart"
}
```

For RED escalations:

```json
{
  "timestamp": "2026-03-10T16:45:00+11:00",
  "error_type": "unknown_pattern",
  "classification": "RED",
  "affected_agents": ["sakura", "coder"],
  "action_taken": "pause_and_notify",
  "decision_by": "operator",
  "operator_choice": "option_1",
  "outcome": "pending",
  "notes": "Waiting for operator response"
}
```

## Prohibited Actions

The monitoring agent must NEVER:

- ❌ Delete any log files
- ❌ Delete or truncate transcript files
- ❌ Modify agents.json
- ❌ Modify secrets.json
- ❌ Modify source code files
- ❌ Restart the entire bridge process
- ❌ Kill processes outside the bridge ecosystem
- ❌ Make network requests to external services (except bridge API)
- ❌ Continue testing after a RED alert without operator approval

## Recovery Hierarchy

When something goes wrong, prefer fixes in this order:

1. **Wait and retry** — Many issues self-resolve
2. **Send /new** — Clear session state
3. **Restart single agent** — Via API, preserves other agents
4. **Skip and continue** — Test other agents, come back later
5. **Pause for human** — When none of above are safe

## Thresholds for Escalation

| Metric | YELLOW Threshold | RED Threshold |
|--------|------------------|---------------|
| Network errors | 3 in 5 min | 5 in 5 min |
| Agent restarts | 2 for same agent | 3 for same agent |
| Command failures | 30% fail rate | 50% fail rate |
| Chat timeouts | 3 consecutive | 5 consecutive |
| Agents offline | 1 non-critical | 2+ or any critical |

## Post-Test Review

After test completion, generate a decision summary:

- Total GREEN auto-fixes applied
- Total YELLOW issues logged
- Total RED escalations
- Operator decisions made
- New error patterns discovered (candidates for DEBUGGING.md)
