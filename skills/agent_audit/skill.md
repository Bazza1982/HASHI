---
id: agent_audit
name: Agent Audit
type: action
description: Local-only daily agent behavior audit report generation
run: agent_audit.py
---

Local-only audit skill.

Purpose:
- Generate the daily agent behavior audit report from local memory and audit artifacts
- Write the report into Lily's workspace
- Return a concise summary for Dad

Hard rules:
- Local execution only
- No OpenRouter
- No DeepSeek
- No external API use for this task
- Report-only
- No automatic remediation
