# Handoff and Escalation

## Lab vs client onsite

| Mode | Extra care |
|---|---|
| `lab_sample` | Faster iteration; still stash before hard reset |
| `client_onsite_lan` | Confirm before destructive change; privacy; shorter windows |

## Escalate to human operator when

| Situation | Why |
|---|---|
| No HDMI after reseat advice | Physical cable/power path |
| Physical power needed | PiKVM cannot press laptop power button |
| Login PIN / password required | Never store; user must type |
| Interactive Hermes/API setup wizard | Needs real TTY desktop session |
| Legal/privacy approval for client data access | Beyond technical fix |
| Hardware RMA | Out of software dual-channel scope |

## Escalate to integration (code)

| Situation | Action |
|---|---|
| Generic product bug | Commit on device branch → review on operator |
| Device-model-only quirk | Document in model SOP; avoid hardcoding |
| Launcher/Host policy bug | Fix in source + dual-channel smoke |

## Handoff note template

```text
adjacency / session_state:
channels used:
what user sees:
what terminal proves:
attempted fixes:
evidence path:
blockers:
next human action:
```
