# Preflight and Channel Select

Run this playbook before any dual-channel work.

## Goal

Decide whether the session can proceed, and which channels to use.

## Steps

### 1. Network adjacency

- Confirm operator and target share **LAN** (or later **VPN**).
- Probe target host reachability (ICMP and/or TCP SSH/RDP as applicable).
- If adjacency fails, set mode `adjacency_missing` and stop dual-channel claims.

### 2. Remote shell probe (Channel B)

- Authenticated shell to target (lab: `apten@192.168.0.41` with project key).
- Record: hostname, user, time zone, current time.
- Optional: `git -C <repo> rev-parse --short HEAD` when source debug applies.

### 3. Visual plane probe (Channel A)

- PiKVM or equivalent: health API + one snapshot.
- Classify video: `no_signal` | `login` | `desktop` | `app_modal` | `unknown`.
- HID online/offline from API if available.

### 4. Combined session state

| State | Meaning | Default plan |
|---|---|---|
| `offline` | No network | Physical / human |
| `remote_only` | Shell OK, no video | Remote diagnostics only; do not claim UI proof |
| `visual_only` | Video OK, no shell | KVM for UI; limited until shell restored |
| `dual` | Both OK | Full dual-channel loop |
| `pre_login` | At login/OOBE | KVM primary; remote may be limited |

### 5. Emit channel plan

Before acting, state explicitly:

```text
adjacency: lan|vpn|missing
session_state: ...
channel_plan: A|B|A+B
destructive_owner: A|B|none
evidence_root: <path>
```

## Hard stops

- Do not invent adjacency.
- Do not paste long commands via HID while planning remote work.
- Do not request or store customer PIN.
