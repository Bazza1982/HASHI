---
name: agent-audit
description: Use when generating or reviewing the local daily HASHI agent behavior audit without external API calls or automatic remediation.
---

# Agent Audit

Use the bundled `scripts/agent_audit.py` for deterministic scheduled runs. For interactive requests, inspect and explain the evidence before invoking it.

Purpose:
- Generate the daily agent behavior audit report from local memory, logs, transcripts, Jobs configuration, and automation implementations
- Write the report into Lily's workspace
- Return a concise summary for Dad

Hard rules:
- Local execution only
- No OpenRouter
- No DeepSeek
- No external API use for this task
- Report-only
- No automatic remediation
- Default reporting mode is delta-first: report new issues, status changes, unresolved risks, and decisions needed
- Do not repeat already-known and already-fixed issues unless their status changes or they regress
- Explicitly audit whether any enabled cron / automated job is using HASHI API, OpenRouter API, or DeepSeek API without explicit design approval
- Do not stop at cron config; inspect every enabled `automation:*` implementation and legacy `skill:*` compatibility route for API-hop patterns and shared fanout
- Every report must disclose `已检查 / 未检查 / 不能确认`
- If no unauthorized API path is found, say exactly what was checked so the report does not create false assurance
- If any automated non-cron path still uses API, flag it as a separate risk
- Approved exception: the onboarding startup wakeup injector in `bin/bridge-u.sh` is allowed and should not be reported as unauthorized unless its scope changes
- Focus the daily audit on unauthorized automation added later, especially any new cron, job, or automation path introduced by Lily that routes automated work through API without explicit approval
- The audit output must not present static historical templates as if they were fresh cross-agent findings
