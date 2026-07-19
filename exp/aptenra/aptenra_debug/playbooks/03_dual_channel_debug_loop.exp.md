# Dual-Channel Onsite Debug Loop

This is the core service loop for lab and client onsite sessions.

## Preconditions

- Playbook `00` completed with `session_state` in `dual` or an explicit partial plan.
- `destructive_owner` assigned to **one** channel only.

## Loop

```text
1. Remote  — health probe (service, process, last launcher events)
2. KVM     — snapshot user-visible state
3. Align   — timestamps (target local time) across log and screenshot
4. Remote  — minimal fix (config, restart component, git, ACL grant best-effort)
5. KVM     — accept UI (pet/shell/dialog gone)
6. Remote  — write evidence JSON; optional commit plan for lab only
```

Repeat until validators pass or escalation triggers.

## Parallelism rules

| Situation | Allowed |
|---|---|
| KVM double-clicks Start Aptenra | Remote **read-only** logs |
| Remote restarts Host service | No concurrent KVM full reboot clicks |
| Existing Electron running | Remote check before second start |
| Modal error dialog visible | Snapshot first, then dismiss once |

## Communication to human operator

Report in plain language:

- What the user would see
- What the terminal proves
- What will be tried next
- What requires human hands (power, PIN, HDMI reseat)

## Client onsite extra discipline

- Higher confirmation threshold before destructive changes.
- Prefer reversible fixes.
- Evidence pack stays on operator media / agreed path; strip personal content.
