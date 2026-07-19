# Aptenra Dual-Channel Debug & Onsite Support EXP

Status: **candidate**

This EXP captures how HASHI operators debug and support an Aptenra target
device when the **operator machine and the target device share network
adjacency** (same LAN now; same VPN later).

It is **not** Personal-SKU-only rescue. It is a stable short/mid-term pattern
for:

- founder lab debugging (e.g. mother + HP Client Zero);
- **client-facing onsite** service (engineer + customer PC on one LAN);
- any product tier that ships as a single adjacent device.

## Intent

Use this EXP when a task needs **both**:

1. **See and touch the UI** (login, dialogs, desktop pet, Start shortcuts);
2. **Reliable terminal control** (logs, git, services, short scripts, evidence).

### Channels

| Channel | Role |
|---|---|
| **A · KVM / visual HID** | HDMI view, screenshots, short keyboard/mouse |
| **B · HASHI remote terminal** | SSH or authenticated remote shell, PowerShell, Git |

```text
Operator (HASHI + this EXP)
        │  same LAN  or  future same VPN
   ┌────┴────┐
   ▼         ▼
  KVM     Remote shell
   └────┬────┘
        ▼
   Target device
```

**Rule:** *KVM sees and clicks; Remote executes and proves.*  
At most one channel may perform a destructive action at a time.

## When to load

- HP / Client Zero debug, dual-channel
- Start Aptenra stuck, dialog, desktop pet invisible
- Onsite support on customer LAN
- Sync code mother ↔ test device over adjacent Git remote
- Collect evidence packs without cloud file sync

## Production rule ("done")

1. Preflight recorded network adjacency and channel plan.
2. User-visible state confirmed (KVM screenshot or explicit no-video state).
3. Terminal state proven (log event, process, git HEAD, or health probe).
4. Evidence written under the target evidence root (no secrets).
5. Escalation path clear if blocked (physical power, credentials, HDMI).

## Core operating principles

1. **Adjacency first** — no LAN/VPN reachability ⇒ no dual-channel service claim.
2. **Short HID only** — never paste long scripts through KVM keyboard injection.
3. **Remote scripts as files** — scp/copy a `.ps1`, then execute; avoid nested SSH quoting.
4. **Git is source truth** — not MEGA, not OneDrive for this EXP.
5. **No customer PINs** — never store or log PIN/password material.
6. **PiKVM Web UI stays product** — this EXP does not replace it; it uses APIs.
7. **Lab paths are examples** — generalise for other hostnames/users.

## Lab instance (APT-HW-0001)

Documented defaults for the first sample device (override on other targets):

| Item | Value |
|---|---|
| Host | `192.168.0.41` |
| User | `apten` |
| PiKVM mgmt | `10.0.0.3` |
| Source tree | `C:\AptenraDebug\src\Aptenra` |
| Bare remote | `C:\AptenraDebug\remotes\Aptenra.git` |
| Launcher | `%LOCALAPPDATA%\Aptenra\Launch-Aptenra.ps1` |
| Logs | `%LOCALAPPDATA%\Aptenra\debug\logs\` |
| Evidence | `C:\AptenraDebug\evidence\` |

## Playbooks

| Name | File |
|---|---|
| Preflight & channel select | `playbooks/00_preflight_and_channel_select.exp.md` |
| KVM visual session | `playbooks/01_kvm_visual_session.exp.md` |
| HASHI remote terminal | `playbooks/02_hashi_remote_terminal.exp.md` |
| Dual-channel loop | `playbooks/03_dual_channel_debug_loop.exp.md` |
| Start Aptenra smoke | `playbooks/04_start_aptenra_smoke.exp.md` |
| Adjacent Git sync | `playbooks/05_git_sync_adjacent.exp.md` |
| Evidence pack | `playbooks/06_evidence_pack.exp.md` |
| Handoff & escalation | `playbooks/07_handoff_and_escalation.exp.md` |

## Validators and failure memory

- Validators: `validators/dual_channel_validators.md`
- Failures: `failures/failure_memory.jsonl`

## Scope limit

Do not treat this as:

- a public-internet always-on rescue control plane;
- a substitute for PiKVM commercial UI productisation;
- a skill that transfers to arbitrary cloud-only endpoints without adjacency;
- a license to store customer credentials.

Promote from `candidate` toward `stable` only after repeated live dual-channel
runs and founder confirmation.
