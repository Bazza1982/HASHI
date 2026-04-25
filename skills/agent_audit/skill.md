---
id: agent_audit
name: Agent Audit
type: action
description: Local-only daily agent behavior audit report generation
run: agent_audit.py
---

Local-only audit skill.

Purpose:
- Generate the daily agent behavior audit report from local memory, logs, transcripts, cron config, and enabled skill implementations
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
- Do not stop at cron config; inspect every enabled `skill:*` implementation for API-hop patterns and shared-skill fanout
- Every report must disclose `已检查 / 未检查 / 不能确认`
- If no unauthorized API path is found, say exactly what was checked so the report does not create false assurance
- If any automated non-cron path still uses API, flag it as a separate risk
- Approved exception: the onboarding startup wakeup injector in `bin/bridge-u.sh` is allowed and should not be reported as unauthorized unless its scope changes
- Focus the daily audit on unauthorized automation added later, especially any new cron / job / skill path introduced by Lily that routes automated work through API without explicit approval
- The audit output must not present static historical templates as if they were fresh cross-agent findings
